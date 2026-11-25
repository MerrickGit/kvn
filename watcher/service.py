import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .limits import Limits
from .panel import DAY, PanelError, WriteRejected, valid_email
from .store import now_ms

REACTIVATE = ("Срок истёк, доступ выключен. Панель также использует этот флаг при ручной "
              "блокировке. Подтвердите включение, если у клиента должен быть доступ.")


class UserNotice(Exception):
    """A short, expected response to a user action."""


class Service:
    def __init__(self, store, panel, admin, days=3, timezone="Europe/Moscow", clock=now_ms, limits=None):
        self.store, self.panel, self.admin = store, panel, admin
        self.days, self.zone, self.clock = days, ZoneInfo(timezone), clock
        # One bot instance. Serialize decisions, panel writes and delivery to a previous owner.
        self.lock = asyncio.Lock()
        self.limits = limits or Limits()

    def date(self, value):
        if value == 0:
            return "без ограничения срока"
        if value < 0:
            return f"{abs(value) // DAY} дн. с первого подключения"
        return datetime.fromtimestamp(value / 1000, self.zone).strftime("%d.%m.%Y %H:%M %Z")

    def require_admin(self, actor):
        if actor != self.admin:
            raise UserNotice("Действие доступно администратору.")

    async def restore_bindings_if_needed(self):
        async with self.lock:
            if not self.store.row("SELECT id FROM pending_binding_restore"):
                return
            bindings = await self.panel.list_bindings()
            # The marker survives failures, including a restart after a user has
            # independently rediscovered one of their subscriptions via the menu.
            with self.store.db as db:
                db.executemany("INSERT OR REPLACE INTO clients(id,email,owner) VALUES (?,?,?)", bindings)
                db.execute("DELETE FROM pending_binding_restore")
            logging.getLogger(__name__).info("Restored %d client bindings from 3x-ui", len(bindings))

    async def subscriptions(self, user):
        clients = await self.panel.owned(user)
        with self.store.db:
            self.store.db.execute("DELETE FROM clients WHERE owner=?", (user,))
            for client in clients:
                self.store.remember(client)
        return clients

    def check_queue(self, kind, uid, email):
        active = "state IN ('pending','applying','review')"
        mine = self.store.rows(f"SELECT id,email FROM requests WHERE kind=? AND user=? AND {active}",
                               kind, uid)
        for row in mine:
            if row["email"] == email:
                return row["id"]
        if kind == "bind" and mine:
            raise UserNotice(f"Ваша заявка на регистрацию №{mine[0]['id']} уже ожидает решения.")
        if kind == "renew" and len(mine) >= self.limits.renewals_per_user:
            raise UserNotice("У вас уже несколько заявок о платеже. Дождитесь решения администратора.")
        cap = self.limits.registrations if kind == "bind" else self.limits.renewals
        if self.store.row(f"SELECT count(*) FROM requests WHERE kind=? AND {active}", kind)[0] >= cap:
            raise UserNotice("Очередь заполнена. Новая заявка не создана. Попробуйте позже.")
        return None

    def create(self, kind, uid, username, email, client_id=None, expiry=None):
        with self.store.db as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.check_queue(kind, uid, email)
            if existing:
                return existing
            rid = db.execute("""INSERT INTO requests(kind,user,username,email,client_id,expiry,created)
                VALUES (?,?,?,?,?,?,?)""", (kind, uid, username, email, client_id, expiry, self.clock())).lastrowid
            self.store.enqueue(f"new:{rid}", self.admin, "card", rid=rid)
        return rid

    async def register(self, uid, username, email):
        email = email.strip()
        if not valid_email(email):
            raise UserNotice("Введите Email из карточки 3x-ui одним сообщением.")
        existing = self.check_queue("bind", uid, email)
        if existing:
            self.store.contact(uid)
            return f"Ваша заявка на регистрацию №{existing} уже ожидает решения."
        try:
            client = await self.panel.get(email)
        except PanelError:
            client = None  # The request remains available for a later panel lookup.
        if client and client.owner == uid:
            with self.store.db:
                self.store.remember(client)
            self.store.contact(uid)
            return f"Подписка уже привязана. Срок — {self.date(client.expiry)}."
        rid = self.create("bind", uid, username, email)
        self.store.contact(uid)
        return f"Заявка на регистрацию №{rid} отправлена администратору."

    async def paid(self, uid, username, cid, expected):
        cached = self.store.row("SELECT * FROM clients WHERE id=? AND owner=?", cid, uid)
        if not cached:
            raise UserNotice("Откройте «Моя подписка» для актуальной информации.")
        existing = self.check_queue("renew", uid, cached["email"])
        if existing:
            return f"Заявка о платеже №{existing} уже ожидает решения."
        try:
            c = await self.panel.get(cached["email"])
        except PanelError:
            c = None
        else:
            if not c or c.id != cid or c.owner != uid or c.expiry != expected:
                raise UserNotice("Данные подписки изменились. Откройте «Моя подписка».")
        if self.store.row("""SELECT id FROM requests WHERE kind='renew' AND client_id=?
                AND user=? AND expiry=? AND state='done'""", cid, uid, expected):
            raise UserNotice("Этот период уже продлён. Откройте «Моя подписка».")
        rid = self.create("renew", uid, username, cached["email"], cid, expected)
        return f"Заявка о платеже №{rid} передана администратору."

    def facts(self, r, c):
        if not c:
            return None, None, None, "Клиент с таким Email не найден."
        issue = ""
        if r["kind"] == "renew":
            if c.owner != r["user"] or c.id != r["client_id"]:
                issue = "Владелец или запись клиента изменились. Продление недоступно."
            else:
                issue = c.exception
                if not c.fields["enable"] and 0 < c.expiry <= self.clock() and not c.limit_exception:
                    issue = REACTIVATE
        return c.id, c.owner, c.expiry, issue

    async def refresh(self, rid):
        r = self.store.request(rid)
        if not r or r["state"] != "pending":
            return r
        c = await self.panel.get(r["email"])
        cid, owner, expiry, issue = self.facts(r, c)
        if r["kind"] == "renew":  # Never retarget a payment to a recreated client.
            cid = r["client_id"]
        snapshot = (cid, owner, expiry, issue)
        if snapshot != tuple(r[k] for k in ("client_id", "owner", "expiry", "issue")):
            with self.store.db as db:
                db.execute("""UPDATE requests SET client_id=?,owner=?,expiry=?,issue=?,rev=rev+1
                              WHERE id=?""", (*snapshot, rid))
        return self.store.request(rid)

    def notify_admin(self, r, reason):
        self.store.enqueue(f"review:{r['id']}:{r['rev']}", self.admin, "card", rid=r["id"], body=reason)

    def pending_again(self, r, reason):
        with self.store.db as db:
            db.execute("""UPDATE requests SET state='pending',rev=rev+1,target=NULL,note=?
                          WHERE id=?""", (reason, r["id"]))
            self.notify_admin(self.store.request(r["id"]), reason)

    async def approve(self, rid, actor, rev, replace=False, activate=False):
        self.require_admin(actor)
        online = True
        try:
            r = await self.refresh(rid)
        except PanelError:
            r = self.store.request(rid)
            online = False
        if not r or r["state"] != "pending" or r["rev"] != rev:
            raise UserNotice("Карточка изменилась. Проверьте обновлённые данные.")
        if r["issue"] is None:
            raise UserNotice("Сначала получите карточку клиента из панели.")
        if activate != (r["issue"] == REACTIVATE):
            raise UserNotice("Проверьте состояние доступа и действие на кнопке.")
        if r["issue"] and not activate:
            raise UserNotice(r["issue"])
        if r["kind"] == "bind":
            takeover = r["owner"] not in (0, r["user"])
            if takeover != replace:
                raise UserNotice("Замена владельца требует отдельного подтверждения.")
        other = self.store.row("""SELECT id FROM requests WHERE email=? AND id<>?
                                   AND state IN ('applying','review')""", r["email"], rid)
        if other:
            raise UserNotice(f"Сначала завершите заявку №{other['id']} по этому клиенту.")
        target = r["user"] if r["kind"] == "bind" else max(r["expiry"], self.clock()) + 30 * DAY
        with self.store.db as db:
            db.execute("""UPDATE requests SET state='applying',target=?,actor=?,decided=?,rev=rev+1
                          WHERE id=?""", (target, actor, self.clock(), rid))
        if online:
            await self.reconcile(rid)

    async def reconcile(self, rid):
        r = self.store.request(rid)
        if not r or r["state"] != "applying":
            return
        c = await self.panel.get(r["email"])
        binding = r["kind"] == "bind"
        if not c or c.id != r["client_id"] or (not binding and c.owner != r["user"]):
            return self.pending_again(r, "Изменился клиент или владелец. Проверьте данные.")
        field = "tgId" if binding else "expiryTime"
        previous = r["owner"] if binding else r["expiry"]
        actual = c.owner if binding else c.expiry
        activate = not binding and r["issue"] == REACTIVATE
        if actual != r["target"]:
            blocked = c.limit_exception if activate else c.exception
            if actual != previous or (not binding and blocked):
                return self.pending_again(r, "Настройки клиента изменились. Требуется новое решение.")
            try:
                await self.panel.change(c, field, r["target"], activate=activate)
            except WriteRejected as exc:
                with self.store.db as db:
                    db.execute("UPDATE requests SET state='review',note=?,rev=rev+1 WHERE id=?",
                               (str(exc), rid))
                    self.notify_admin(self.store.request(rid), str(exc))
                return
            c = await self.panel.get(r["email"])
            if (not c or c.id != r["client_id"] or c.owner != r["user"]
                    or (not binding and c.expiry != r["target"])):
                return self.pending_again(r, "Панель вернула другие данные после записи. Проверьте вручную.")
        if not binding and not c.fields["enable"]:
            with self.store.db as db:
                note = "Дата уже продлена, доступ выключен. Проверьте и при необходимости включите клиента в панели."
                db.execute("UPDATE requests SET state='review',note=?,rev=rev+1 WHERE id=?", (note, rid))
                self.notify_admin(self.store.request(rid), note)
            return
        with self.store.db as db:
            db.execute("UPDATE requests SET state='done',note=NULL WHERE id=?", (rid,))
            self.store.remember(c)
            self.store.enqueue(f"result:{rid}", r["user"], "success", rid=rid, cid=c.id)
            self.store.enqueue(f"done:{rid}", self.admin, "card", rid=rid)

    async def retry_review(self, rid, actor, rev):
        self.require_admin(actor)
        with self.store.db as db:
            changed = db.execute("""UPDATE requests SET state='applying',rev=rev+1,actor=?
                                    WHERE id=? AND state='review' AND rev=?""", (actor, rid, rev)).rowcount
        if not changed:
            raise UserNotice("Заявка уже изменилась.")
        await self.reconcile(rid)

    def decline(self, rid, actor, rev, correction=False):
        self.require_admin(actor)
        r = self.store.request(rid)
        if not r or r["rev"] != rev or r["state"] not in ("pending", "review"):
            raise UserNotice("Заявка уже изменилась.")
        if correction and r["kind"] != "bind":
            raise UserNotice("Исправление доступно для регистрации.")
        state = "correction" if correction else "rejected"
        topic = "на регистрацию" if r["kind"] == "bind" else "о платеже"
        body = (f"Проверьте Email для заявки №{rid} и пришлите его ещё раз."
                if correction else f"Администратор отклонил заявку {topic} №{rid}.")
        with self.store.db as db:
            db.execute("UPDATE requests SET state=?,rev=rev+1,actor=?,decided=? WHERE id=?",
                       (state, actor, self.clock(), rid))
            if correction:
                db.execute("INSERT OR REPLACE INTO contacts(user,entering,expires) VALUES (?,1,?)",
                           (r["user"], now_ms() + DAY))
            self.store.enqueue(f"result:{rid}", r["user"], "text", body=body)

    async def reminders(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM contacts WHERE expires<=?", (now_ms(),))
        # Only approved or previously verified owners generate background panel traffic.
        for row in self.store.rows("SELECT DISTINCT owner FROM clients WHERE owner>0"):
            uid = row["owner"]
            async with self.lock:
                for c in await self.subscriptions(uid):
                    if c.fields["enable"] and 0 < c.expiry - self.clock() <= self.days * DAY:
                        with self.store.db:
                            self.store.enqueue(f"remind:{uid}:{c.id}:{c.expiry}", uid, "reminder",
                                               cid=c.id, expiry=c.expiry)

