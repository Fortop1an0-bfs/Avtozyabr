"""
Order Orchestrator — state machine for the full purchase flow.

Flow:
  pending → cart_added → awaiting_confirm → confirmed → paid
                                          ↘ cancelled
  Any step can → failed

The orchestrator:
  1. Checks safety rules (daily spend limit, max_price, already ordered today).
  2. Adds item to cart via ZYClient.
  3. Sends a Telegram confirmation message with inline buttons.
  4. On confirm: sends user directly to checkout URL.
  5. On reject: removes from cart, marks cancelled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import structlog

from avtozyabr.models import AutoOrderRule, OrderStatus, StockEvent
from avtozyabr.poller.client import ZYClient

log = structlog.get_logger(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _already_ordered_today(
    conn: asyncpg.Connection, product_id: int, variant_id: int
) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1 FROM order_attempts
        WHERE product_id = $1
          AND variant_id = $2
          AND order_date = CURRENT_DATE
          AND status NOT IN ('failed', 'cancelled')
        LIMIT 1
        """,
        product_id, variant_id,
    )
    return row is not None


async def _daily_spend_today(conn: asyncpg.Connection) -> Decimal:
    result = await conn.fetchval(
        """
        SELECT COALESCE(SUM(price_at_order), 0)
        FROM order_attempts
        WHERE order_date = CURRENT_DATE
          AND status IN ('confirmed', 'paid')
        """
    )
    return Decimal(str(result))


async def _create_attempt(
    conn: asyncpg.Connection,
    product_id: int,
    variant_id: int,
    price: Decimal | None,
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO order_attempts
            (product_id, variant_id, triggered_at, order_date, status, price_at_order)
        VALUES ($1, $2, now(), CURRENT_DATE, 'pending', $3)
        ON CONFLICT (product_id, variant_id, order_date)
        DO NOTHING
        RETURNING id
        """,
        product_id, variant_id, price,
    )
    return row["id"] if row else -1


async def update_attempt_status(
    conn: asyncpg.Connection,
    attempt_id: int,
    status: OrderStatus,
    *,
    telegram_msg_id: int | None = None,
    external_order_id: str | None = None,
    error: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE order_attempts
        SET status = $2,
            telegram_msg_id = COALESCE($3, telegram_msg_id),
            external_order_id = COALESCE($4, external_order_id),
            error = COALESCE($5, error),
            updated_at = now()
        WHERE id = $1
        """,
        attempt_id, str(status), telegram_msg_id, external_order_id, error,
    )


async def get_attempt_by_msg(
    conn: asyncpg.Connection, telegram_msg_id: int
) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM order_attempts WHERE telegram_msg_id = $1",
        telegram_msg_id,
    )
    return dict(row) if row else None


# ── Main orchestration logic ───────────────────────────────────────────────────

async def process_stock_event(
    event: StockEvent,
    rule: AutoOrderRule | None,
    client: ZYClient,
    pool: asyncpg.Pool,
    notify_fn,            # async (event, attempt_id, checkout_url) -> telegram_msg_id
    daily_spend_limit: float = 0.0,
) -> None:
    """
    Entry point called by the scheduler when a StockEvent is detected.

    notify_fn is injected so the orchestrator stays free of Telegram imports.
    Signature: async (event: StockEvent, attempt_id: int, checkout_url: str) -> int | None
    """
    variant = event.variant

    async with pool.acquire() as conn:
        # ── Safety checks ──────────────────────────────────────────────────────
        if await _already_ordered_today(conn, event.product_id, variant.variant_id):
            log.info("orchestrator.skip.already_ordered_today", product_id=event.product_id)
            return

        if rule and not rule.enabled:
            log.info("orchestrator.skip.rule_disabled", product_id=event.product_id)
            # Still notify — just don't auto-cart
            await notify_fn(event, None, await client.get_checkout_url())
            return

        if rule and rule.max_price and variant.price and variant.price > rule.max_price:
            log.info(
                "orchestrator.skip.price_too_high",
                product_id=event.product_id,
                price=str(variant.price),
                max_price=str(rule.max_price),
            )
            await notify_fn(event, None, await client.get_checkout_url())
            return

        if daily_spend_limit > 0:
            spent = await _daily_spend_today(conn)
            price = variant.price or Decimal("0")
            if spent + price > Decimal(str(daily_spend_limit)):
                log.warning(
                    "orchestrator.skip.daily_limit_reached",
                    spent=str(spent),
                    limit=daily_spend_limit,
                )
                return

        attempt_id = await _create_attempt(conn, event.product_id, variant.variant_id, variant.price)
        if attempt_id == -1:
            log.info("orchestrator.skip.conflict", product_id=event.product_id)
            return

    # ── Add to cart ────────────────────────────────────────────────────────────
    try:
        await client.add_to_cart(variant.variant_id, qty=rule.max_qty if rule else 1)
        async with pool.acquire() as conn:
            await update_attempt_status(conn, attempt_id, OrderStatus.CART_ADDED)
        log.info("orchestrator.cart_added", attempt_id=attempt_id, variant_id=variant.variant_id)
    except Exception as exc:
        async with pool.acquire() as conn:
            await update_attempt_status(conn, attempt_id, OrderStatus.FAILED, error=str(exc))
        log.error("orchestrator.cart_failed", attempt_id=attempt_id, error=str(exc))
        return

    # ── Notify user ───────────────────────────────────────────────────────────
    checkout_url = await client.get_checkout_url()
    try:
        msg_id = await notify_fn(event, attempt_id, checkout_url)
        if msg_id:
            async with pool.acquire() as conn:
                await update_attempt_status(
                    conn, attempt_id, OrderStatus.AWAITING_CONFIRM,
                    telegram_msg_id=msg_id,
                )
    except Exception as exc:
        log.error("orchestrator.notify_failed", attempt_id=attempt_id, error=str(exc))


async def handle_user_confirm(
    attempt_id: int, pool: asyncpg.Pool
) -> str | None:
    """Called when user presses Confirm in Telegram. Returns checkout URL."""
    async with pool.acquire() as conn:
        await update_attempt_status(conn, attempt_id, OrderStatus.CONFIRMED)
        row = await conn.fetchrow(
            "SELECT product_id FROM order_attempts WHERE id = $1", attempt_id
        )
    return None  # Checkout URL returned by caller from client.get_checkout_url()


async def handle_user_cancel(attempt_id: int, pool: asyncpg.Pool) -> None:
    """Called when user presses Cancel in Telegram."""
    async with pool.acquire() as conn:
        await update_attempt_status(conn, attempt_id, OrderStatus.CANCELLED)
    log.info("orchestrator.user_cancelled", attempt_id=attempt_id)
