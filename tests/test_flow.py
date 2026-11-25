import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Update

from watcher.__main__ import backup
from watcher.panel import Client, DAY, PanelError, WriteRejected
from watcher.service import REACTIVATE, Service, UserNotice
from watcher.store import Store
from watcher.telegram import card, delivery, dispatcher, flush

NOW = 1_800_000_000_000
USER, ADMIN = 424242, 900100


class FakePanel:
    def __init__(self):
        self.client = Client(dict(id=7, email="client-a", tgId=0, expiryTime=NOW+2*DAY, enable=True,
                                  totalGB=1000, uuid="stable-uuid", subId="stable-sub"))
        self.failure = None
        self.writes = 0
        self.online = True

    async def get(self, email):
        if not self.online:
            raise PanelError("offline")
        return self.client if email == self.client.email else None

    async def owned(self, user):
        c = await self.get(self.client.email)
        return [c] if c.owner == user else []

    async def change(self, client, field, value, *, activate=False):
        self.writes += 1
        if self.failure == "reject":
            raise WriteRejected("write rejected")
        if self.failure == "before":
            raise PanelError("timeout")
        client.fields[field] = value
        if activate:
            client.fields["enable"] = True
        if self.failure == "after":
            raise PanelError("response lost")


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"watcher.db"
        self.panel = FakePanel()
        self.t = NOW
        self.reopen()

    def reopen(self):
        self.db = Store(self.path)
        self.s = Service(self.db, self.panel, ADMIN, clock=lambda: self.t)

    def tearDown(self):
        self.db.db.close()
        self.tmp.cleanup()

    async def bind(self):
        await self.s.register(USER, "test", "client-a")
        r = await self.s.refresh(1)
        await self.s.approve(1, ADMIN, r["rev"])
        self.assertEqual(self.db.request(1)["state"], "done")
        return self.panel.client

    async def payment(self):
        c = self.panel.client
        await self.s.paid(USER, "test", c.id, c.expiry)
        return await self.s.refresh(2)

    async def test_early_renewal_and_repeat(self):
        c = await self.bind()
        old = c.expiry
        r = await self.payment()
        self.assertEqual(r["id"], (await self.payment())["id"])
        await self.s.approve(2, ADMIN, r["rev"])
        self.assertEqual(c.expiry, old + 30*DAY)
        with self.assertRaises(UserNotice):
            await self.s.approve(2, ADMIN, r["rev"])
        with self.assertRaises(UserNotice):
            await self.s.paid(USER, None, c.id, old)
        self.assertEqual(self.panel.writes, 2)

    async def test_expired_disabled_requires_explicit_activation(self):
        c = await self.bind()
        c.fields.update(enable=False, expiryTime=NOW-DAY)
        r = await self.payment()
        self.assertEqual(r["issue"], REACTIVATE)
        with self.assertRaises(UserNotice):
            await self.s.approve(2, ADMIN, r["rev"])
        await self.s.approve(2, ADMIN, r["rev"], activate=True)
        self.assertEqual(c.expiry, NOW+30*DAY)
        self.assertTrue(c.fields["enable"])

    async def test_conflicting_date_requires_new_preview(self):
        c = await self.bind()
        r = await self.payment()
        c.fields["expiryTime"] += DAY
        with self.assertRaises(UserNotice):
            await self.s.approve(2, ADMIN, r["rev"])
        self.assertEqual(self.panel.writes, 1)
        r = self.db.request(2)
        await self.s.approve(2, ADMIN, r["rev"])
        self.assertEqual(c.expiry, NOW + 33*DAY)

    async def test_owner_change_invalidates_buttons_requests_and_deliveries(self):
        c = await self.bind()
        old = await self.payment()
        await self.s.reminders()
        await self.s.register(USER+1, "other", c.email)
        r = await self.s.refresh(3)
        with self.assertRaises(UserNotice):
            await self.s.approve(3, ADMIN, r["rev"])
        await self.s.approve(3, ADMIN, r["rev"], replace=True)
        with self.assertRaises(UserNotice):
            await self.s.approve(2, ADMIN, old["rev"])
        with self.assertRaises(UserNotice):
            await self.s.paid(USER, None, c.id, c.expiry)
        self.assertEqual(await self.s.subscriptions(USER), [])
        for item in self.db.rows("SELECT * FROM outgoing WHERE chat=? AND kind IN ('reminder','success')", USER):
            self.assertIsNone(await delivery(self.s, item))

    async def test_lost_response_then_restart(self):
        c = await self.bind()
        r = await self.payment()
        self.panel.failure = "after"
        with self.assertRaises(PanelError):
            await self.s.approve(2, ADMIN, r["rev"])
        self.db.db.close()
        self.reopen()
        self.assertEqual(self.db.request(2)["state"], "applying")
        self.panel.failure = None
        await self.s.reconcile(2)
        self.assertEqual(self.db.request(2)["state"], "done")
        self.assertEqual(self.panel.writes, 2)
        self.assertEqual(c.expiry, NOW+32*DAY)

    async def test_timeout_before_write_preserves_target(self):
        c = await self.bind()
        c.fields["expiryTime"] = NOW-DAY
        r = await self.payment()
        self.panel.failure = "before"
        with self.assertRaises(PanelError):
            await self.s.approve(2, ADMIN, r["rev"])
        self.t += DAY
        self.panel.failure = None
        await self.s.reconcile(2)
        self.assertEqual(c.expiry, NOW+30*DAY)

    async def test_approval_of_viewed_card_survives_offline_panel(self):
        c = await self.bind()
        r = await self.payment()
        self.panel.online = False
        await self.s.approve(2, ADMIN, r["rev"])
        self.assertEqual(self.db.request(2)["state"], "applying")
        self.panel.online = True
        await self.s.reconcile(2)
        self.assertEqual(c.expiry, NOW+32*DAY)

    async def test_rejected_write_enters_review(self):
        await self.bind()
        r = await self.payment()
        self.panel.failure = "reject"
        await self.s.approve(2, ADMIN, r["rev"])
        self.assertEqual(self.db.request(2)["state"], "review")
        self.panel.failure = None
        await self.s.retry_review(2, ADMIN, self.db.request(2)["rev"])
        self.assertEqual(self.db.request(2)["state"], "done")

    async def test_unknown_email_correction_and_offline_registration(self):
        await self.s.register(USER, None, "typo")
        r = await self.s.refresh(1)
        self.assertIn("не найден", r["issue"])
        with self.assertRaises(UserNotice):
            await self.s.approve(1, ADMIN, r["rev"])
        self.s.decline(1, ADMIN, r["rev"], correction=True)
        self.assertEqual(self.db.row("SELECT entering FROM contacts WHERE user=?", USER)[0], 1)
        self.panel.online = False
        await self.s.register(USER, None, "client-a")
        self.assertEqual(self.db.request(2)["state"], "pending")
        self.panel.online = True
        r = await self.s.refresh(2)
        await self.s.approve(2, ADMIN, r["rev"])
        self.assertEqual(self.panel.client.owner, USER)

    async def test_special_states_and_unauthorized_approval(self):
        c = await self.bind()
        r = await self.payment()
        with self.assertRaises(UserNotice):
            await self.s.approve(2, USER, r["rev"])
        for expiry, enabled, used in [(0, True, 0), (-DAY, True, 0), (NOW+DAY, False, 0),
                                     (NOW-DAY, False, 1000)]:
            c.fields.update(expiryTime=expiry, enable=enabled)
            c.used = used
            r = await self.s.refresh(2)
            with self.assertRaises(UserNotice):
                await self.s.approve(2, ADMIN, r["rev"])
            with self.assertRaises(UserNotice):
                await self.s.approve(2, ADMIN, r["rev"], activate=True)
        self.assertEqual(self.panel.writes, 1)

    async def test_reminder_once_per_expiry_and_delivery_retry(self):
        c = await self.bind()
        for _ in range(2):
            await flush(self.s, AsyncMock())
        await self.s.reminders()
        await self.s.reminders()
        self.assertEqual(len(self.db.rows("SELECT id FROM outgoing WHERE kind='reminder'")), 1)
        bot = AsyncMock()
        bot.send_message.side_effect = OSError()
        await flush(self.s, bot)
        self.assertEqual(self.db.row("SELECT status FROM outgoing WHERE kind='reminder'")[0], "waiting")
        self.t += 6000
        bot.send_message.side_effect = None
        await flush(self.s, bot)
        self.assertEqual(self.db.row("SELECT status FROM outgoing WHERE kind='reminder'")[0], "sent")
        c.fields["expiryTime"] += DAY//2
        await self.s.reminders()
        self.assertEqual(len(self.db.rows("SELECT id FROM outgoing WHERE kind='reminder'")), 2)

    async def test_backup_contains_pending_decisions(self):
        await self.s.register(USER, None, "client-a")
        copy = Path(self.tmp.name)/"backup.db"
        backup(self.path, copy)
        b = Store(copy)
        try:
            self.assertEqual(b.request(1)["state"], "pending")
        finally:
            b.db.close()

    async def test_telegram_retry_after_pauses_outbox(self):
        with self.db.db:
            self.db.enqueue("a", USER, "text", body="first")
            self.db.enqueue("b", ADMIN, "text", body="second")
        bot = AsyncMock()
        bot.send_message.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=USER, text="first"), message="retry", retry_after=20)
        self.assertEqual(await flush(self.s, bot), 21)
        bot.send_message.assert_awaited_once()
        self.assertEqual(self.db.row("SELECT available FROM outgoing WHERE fingerprint='a'")[0], NOW+21000)

    async def test_telegram_entry_points(self):
        session = RecordingSession()
        bot = Bot("123456:local-demo-token", session=session)
        dp = dispatcher(self.s)
        counter = 0
        async def feed(uid, text=None, data=None):
            nonlocal counter
            counter += 1
            person = dict(id=uid, is_bot=False, first_name="Tester")
            msg = {"message_id":counter, "date":NOW//1000, "chat":{"id":uid,"type":"private"}, "from":person}
            if text:
                packet = {"message":dict(msg, text=text)}
            else:
                packet = {"callback_query":{"id":str(counter), "from":person, "message":dict(msg, text="menu"),
                                            "data":data, "chat_instance":"test"}}
            await dp.feed_update(bot, Update.model_validate(dict(update_id=counter, **packet)))
        await feed(USER, text="/start")
        await feed(USER, data="reg")
        await feed(USER, text="client-a")
        await flush(self.s, bot)
        r = self.db.request(1)
        await feed(USER, data=f"approve:1:{r['rev']}")
        self.assertEqual(self.panel.client.owner, 0)
        await feed(ADMIN, data=f"approve:1:{r['rev']}")
        await feed(USER, data="subs")
        await feed(USER, data=f"paid:7:{self.panel.client.expiry}")
        for _ in range(2):
            await flush(self.s, bot)
        await feed(ADMIN, data="queue:0")
        r = self.db.request(2)
        await feed(ADMIN, data=f"approve:2:{r['rev']}")
        await flush(self.s, bot)
        self.assertEqual(self.db.request(2)["state"], "done")
        self.assertTrue(any("Продление подтверждено" in str(getattr(call,"text","")) for call in session.calls))
        await bot.session.close()

