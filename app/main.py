"""Точка входа: web-сервер (OAuth) + Telegram-бот + планировщик в одном процессе."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import uvicorn

from .bot import build_dispatcher, configure_bot_menu
from .config import settings
from .db import Store
from .runtime import Runtime
from .scheduler import Scheduler
from .service import ContentService
from .web import create_app

log = logging.getLogger("ghostwriter")


def configure_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "apscheduler", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> int:
    configure_logging()
    store = Store()
    store.init()
    stalled = store.fail_stale_publishing()
    if stalled:
        log.warning("Помечено прерванных публикаций: %s", stalled)

    rt = Runtime(store)
    service = ContentService(store, rt)
    scheduler = Scheduler(service, rt)
    app = create_app(service, rt)

    admin_ids = settings.admin_ids
    if not admin_ids:
        log.warning(
            "TELEGRAM_ADMIN_IDS пуст — бот ответит любому пользователю его ID. "
            "Добавь TELEGRAM_ADMIN_IDS в .env и перезапусти."
        )
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN не задан — запускаю только web (OAuth/health).")

    config = uvicorn.Config(
        app, host=settings.web_host, port=settings.web_port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)

    tasks: list[asyncio.Task] = []
    bot_dp = None
    bot = None
    if settings.telegram_bot_token:
        bot_dp, notifier, bot = build_dispatcher(service, rt, scheduler, admin_ids)
        await configure_bot_menu(bot)
        scheduler.start()
        tasks.append(asyncio.create_task(bot_dp.start_polling(bot), name="bot"))
        log.info("Telegram-бот запущен. Расписание: %s", scheduler.describe())
    else:
        scheduler.start()

    tasks.append(asyncio.create_task(server.serve(), name="web"))
    log.info("Web: http://%s:%s/health", settings.web_host, settings.web_port)

    stop = asyncio.Event()

    def _handle_signal() -> None:
        log.info("Получен сигнал остановки")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _handle_signal)

    watchers = [asyncio.create_task(stop.wait(), name="stop")]
    try:
        done, pending = await asyncio.wait(tasks + watchers, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.get_name() == "stop":
                continue
            exc = task.exception()
            if exc:
                log.error("Компонент %s упал: %r", task.get_name(), exc)
                raise exc
            log.warning("Компонент %s завершился", task.get_name())
    finally:
        scheduler.shutdown()
        if bot_dp is not None:
            with contextlib.suppress(Exception):
                await bot_dp.stop_polling()
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.session.close()
        server.should_exit = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Остановлено")
    return 0


def run() -> None:
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    run()
