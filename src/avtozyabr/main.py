"""
Application entry point.

Starts:
  1. Postgres connection pool + migrations
  2. ZYClient HTTP session
  3. APScheduler with polling jobs
  4. aiogram Bot + Dispatcher (long polling)
"""

from __future__ import annotations

import asyncio
import signal
import sys

import sentry_sdk
import structlog
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from avtozyabr.bot.handlers import router, setup_handlers
from avtozyabr.config import get_settings
from avtozyabr.db import close_pool, init_pool
from avtozyabr.logging_setup import setup_logging
from avtozyabr.poller.client import ZYClient
from avtozyabr.scheduler.jobs import job_poll_stock, job_refresh_wishlist

log = structlog.get_logger(__name__)


async def run() -> None:
    cfg = get_settings()
    setup_logging(cfg.log_level)

    if cfg.sentry_dsn:
        sentry_sdk.init(dsn=cfg.sentry_dsn, traces_sample_rate=0.1)
        log.info("sentry.initialised")

    # ── DB ────────────────────────────────────────────────────────────────────
    pool = await init_pool(cfg.postgres_dsn)
    log.info("db.pool_ready")

    # ── ZY HTTP client ────────────────────────────────────────────────────────
    client = ZYClient(cfg.zy_base_url, cookies_file=cfg.zy_cookies_file)
    await client.start()

    auth_ok = await client.is_authenticated()
    if not auth_ok:
        log.warning(
            "zy_client.not_authenticated",
            hint="Send /setcookies <json> in Telegram to authenticate",
        )

    # ── Telegram bot ──────────────────────────────────────────────────────────
    bot = Bot(token=cfg.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    paused: list[bool] = [False]  # mutable flag shared with scheduler
    setup_handlers(pool, client, cfg.telegram_allowed_users, paused)

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        job_refresh_wishlist,
        trigger="interval",
        seconds=cfg.poll_interval_normal,
        kwargs={"client": client, "pool": pool, "paused": paused},
        id="refresh_wishlist",
        next_run_time=None,   # fire immediately on first tick
        misfire_grace_time=60,
    )

    scheduler.add_job(
        job_poll_stock,
        trigger="interval",
        seconds=cfg.poll_interval_hot,
        kwargs={
            "client": client,
            "pool": pool,
            "bot": bot,
            "allowed_users": cfg.telegram_allowed_users,
            "daily_spend_limit": cfg.daily_spend_limit,
            "paused": paused,
        },
        id="poll_stock",
        misfire_grace_time=30,
    )

    scheduler.start()
    log.info("scheduler.started", normal_interval=cfg.poll_interval_normal, hot_interval=cfg.poll_interval_hot)

    # Fire wishlist refresh immediately (don't wait for first interval tick)
    asyncio.create_task(
        job_refresh_wishlist(client=client, pool=pool, paused=paused)
    )

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    async def shutdown(sig: signal.Signals) -> None:
        log.info("shutdown.received", signal=sig.name)
        scheduler.shutdown(wait=False)
        await client.close()
        await close_pool()
        await bot.session.close()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s)))

    # ── Start polling ─────────────────────────────────────────────────────────
    log.info("bot.polling_started")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
