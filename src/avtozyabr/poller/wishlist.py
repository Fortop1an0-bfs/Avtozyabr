"""Fetch wishlist and product stock, persist to DB."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import asyncpg
import structlog

from avtozyabr.models import ProductStock, StockVariant, WishlistItem
from avtozyabr.poller.client import ZYClient

log = structlog.get_logger(__name__)


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _parse_wishlist(raw: list[dict[str, Any]]) -> list[WishlistItem]:
    items: list[WishlistItem] = []
    for entry in raw:
        try:
            product = entry.get("product", entry)
            items.append(
                WishlistItem(
                    product_id=int(product["id"]),
                    name=product.get("name", ""),
                    url=product.get("url", product.get("slug", "")),
                    image_url=product.get("image", product.get("imageUrl")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("wishlist.parse_error", entry=entry, error=str(exc))
    return items


def _parse_stock(product_id: int, raw: dict[str, Any]) -> ProductStock:
    checked_at = datetime.now(tz=timezone.utc)
    variants: list[StockVariant] = []

    raw_variants = raw.get("variants", raw.get("data", {}).get("variants", []))
    if not raw_variants:
        # Fallback: treat whole product as single nameless variant
        raw_variants = [raw.get("data", raw)]

    for v in raw_variants:
        try:
            price_raw = v.get("price", v.get("currentPrice"))
            variants.append(
                StockVariant(
                    variant_id=int(v.get("id", product_id)),
                    variant_name=str(v.get("name", v.get("colorName", "default"))),
                    in_stock=bool(v.get("inStock", v.get("available", False))),
                    price=Decimal(str(price_raw)) if price_raw is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("stock.parse_error", product_id=product_id, variant=v, error=str(exc))

    return ProductStock(product_id=product_id, checked_at=checked_at, variants=variants)


# ── DB persistence ─────────────────────────────────────────────────────────────

async def upsert_wishlist_items(conn: asyncpg.Connection, items: list[WishlistItem]) -> None:
    for item in items:
        await conn.execute(
            """
            INSERT INTO wishlist_items (product_id, name, url, image_url, last_seen_at, is_active)
            VALUES ($1, $2, $3, $4, now(), TRUE)
            ON CONFLICT (product_id) DO UPDATE
                SET name = EXCLUDED.name,
                    url = EXCLUDED.url,
                    image_url = EXCLUDED.image_url,
                    last_seen_at = now(),
                    is_active = TRUE
            """,
            item.product_id, item.name, item.url, item.image_url,
        )


async def save_stock_snapshot(conn: asyncpg.Connection, stock: ProductStock) -> None:
    for variant in stock.variants:
        await conn.execute(
            """
            INSERT INTO stock_history
                (product_id, checked_at, in_stock, price, variant_id, variant_name)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            stock.product_id,
            stock.checked_at,
            variant.in_stock,
            variant.price,
            variant.variant_id,
            variant.variant_name,
        )


async def get_last_stock(
    conn: asyncpg.Connection, product_id: int
) -> dict[int, bool]:
    """Return {variant_id: in_stock} for the most recent poll of this product."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (variant_id)
            variant_id, in_stock
        FROM stock_history
        WHERE product_id = $1
        ORDER BY variant_id, checked_at DESC
        """,
        product_id,
    )
    return {row["variant_id"]: row["in_stock"] for row in rows}


# ── Main polling operation ─────────────────────────────────────────────────────

async def fetch_and_store_wishlist(
    client: ZYClient, pool: asyncpg.Pool
) -> list[WishlistItem]:
    raw = await client.get_wishlist()
    items = _parse_wishlist(raw)
    log.info("wishlist.fetched", count=len(items))
    async with pool.acquire() as conn:
        await upsert_wishlist_items(conn, items)
    return items


async def fetch_and_store_stock(
    client: ZYClient, pool: asyncpg.Pool, product_id: int
) -> ProductStock:
    raw = await client.get_product_stock(product_id)
    stock = _parse_stock(product_id, raw)
    async with pool.acquire() as conn:
        await save_stock_snapshot(conn, stock)
    return stock
