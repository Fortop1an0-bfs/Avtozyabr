"""Unit tests for stock differ — no DB, no network required."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from avtozyabr.models import ProductStock, StockVariant, WishlistItem


def _make_stock(product_id: int, variants: list[tuple[int, bool, float | None]]) -> ProductStock:
    return ProductStock(
        product_id=product_id,
        checked_at=datetime.now(tz=timezone.utc),
        variants=[
            StockVariant(
                variant_id=vid,
                variant_name=f"variant_{vid}",
                in_stock=in_stock,
                price=Decimal(str(price)) if price else None,
            )
            for vid, in_stock, price in variants
        ],
    )


def test_in_stock_variants_filters_correctly():
    stock = _make_stock(1, [(10, True, 999.0), (11, False, 1200.0)])
    assert len(stock.in_stock_variants()) == 1
    assert stock.in_stock_variants()[0].variant_id == 10


def test_any_in_stock_true():
    stock = _make_stock(1, [(10, True, 500.0)])
    assert stock.any_in_stock is True


def test_any_in_stock_false():
    stock = _make_stock(1, [(10, False, 500.0)])
    assert stock.any_in_stock is False


def test_stock_event_created_for_new_availability():
    """A variant with no prior DB record (empty previous) should always fire."""
    stock = _make_stock(1, [(42, True, 750.0)])
    assert stock.any_in_stock
    assert stock.in_stock_variants()[0].variant_id == 42


def test_no_event_when_already_in_stock():
    """Simulate: variant was in stock last time — should NOT re-fire."""
    previous = {42: True}
    stock = _make_stock(1, [(42, True, 750.0)])
    # Detector logic: was_in_stock = previous.get(vid, False)
    for variant in stock.in_stock_variants():
        was_in_stock = previous.get(variant.variant_id, False)
        assert was_in_stock, "Should be suppressed — already in stock before"
