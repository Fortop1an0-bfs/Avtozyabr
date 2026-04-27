"""
APScheduler job definitions.

Two job types:
  poll_wishlist   — refresh wishlist items from ZY (runs every POLL_INTERVAL_NORMAL)
  poll_stock      — check stock for each wishlist product; hot items use a shorter interval
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import asyncpg
import structlog

from avtozyabr.detector.stock import detect_stock_changes, get_active_wishlist_products
from avtozyabr.models import AutoOrderRule, WishlistItem
from avtozyabr.orchestrator.order import process_stock_event
from avtozyabr.poller.client import AuthError, ZYClient
from avtozyabr.poller.wishlist import fetch_and_store_stock, fetch_and_store_wishlist

if TYPE_CHECKING:
    from aiogram import Bot

log = structlog.get_logger(__name__)


async def job_refresh_wishlist(
    client: ZYClient,
    pool: asyncpg.Pool,
    paused: list[bool],
) -> None:
    if paused[0]:
        log.debug("scheduler.paused_skip", job="refresh_wishlist")
        return
    try:
        items = await fetch_and_store_wishlist(client, pool)
        log.info("scheduler.wishlist_refreshed", count=len(items))
    except AuthError:
        log.error("scheduler.auth_error", job="refresh_wishlist")
    except Exception as exc:
        log.error("scheduler.job_error", job="refresh_wishlist", error=str(exc))


async def job_poll_stock(
    client: ZYClient,
    pool: asyncpg.Pool,
    bot: "Bot",
    allowed_users: list[int],
    daily_spend_limit: float,
    paused: list[bool],
) -> None:
    if paused[0]:
        log.debug("scheduler.paused_skip", job="poll_stock")
        return

    try:
        async with pool.acquire() as conn:
            products = await get_active_wishlist_products(conn)
    except Exception as exc:
        log.error("scheduler.db_error", job="poll_stock", error=str(exc))
        return

    for product in products:
        item = WishlistItem(
            product_id=product["product_id"],
            name=product["name"],
            url=product["url"],
            image_url=product.get("image_url"),
        )
        rule: AutoOrderRule | None = None
        if product.get("rule_enabled") is not None:
            rule = AutoOrderRule(
                product_id=product["product_id"],
                enabled=bool(product["rule_enabled"]),
                max_price=product["max_price"],
                max_qty=product["max_qty"] or 1,
                require_confirm=bool(product["require_confirm"]) if product["require_confirm"] is not None else True,
            )

        try:
            stock = await fetch_and_store_stock(client, pool, item.product_id)
        except AuthError:
            log.error("scheduler.auth_error", product_id=item.product_id)
            break  # all requests will fail — stop the loop
        except Exception as exc:
            log.warning("scheduler.stock_fetch_error", product_id=item.product_id, error=str(exc))
            continue

        try:
            async with pool.acquire() as conn:
                events = await detect_stock_changes(conn, item, stock)
        except Exception as exc:
            log.error("scheduler.detect_error", product_id=item.product_id, error=str(exc))
            continue

        for event in events:
            from avtozyabr.bot.notifications import notify_stock_event

            async def _notify(ev, attempt_id, checkout_url, _bot=bot, _users=allowed_users):
                return await notify_stock_event(_bot, _users, ev, attempt_id, checkout_url)

            await process_stock_event(
                event=event,
                rule=rule,
                client=client,
                pool=pool,
                notify_fn=_notify,
                daily_spend_limit=daily_spend_limit,
            )
