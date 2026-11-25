import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiogram.types import Update

from watcher.limits import Admission, Limits
from watcher.service import Service, UserNotice
from watcher.store import Store

from test_flow import FakePanel, NOW, USER, ADMIN


def message(uid):
    return Update.model_validate({"update_id":1, "message":{"message_id":1, "date":NOW//1000,
        "chat":{"id":uid, "type":"private"}, "from":{"id":uid,"first_name":"Test","is_bot":False},
        "text":"/start"}})


class LimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_refill_and_independent_public_budget(self):
        t = [0.]
        gate = Admission(ADMIN, Limits(user_per_minute=1, public_per_minute=2), clock=lambda:t[0])
        self.assertTrue(gate.admit(USER))
        self.assertFalse(gate.admit(USER))
        self.assertTrue(gate.admit(USER+1))
        self.assertFalse(gate.admit(USER+2))
        t[0] += 60
        self.assertTrue(gate.admit(USER))

    async def test_admin_admitted_when_public_capacity_is_busy(self):
        gate = Admission(ADMIN, Limits())
        gate.active.update([101,102,103])
        handler = AsyncMock()
        await gate(handler, message(USER), {})
        handler.assert_not_awaited()
        await gate(handler, message(ADMIN), {})
        handler.assert_awaited_once()
        self.assertEqual(gate.dropped, 1)

    async def test_public_capacity_released_on_error(self):
        gate = Admission(ADMIN, Limits())
        with self.assertRaises(RuntimeError):
            await gate(AsyncMock(side_effect=RuntimeError()), message(USER), {})
        self.assertEqual(gate.active, set())

    async def test_separate_queues_and_persistent_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"watcher.db"
            db = Store(path)
            panel = FakePanel()
            panel.get = AsyncMock(wraps=panel.get)
            limits = Limits(registrations=1, renewals=1)
            s = Service(db,panel,ADMIN,limits=limits)
            try:
                await s.register(USER,None,"client-a")
                self.assertIn("№1", await s.register(USER,None,"client-a"))
                with self.assertRaises(UserNotice):
                    await s.register(USER,None,"typo")
                with self.assertRaises(UserNotice):
                    await s.register(USER+1,None,"client-b")
                self.assertEqual(panel.get.await_count, 1)
                self.assertEqual(len(db.rows("SELECT id FROM outgoing")), 1)
                panel.client.fields["tgId"] = USER+2
                await s.subscriptions(USER+2)
                await s.paid(USER+2,None,panel.client.id,panel.client.expiry)
                self.assertEqual(db.request(2)["kind"], "renew")
                db.db.close()
                db = Store(path)
                s = Service(db,panel,ADMIN,limits=limits)
                with self.assertRaises(UserNotice):
                    await s.register(USER+1,None,"client-b")
                s.decline(1,ADMIN,db.request(1)["rev"])
                self.assertIn("№3", await s.register(USER+1,None,"client-b"))
            finally:
                db.db.close()

    async def test_pending_payments_per_owner(self):
        db = Store(":memory:")
        try:
            s = Service(db, FakePanel(), ADMIN, limits=Limits(renewals_per_user=1))
            self.assertEqual(s.create("renew", USER, None, "one", 12, NOW), 1)
            self.assertEqual(s.create("renew", USER, None, "one", 12, NOW), 1)
            with self.assertRaises(UserNotice):
                s.create("renew", USER, None, "two", 13, NOW)
            self.assertEqual(s.create("renew", USER+1, None, "three", 14, NOW), 2)
        finally:
            db.db.close()

    async def test_unlinked_visitors_do_not_trigger_background_reads(self):
        panel = FakePanel()
        panel.owned = AsyncMock(wraps=panel.owned)
        db = Store(":memory:")
        try:
            s = Service(db,panel,ADMIN)
            db.contact(USER, entering=True)
            db.contact(USER+1, entering=True)
            await s.reminders()
            panel.owned.assert_not_called()
        finally:
            db.db.close()

