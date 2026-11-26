import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Update

from test_flow import ADMIN, NOW, USER, FakePanel, RecordingSession
from watcher.panel import Client, DAY
from watcher.service import Service
from watcher.store import Store
from watcher.telegram import dispatcher, flush


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.panel = FakePanel()
        self.session = RecordingSession()
        self.bot = Bot("123456:local-test", session=self.session)
        self.addAsyncCleanup(self.bot.session.close)
        self.seq = 0
        self.reopen()

    def reopen(self):
        self.store = Store(Path(self.temp.name) / "bot.db")
        self.addCleanup(self.store.db.close)
        self.service = Service(self.store, self.panel, ADMIN, clock=lambda: NOW)
        self.dp = dispatcher(self.service)

    def packet(self, uid, *, text=None, data=None, message_id=99):
        self.seq += 1
        user = dict(id=uid, is_bot=False, first_name="Test")
        msg = {"message_id": message_id, "date": NOW // 1000, "chat": {"id": uid, "type": "private"}}
        if text is not None:
            body = {"message": {**msg, "from": user, "text": text}}
        else:
            body = {"callback_query": {"id": str(self.seq), "from": user, "chat_instance": "test",
                                       "message": dict(msg, text="menu"), "data": data}}
        return Update.model_validate(dict(update_id=self.seq, **body))

    def replies(self, start):
        return [c for c in self.session.calls[start:] if isinstance(c, (SendMessage, EditMessageText))]

    async def step(self, uid, **kwargs):
        update = self.packet(uid, **kwargs)
        start = len(self.session.calls)
        await self.dp.feed_update(self.bot, update)
        after = len(self.session.calls)
        await self.dp.feed_update(self.bot, update)
        self.assertEqual(len(self.session.calls), after)
        return self.replies(start)

    async def drain(self):
        start = len(self.session.calls)
        for _ in range(5):
            await flush(self.service, self.bot)
        return self.replies(start)

    async def test_admin_uses_own_subscription_and_queue(self):
        for text in ("/start", "/id", "unknown"):
            self.assertEqual(len(await self.step(ADMIN, text=text)), 1)
        self.assertEqual(len(await self.step(ADMIN, data="reg")), 1)
        self.assertEqual(await self.step(ADMIN, text="client-a"), [])
        pending, = await self.drain()
        self.assertIn("ожидает решения", pending.text)
        for data in ("queue:0", "queue:0:bind", "queue:0:renew", "home"):
            view, = await self.step(ADMIN, data=data)
            self.assertIsInstance(view, EditMessageText)
        opened, = await self.step(ADMIN, data="open:1")
        self.assertIsInstance(opened, SendMessage)
        refreshed, = await self.step(ADMIN, data="card:1")
        self.assertIsInstance(refreshed, EditMessageText)
        rev = self.store.request(1)["rev"]
        approved, = await self.step(ADMIN, data=f"approve:1:{rev}")
        self.assertIn("Текущий срок", approved.text)
        self.assertEqual(await self.drain(), [])
        subscription, = await self.step(ADMIN, data="subs")
        paid = subscription.reply_markup.inline_keyboard[0][0].callback_data
        self.assertEqual(await self.step(ADMIN, data=paid), [])
        payment, = await self.drain()
        self.assertIn("Сообщение об оплате", payment.text)
        rev = self.store.request(2)["rev"]
        renewal, = await self.step(ADMIN, data=f"approve:2:{rev}")
        self.assertIn("Продлено до", renewal.text)
        self.assertEqual(await self.drain(), [])
        self.assertEqual(self.panel.client.expiry, NOW + 32 * DAY)

    async def test_paging_preserves_distinct_clients_and_one_reply(self):
        first = self.panel.client
        second = Client(dict(first.fields, id=8, email="client-b"))
        self.panel.owned = AsyncMock(return_value=[second, first, first])
        for data, selected, page in (("subs", first, 1), ("subs:1", second, 2), ("subs:0", first, 1)):
            reply, = await self.step(ADMIN, data=data)
            self.assertIsInstance(reply, EditMessageText)
            self.assertEqual(reply.message_id, 99)
            self.assertIn(f"{page} из 2", reply.text)
            self.assertIn(selected.email, reply.text)
            self.assertEqual(reply.reply_markup.inline_keyboard[0][0].callback_data,
                             f"paid:{selected.id}:{selected.expiry}")
        self.panel.owned.return_value = []
        empty, = await self.step(ADMIN, data="subs:1")
        self.assertIn("пока нет", empty.text)

    async def test_separate_chats_and_recovery_edit_the_original_admin_card(self):
        await self.step(USER, data="reg")
        ack, = await self.step(USER, text="client-a")
        self.assertEqual(ack.chat_id, USER)
        pending, = await self.drain()
        self.assertEqual(pending.chat_id, ADMIN)
        rev = self.store.request(1)["rev"]
        self.panel.failure = "after"
        applying, = await self.step(ADMIN, data=f"approve:1:{rev}", message_id=42)
        self.assertIn("автоматически", applying.text)
        self.store.db.close()
        self.reopen()
        self.panel.failure = None
        await self.service.reconcile(1)
        sent = await self.drain()
        client, = [c for c in sent if c.chat_id == USER]
        admin, = [c for c in sent if c.chat_id == ADMIN]
        self.assertIsInstance(client, SendMessage)
        self.assertIn("Привязка подтверждена", client.text)
        self.assertIsInstance(admin, EditMessageText)
        self.assertEqual(admin.message_id, 42)
        self.assertEqual(self.panel.writes, 1)

    async def test_reject_correction_review_replace_and_reactivate(self):
        for rid, decision in ((1, "correct"), (2, "reject")):
            await self.service.register(ADMIN, None, "client-a")
            await self.drain()
            rev = self.store.request(rid)["rev"]
            reply, = await self.step(ADMIN, data=f"{decision}:{rid}:{rev}")
            self.assertIsInstance(reply, EditMessageText)
            self.assertEqual(await self.drain(), [])
        self.panel.client.fields["tgId"] = USER
        await self.service.register(ADMIN, None, "client-a")
        await self.drain()
        rev = self.store.request(3)["rev"]
        confirm, = await self.step(ADMIN, data=f"confirm:3:{rev}")
        self.assertIn("replace:", confirm.reply_markup.inline_keyboard[0][0].callback_data)
        self.panel.failure = "reject"
        review, = await self.step(ADMIN, data=f"replace:3:{rev}")
        self.assertIn("нужна ручная проверка", review.text)
        self.assertEqual(await self.drain(), [])
        self.panel.failure = None
        rev = self.store.request(3)["rev"]
        done, = await self.step(ADMIN, data=f"retry:3:{rev}")
        self.assertIn("выполнена", done.text)
        self.panel.client.fields.update(enable=False, expiryTime=NOW - DAY)
        await self.service.paid(ADMIN, None, self.panel.client.id, NOW - DAY)
        await self.drain()
        rev = self.store.request(4)["rev"]
        done, = await self.step(ADMIN, data=f"activate:4:{rev}")
        self.assertIn("Продлено до", done.text)
        self.assertEqual(await self.drain(), [])

    async def test_concurrent_replay_and_restart(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def owned(uid):
            entered.set()
            await release.wait()
            return []

        self.panel.owned = owned
        update = self.packet(ADMIN, data="subs")
        first = asyncio.create_task(self.dp.feed_update(self.bot, update))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await self.dp.feed_update(self.bot, update)
        finally:
            release.set()
            await first
        self.store.db.close()
        self.reopen()
        await self.dp.feed_update(self.bot, update)
        self.assertEqual(len(self.replies(0)), 1)
        self.assertEqual(len(await self.step(ADMIN, text="/id")), 1)

    async def test_unmodified_edit_sends_no_fallback_and_failed_command_can_retry(self):
        original = self.session.make_request

        async def unchanged(bot, method, timeout=None):
            if isinstance(method, EditMessageText):
                raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
            return await original(bot, method, timeout)

        with patch.object(self.session, "make_request", side_effect=unchanged):
            self.assertEqual(await self.step(ADMIN, data="subs"), [])
        update = self.packet(ADMIN, text="/id")
        with patch.object(self.session, "make_request", side_effect=OSError("connection closed")):
            with self.assertRaises(OSError):
                await self.dp.feed_update(self.bot, update)
        start = len(self.session.calls)
        await self.dp.feed_update(self.bot, update)
        self.assertEqual(len(self.replies(start)), 1)

    async def test_separate_user_and_admin_each_see_one_result(self):
        for action in ("correct", "reject", "approve"):
            await self.service.register(USER, None, "client-a")
            pending, = await self.drain()
            rid = self.store.row("SELECT max(id) FROM requests")[0]
            rev = self.store.request(rid)["rev"]
            decision, = await self.step(ADMIN, data=f"{action}:{rid}:{rev}")
            self.assertIsInstance(decision, EditMessageText)
            self.assertEqual(decision.chat_id, pending.chat_id)
            result, = await self.drain()
            self.assertIsInstance(result, SendMessage)
            self.assertEqual(result.chat_id, USER)

    async def test_upgrade_discards_stale_notifications_for_same_chat(self):
        await self.service.register(ADMIN, None, "client-a")
        r = await self.service.refresh(1)
        await self.service.approve(1, ADMIN, r["rev"])
        with self.store.db as db:
            db.execute("ALTER TABLE requests DROP COLUMN admin_message_id")
            db.execute("DROP TABLE handled_updates")
            self.store.enqueue("result:1", ADMIN, "success", rid=1, cid=self.panel.client.id)
        self.store.db.close()
        self.reopen()
        result, = await self.drain()
        self.assertIsInstance(result, SendMessage)
        self.assertIn("выполнена", result.text)
        self.assertEqual(await self.drain(), [])
