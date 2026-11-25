import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


@dataclass(frozen=True)
class Limits:
    user_per_minute: int = 12
    public_per_minute: int = 60
    registrations: int = 50
    renewals: int = 100
    renewals_per_user: int = 3

    def __post_init__(self):
        if any(value < 1 for value in vars(self).values()):
            raise ValueError("Лимиты частоты и очередей должны быть положительными.")


class Bucket:
    def __init__(self, rate, burst, now):
        self.rate = rate / 60
        self.capacity = min(rate, burst)
        self.tokens = float(self.capacity)
        self.updated = now

    def refill(self, now):
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now


class Admission(BaseMiddleware):
    """Reject excess public work before SQL, panel reads and outgoing Telegram calls."""
    def __init__(self, admin, limits, clock=time.monotonic):
        self.admin, self.limits, self.clock = admin, limits, clock
        self.public = Bucket(limits.public_per_minute, 12, clock())
        self.users = OrderedDict()
        self.active = set()
        self.dropped = 0
        self.last_report = clock()

    def admit(self, uid):
        now = self.clock()
        self.public.refill(now)
        if self.public.tokens < 1 or uid in self.active or len(self.active) >= 3:
            return False
        # A minute of inactivity always refills these buckets. Expire inactive ids
        # and fail closed at the memory bound, even with an inflated configured rate.
        while self.users and next(iter(self.users.values())).updated <= now - 60:
            self.users.popitem(last=False)
        bucket = self.users.get(uid)
        if bucket is None:
            if len(self.users) >= 4096:
                return False
            bucket = self.users[uid] = Bucket(self.limits.user_per_minute, 6, now)
        bucket.refill(now)
        self.users.move_to_end(uid)
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        self.public.tokens -= 1
        return True

    async def __call__(self, handler, update, data):
        event = update.callback_query or update.message
        msg = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(msg, Message) or not event.from_user or msg.chat.type != "private":
            return None
        uid = event.from_user.id
        if uid != msg.chat.id:
            return None
        if uid == self.admin:
            return await handler(update, data)
        if not self.admit(uid):
            self.dropped += 1
            if self.clock() - self.last_report >= 60:
                logging.getLogger(__name__).warning("Rate/concurrency limits skipped %d updates", self.dropped)
                self.dropped = 0
                self.last_report = self.clock()
            return None
        self.active.add(uid)
        try:
            return await handler(update, data)
        finally:
            self.active.remove(uid)

