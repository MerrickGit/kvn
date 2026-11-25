import sqlite3
import time
from pathlib import Path


def now_ms():
    return time.time_ns() // 1_000_000


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS contacts(user INTEGER PRIMARY KEY, entering INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY, email TEXT, owner INTEGER);
            CREATE INDEX IF NOT EXISTS client_owners ON clients(owner);
            CREATE TABLE IF NOT EXISTS pending_binding_restore(id INTEGER PRIMARY KEY CHECK(id=1));
            INSERT OR IGNORE INTO pending_binding_restore(id)
                SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM clients WHERE owner>0);
            CREATE TABLE IF NOT EXISTS requests(
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, user INTEGER NOT NULL, username TEXT,
                email TEXT NOT NULL, client_id INTEGER, owner INTEGER, expiry INTEGER, issue TEXT,
                state TEXT NOT NULL DEFAULT 'pending', rev INTEGER NOT NULL DEFAULT 0,
                target INTEGER, actor INTEGER, created INTEGER NOT NULL, decided INTEGER, note TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS open_request ON requests(kind,user,email)
                WHERE state IN ('pending','applying','review');
            CREATE TABLE IF NOT EXISTS outgoing(
                id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, chat INTEGER NOT NULL,
                kind TEXT NOT NULL, request_id INTEGER, client_id INTEGER, expiry INTEGER,
                body TEXT, status TEXT DEFAULT 'waiting', attempts INTEGER DEFAULT 0,
                available INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS ready ON outgoing(status,available,id);
        """)
        if "expires" not in {r["name"] for r in self.rows("PRAGMA table_info(contacts)")}:
            with self.db:
                self.db.execute("ALTER TABLE contacts ADD COLUMN expires INTEGER DEFAULT 0")
                self.db.execute("UPDATE contacts SET expires=? WHERE entering=1", (now_ms()+86_400_000,))

    def row(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def rows(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def request(self, rid):
        return self.row("SELECT * FROM requests WHERE id=?", rid)

    def contact(self, uid, entering=False):
        with self.db:
            if entering:
                self.db.execute("INSERT OR REPLACE INTO contacts(user,entering,expires) VALUES (?,1,?)",
                                (uid, now_ms() + 86_400_000))
            else:
                self.db.execute("DELETE FROM contacts WHERE user=?", (uid,))

    def remember(self, client):
        self.db.execute("INSERT OR REPLACE INTO clients VALUES (?,?,?)",
                        (client.id, client.email, client.owner))

    def enqueue(self, fingerprint, chat, kind, *, rid=None, cid=None, expiry=None, body=None):
        self.db.execute("""INSERT OR IGNORE INTO outgoing
            (fingerprint,chat,kind,request_id,client_id,expiry,body) VALUES (?,?,?,?,?,?,?)""",
                        (fingerprint, chat, kind, rid, cid, expiry, body))

