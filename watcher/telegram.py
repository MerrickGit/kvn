import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                               TelegramRetryAfter)
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .panel import DAY, PanelError
from .limits import Admission
from .store import now_ms
from .service import REACTIVATE, UserNotice

log = logging.getLogger(__name__)
STATES = {"pending": "ожидает решения", "applying": "изменение отправляется в панель",
          "review": "нужна ручная проверка", "done": "выполнена", "rejected": "отклонена",
          "correction": "запрошено исправление Email"}


def keyboard(*buttons):
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons])


def home(s, uid):
    buttons = [("Регистрация", "reg"), ("Моя подписка", "subs")]
    if uid == s.admin:
        buttons.append(("Ожидают решения", "queue:0"))
    return keyboard(*buttons)


def pay_button(client):
    return ("Я оплатил", f"paid:{client.id}:{client.expiry}")


async def card(s, rid, confirm=False):
    r = s.store.request(rid)
    if not r:
        return "Заявка не найдена.", None
    offline = False
    try:
        r = await s.refresh(rid)
    except PanelError:
        offline = True
    state, kind = r["state"], r["kind"]
    title = "Регистрация" if kind == "bind" else "Сообщение об оплате"
    who = f"@{r['username']} · " if r["username"] else ""
    lines = [f"№{rid} · {title} · {STATES[state]}", f"Email — {r['email']}",
             f"Telegram — {who}{r['user']}"]
    actions = []
    key = f"{rid}:{r['rev']}"
    if r["note"]:
        lines.append(r["note"])
    if state == "pending":
        if offline:
            lines.append("Панель недоступна. Проверьте соединение и обновите карточку.")
        elif r["issue"] == REACTIVATE:
            lines.extend([REACTIVATE, "Прежний срок — " + s.date(r["expiry"])])
            actions.append(("Включить и продлить на 30 дней", "activate:" + key))
        elif r["issue"]:
            lines.append(r["issue"])
        elif kind == "bind":
            if r["owner"] not in (0, r["user"]):
                lines.append(f"Смена Telegram ID с {r['owner']} на {r['user']}.")
                actions.append(("Подтверждаю замену владельца", "replace:" + key) if confirm
                               else ("Заменить привязку", "confirm:" + key))
            else:
                actions.append(("Подтвердить привязку", "approve:" + key))
        else:
            lines.append("Текущий срок — " + s.date(r["expiry"]))
            if r["expiry"] > s.clock():
                lines.append("После продления — " + s.date(r["expiry"] + 30 * DAY))
            else:
                lines.append("После продления — 30 дней с момента подтверждения.")
            actions.append(("Продлить на 30 дней", "approve:" + key))
        actions.append(("Обновить", f"card:{rid}"))
        if kind == "bind":
            actions.append(("Запросить исправление", "correct:" + key))
        actions.append(("Отклонить", "reject:" + key))
    elif state == "review":
        lines.append("После сверки повтор использует ранее сохранённую цель.")
        actions.extend([("Проверил, повторить сверку", "retry:" + key), ("Закрыть заявку", "reject:" + key)])
    elif state == "done" and kind == "renew":
        lines.append("Продлено до " + s.date(r["target"]))
    elif state == "applying":
        lines.append("Бот автоматически сверит результат после восстановления связи.")
        actions.append(("Обновить", f"card:{rid}"))
    return "\n".join(lines), keyboard(*actions)


async def edit_or_send(message, text, markup):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in exc.message.lower():
            return
        await message.answer(text, reply_markup=markup)


def dispatcher(s):
    router = Router()
    router.message.filter(F.chat.type == "private")

    @router.message(CommandStart())
    async def start(m: Message):
        s.store.contact(m.from_user.id)
        await m.answer("Управление подпиской. Выберите действие.", reply_markup=home(s, m.from_user.id))

    @router.message(Command("id"))
    async def user_id(m: Message):
        await m.answer(f"Ваш Telegram ID — {m.from_user.id}")

    @router.message(F.text)
    async def email(m: Message):
        uid = m.from_user.id
        contact = s.store.row("SELECT entering FROM contacts WHERE user=? AND expires>?", uid, now_ms())
        if not contact or not contact["entering"]:
            await m.answer("Выберите действие.", reply_markup=home(s, uid))
            return
        async with s.lock:
            try:
                result = await s.register(uid, m.from_user.username, m.text)
            except UserNotice as exc:
                result = str(exc)
        await m.answer(result, reply_markup=home(s, uid))

    @router.callback_query()
    async def click(q: CallbackQuery):
        uid, m = q.from_user.id, q.message
        if not isinstance(m, Message) or m.chat.type != "private" or m.chat.id != uid:
            await q.answer("Откройте личный чат с ботом.", show_alert=True)
            return
        args = (q.data or "").split(":")
        action = args[0]
        admin_actions = {"queue", "card", "approve", "activate", "confirm", "replace", "retry", "reject", "correct"}
        if action in admin_actions and uid != s.admin:
            await q.answer("Доступно администратору.", show_alert=True)
            return
        await q.answer()
        async with s.lock:
            try:
                if action == "reg":
                    s.store.contact(uid, entering=True)
                    await m.answer("Введите Email из карточки 3x-ui. Подойдёт и имя вида ivan-vpn.")
                elif action == "subs":
                    clients = await s.subscriptions(uid)
                    if not clients:
                        await m.answer("Привязанных подписок пока нет.", reply_markup=home(s, uid))
                    for c in clients:
                        details = f"{c.email}\nСрок — {s.date(c.expiry)}."
                        if c.exception:
                            details += "\n" + c.exception
                        await m.answer(details, reply_markup=keyboard(pay_button(c)))
                elif action == "paid":
                    result = await s.paid(uid, q.from_user.username, int(args[1]), int(args[2]))
                    await m.answer(result, reply_markup=home(s, uid))
                elif action == "queue":
                    offset = max(0, int(args[1]))
                    kind = args[2] if len(args) > 2 and args[2] in {"bind", "renew"} else ""
                    rows = s.store.rows("""SELECT id,kind,email,state FROM requests
                        WHERE state IN ('pending','applying','review') AND (?='' OR kind=?)
                        ORDER BY id LIMIT 11 OFFSET ?""", kind, kind, offset)
                    items = [(f"№{r['id']} · {r['email']} · {STATES[r['state']]}", f"card:{r['id']}")
                             for r in rows[:10]]
                    if offset:
                        items.append(("Назад", f"queue:{max(0, offset - 10)}:{kind}"))
                    if len(rows) > 10:
                        items.append(("Дальше", f"queue:{offset + 10}:{kind}"))
                    items.extend([("Регистрации", "queue:0:bind"), ("Продления", "queue:0:renew")])
                    await m.answer("Заявки на рассмотрении" if rows else "Очередь пуста.",
                                   reply_markup=keyboard(*items))
                elif action in admin_actions:
                    rid = int(args[1])
                    tip = None
                    if action not in {"card", "confirm"}:
                        rev = int(args[2])
                        try:
                            if action in ("approve", "replace", "activate"):
                                await s.approve(rid, uid, rev, replace=action == "replace", activate=action == "activate")
                            elif action == "retry":
                                await s.retry_review(rid, uid, rev)
                            else:
                                s.decline(rid, uid, rev, correction=action == "correct")
                        except (UserNotice, PanelError) as exc:
                            tip = str(exc)
                    body, markup = await card(s, rid, confirm=action == "confirm")
                    if tip:
                        body = tip + "\n\n" + body
                    # Queue buttons share one message; opening a request keeps the list intact.
                    if action == "card":
                        await m.answer(body, reply_markup=markup)
                    else:
                        await edit_or_send(m, body, markup)
                else:
                    raise UserNotice("Откройте меню командой /start.")
            except (UserNotice, PanelError) as exc:
                await m.answer(str(exc), reply_markup=home(s, uid))
            except (ValueError, IndexError):
                await m.answer("Кнопка устарела. Откройте меню командой /start.")

    dp = Dispatcher(disable_fsm=True)  # The registration prompt lives in SQLite.
    dp.update.outer_middleware(Admission(s.admin, s.limits))
    dp.include_router(router)
    return dp


async def delivery(s, item):
    kind, uid = item["kind"], item["chat"]
    if kind == "card":
        return await card(s, item["request_id"])
    if kind == "text":
        return item["body"], home(s, uid)
    clients = await s.subscriptions(uid)
    c = next((c for c in clients if c.id == item["client_id"]), None)
    if not c:
        return None  # Ownership changed before delivery.
    if kind == "reminder":
        if not c.fields["enable"] or c.expiry != item["expiry"] or not 0 < c.expiry - s.clock() <= s.days * DAY:
            return None
        return f"Подписка {c.email} заканчивается {s.date(c.expiry)}.", keyboard(pay_button(c))
    r = s.store.request(item["request_id"])
    title = "Привязка подтверждена" if r["kind"] == "bind" else "Продление подтверждено"
    return f"{title}. Заявка №{r['id']}.\n{c.email}\nТекущий срок — {s.date(c.expiry)}.", home(s, uid)


async def flush(s, bot: Bot):
    batch = s.store.rows("""SELECT * FROM outgoing WHERE id IN (
        SELECT min(id) AS first_id FROM outgoing WHERE status='waiting' AND available<=?
        GROUP BY chat ORDER BY first_id LIMIT 30) ORDER BY id""", s.clock())
    for item in batch:
        status, delay = "sent", 0
        telegram_pause = 0
        async with s.lock:
            try:
                payload = await delivery(s, item)
                if payload:
                    await bot.send_message(item["chat"], payload[0], reply_markup=payload[1])
                else:
                    status = "obsolete"
            except TelegramRetryAfter as exc:
                status, delay = "waiting", exc.retry_after + 1
                telegram_pause = delay
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                status = "undeliverable"
                log.warning("Telegram delivery %d failed (%s)", item["id"], type(exc).__name__)
            except (PanelError, TelegramAPIError, OSError, asyncio.TimeoutError):
                status, delay = "waiting", min(1800, 5 * 2 ** min(9, item["attempts"]))
            with s.store.db as db:
                db.execute("UPDATE outgoing SET status=?,available=?,attempts=attempts+1 WHERE id=?",
                           (status, s.clock() + delay * 1000, item["id"]))
        if telegram_pause:
            return telegram_pause
    return 0


async def worker(s, bot):
    next_reminders = 0
    next_delivery = 0
    while True:
        try:
            for r in s.store.rows("SELECT id FROM requests WHERE state='applying' ORDER BY id"):
                try:
                    async with s.lock:
                        await s.reconcile(r["id"])
                except PanelError:
                    log.warning("Panel verification deferred for request %s", r["id"])
            if time.monotonic() >= next_delivery:
                pause = await flush(s, bot)
                next_delivery = time.monotonic() + pause
            if time.monotonic() >= next_reminders:
                try:
                    await s.restore_bindings_if_needed()
                    await s.reminders()
                    next_reminders = time.monotonic() + 3600
                except PanelError as exc:
                    next_reminders = time.monotonic() + 60
                    log.warning("Binding restore or reminder scan deferred: %s", exc)
        except Exception as exc:
            log.error("Background operation failed (%s)", type(exc).__name__)
        await asyncio.sleep(5)

