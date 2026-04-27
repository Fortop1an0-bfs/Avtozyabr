"""Shared domain models (pure dataclasses, no ORM)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING = "pending"
    CART_ADDED = "cart_added"
    AWAITING_CONFIRM = "awaiting_confirm"
    CONFIRMED = "confirmed"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WishlistItem:
    product_id: int
    name: str
    url: str
    image_url: str | None = None


@dataclass
class StockVariant:
    variant_id: int
    variant_name: str
    in_stock: bool
    price: Decimal | None


@dataclass
class ProductStock:
    product_id: int
    checked_at: datetime
    variants: list[StockVariant] = field(default_factory=list)

    @property
    def any_in_stock(self) -> bool:
        return any(v.in_stock for v in self.variants)

    def in_stock_variants(self) -> list[StockVariant]:
        return [v for v in self.variants if v.in_stock]


@dataclass
class StockEvent:
    """Emitted when a previously out-of-stock item becomes available."""

    product_id: int
    product_name: str
    product_url: str
    variant: StockVariant


@dataclass
class AutoOrderRule:
    product_id: int
    enabled: bool = True
    max_price: Decimal | None = None
    max_qty: int = 1
    require_confirm: bool = True
