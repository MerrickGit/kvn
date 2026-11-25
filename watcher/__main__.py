import argparse
import asyncio
import logging
import os
import sqlite3
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from .limits import Limits
from .panel import Panel
from .service import Service
from .store import Store
from .telegram import dispatcher, worker


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Заполните {name} в настройках.")
    return value


async def main(db_path):
    url, token = required("PANEL_URL"), required("PANEL_TOKEN")
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("Проверьте PANEL_URL. Укажите полный адрес панели с базовым путём.")
    admin = int(required("ADMIN_ID"))
    days = int(os.getenv("REMINDER_DAYS", "3"))
    if admin <= 0 or days < 1:
        raise ValueError("ADMIN_ID и REMINDER_DAYS должны быть положительными.")
    limits = Limits(*(int(os.getenv(name, default)) for name, default in (
        ("USER_ACTIONS_PER_MINUTE", 12), ("PUBLIC_ACTIONS_PER_MINUTE", 60),
        ("MAX_OPEN_REGISTRATIONS", 50), ("MAX_OPEN_RENEWALS", 100),
        ("MAX_USER_RENEWALS", 3))))
    bot = Bot(required("BOT_TOKEN"), session=AiohttpSession(timeout=15))
    store = Store(db_path)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            s = Service(store, Panel(session, url, token), admin, days, os.getenv("TIME_ZONE", "Europe/Moscow"),
                        limits=limits)
            dp = dispatcher(s)
            await bot.delete_webhook(drop_pending_updates=False)
            job = asyncio.create_task(worker(s, bot))
            try:
                await dp.start_polling(bot, handle_as_tasks=True, tasks_concurrency_limit=8,
                                       tasks_shutdown_timeout=30, close_bot_session=False)
            finally:
                job.cancel()
                with suppress(asyncio.CancelledError):
                    await job
    finally:
        store.db.close()
        await bot.session.close()


def backup(source, dest):
    target = Path(dest)
    if not Path(source).is_file():
        raise ValueError("База данных ещё не создана.")
    with target.open("xb"):
        pass
    src = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
    out = sqlite3.connect(target)
    try:
        src.backup(out)
        if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Проверка резервной копии не пройдена.")
    finally:
        out.close()
        src.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", metavar="FILE", help="Создать проверенную копию SQLite")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db_path = os.getenv("DB_PATH", "data/watcher.sqlite3")
    try:
        if args.backup:
            backup(db_path, args.backup)
        else:
            asyncio.run(main(db_path))
    except (ValueError, FileExistsError) as exc:
        parser.exit(2, str(exc) + "\n")

