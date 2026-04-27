"""
Stock change detector.

Compares current ProductStock against the previous DB snapshot and emits
StockEvent for each variant that just became in_stock.
"""

from __future__ import annotations

import asyncpg
import structlog

from avtozyabr.models import ProductStock, StockEvent, WishlistItem
from avtozyabr.poller.wishlist import get_last_stock

log = structlog.get_logger(__name__)


async def detect_stock_changes(
    conn: asyncpg.Connection,
    item: WishlistItem,
    current: ProductStock,
) -> list[StockEvent]:
    """
    Returns StockEvents for variants that flipped out_of_stock → in_stock.
    Ignores variants that were already in stock on the previous poll.
    """
    previous = await get_last_stock(conn, item.product_id)
    events: list[StockEvent] = []

    for variant in current.in_stock_variants():
        was_in_stock = previous.get(variant.variant_id, False)
        if not was_in_stock:
            log.info(
                "stock.appeared",
                product_id=item.product_id,
                product_name=item.name,
                variant_id=variant.variant_id,
                variant_name=variant.variant_name,
                price=str(variant.price),
            )
            events.append(
                StockEvent(
                    product_id=item.product_id,
                    product_name=item.name,
                    product_url=item.url,
                    variant=variant,
                )
            )

    return events


async def get_active_wishlist_products(conn: asyncpg.Connection) -> list[dict]:
    """Return all active wishlist products with their auto-order rule (if any)."""
    rows = await conn.fetch(
        """
        SELECT
            w.product_id,
            w.name,
            w.url,
            w.image_url,
            r.enabled          AS rule_enabled,
            r.max_price,
            r.max_qty,
            r.require_confirm
        FROM wishlist_items w
        LEFT JOIN auto_order_rules r ON r.product_id = w.product_id
        WHERE w.is_active = TRUE
        ORDER BY w.product_id
        """
    )
    return [dict(row) for row in rows]
