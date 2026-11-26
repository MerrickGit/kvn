import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from watcher.panel import DAY, Panel, PanelError
from watcher.service import Service
from watcher.store import Store
from watcher.telegram import worker

from test_flow import ADMIN, NOW, USER


def record(cid, owner, expiry):
    return dict(id=cid, email=f"user-{cid}", tgId=owner, expiryTime=expiry, enable=True, totalGB=0)


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "bot.db"
        self.panel = Panel(None, "http://panel.test", "fixture")

    def open(self):
        db = Store(self.path)
        self.addCleanup(db.db.close)
        return Service(db, self.panel, ADMIN, clock=lambda: NOW)

    async def test_startup_recovers_all_bound_clients_before_first_reminders(self):
        rows = [record(1, USER, NOW + DAY), record(2, USER, NOW + 9 * DAY),
                record(3, USER + 1, NOW - DAY), record(4, 0, NOW + DAY),
                record(5, -100, NOW + DAY)]

        async def request(method, route):
            self.assertEqual(method, "GET")
            if route == "list":
                return rows
            owner = int(route.removeprefix("get/tgId/"))
            return [{"client": row} for row in rows if row["tgId"] == owner]

        self.panel._request = AsyncMock(side_effect=request)
        s = self.open()
        with patch("watcher.telegram.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await worker(s, AsyncMock())
        self.assertEqual([tuple(r) for r in s.store.rows("SELECT * FROM clients ORDER BY id")],
                         [(1, "user-1", USER), (2, "user-2", USER), (3, "user-3", USER + 1)])
        self.assertEqual([tuple(r) for r in s.store.rows("SELECT kind,chat,client_id FROM outgoing")],
                         [("reminder", USER, 1)])
        self.assertEqual(self.panel._request.await_args_list.count(call("GET", "list")), 1)
        s.store.db.close()
        self.panel._request.reset_mock()
        await self.open().restore_bindings_if_needed()
        self.panel._request.assert_not_awaited()

    async def test_failed_restore_retries_after_restart_even_with_a_local_binding(self):
        self.panel._request = AsyncMock(side_effect=[
            PanelError("offline"), [record(1, USER, NOW), {"id": 2, "email": "malformed"}],
            [record(1, USER, NOW), record(2, USER + 1, NOW)]])
        s = self.open()
        for _ in range(2):
            with self.assertRaises(PanelError):
                await s.restore_bindings_if_needed()
            self.assertFalse(s.store.row("SELECT 1 FROM clients"))
        # A menu lookup can populate one binding while the full restore is pending.
        with s.store.db:
            s.store.db.execute("INSERT INTO clients VALUES (1,'user-1',?)", (USER,))
        rid = s.create("bind", USER + 5, None, "other")
        s.store.db.close()
        s = self.open()
        await s.restore_bindings_if_needed()
        self.assertEqual(s.store.row("SELECT count(*) FROM clients")[0], 2)
        self.assertEqual(s.store.request(rid)["state"], "pending")
        await s.restore_bindings_if_needed()
        self.assertEqual(self.panel._request.await_count, 3)

    async def test_empty_panel_completes_without_hourly_list_calls(self):
        self.panel._request = AsyncMock(return_value=[])
        s = self.open()
        await s.restore_bindings_if_needed()
        await s.restore_bindings_if_needed()
        self.panel._request.assert_awaited_once_with("GET", "list")
        self.assertFalse(s.store.row("SELECT 1 FROM clients"))

    async def test_worker_retries_and_keeps_serving_previously_discovered_owners(self):
        rows = [record(1, USER, NOW + DAY), record(2, USER + 1, NOW + DAY)]
        clock = 0

        async def request(method, route):
            if route == "list":
                if clock == 0:
                    raise PanelError("offline")
                return rows
            owner = int(route.removeprefix("get/tgId/"))
            return [{"client": row} for row in rows if row["tgId"] == owner]

        s = self.open()
        self.panel._request = AsyncMock(side_effect=request)
        await s.subscriptions(USER)

        async def advance(_):
            nonlocal clock
            if clock:
                raise asyncio.CancelledError
            self.assertEqual([r["chat"] for r in s.store.rows("SELECT chat FROM outgoing")], [USER])
            clock = 60

        with patch("watcher.telegram.time", SimpleNamespace(monotonic=lambda: clock)), \
             patch("watcher.telegram.asyncio.sleep", side_effect=advance), \
             self.assertLogs("watcher.telegram", level="WARNING"), self.assertRaises(asyncio.CancelledError):
            await worker(s, AsyncMock())
        self.assertEqual([r["chat"] for r in s.store.rows("SELECT chat FROM outgoing ORDER BY chat")],
                         [USER, USER + 1])
        self.assertEqual(self.panel._request.await_args_list.count(call("GET", "list")), 2)

    async def test_upgrade_with_existing_bindings_skips_full_restore(self):
        db = sqlite3.connect(self.path)
        with db:
            db.execute("CREATE TABLE clients(id INTEGER PRIMARY KEY, email TEXT, owner INTEGER)")
            db.execute("INSERT INTO clients VALUES (1,'existing',?)", (USER,))
        db.close()
        self.panel._request = AsyncMock()
        s = self.open()
        await s.restore_bindings_if_needed()
        self.panel._request.assert_not_awaited()
        self.assertEqual(s.store.row("SELECT email FROM clients")[0], "existing")
