"""Inline keyboard factories for Telegram messages."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def stock_alert_keyboard(attempt_id: int, checkout_url: str) -> InlineKeyboardMarkup:
    """Keyboard attached to a 'товар появился' notification."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Подтвердить заказ",
            callback_data=f"confirm:{attempt_id}",
        ),
        InlineKeyboardButton(
            text="🛒 Открыть корзину",
            url=checkout_url,
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"cancel:{attempt_id}",
        )
    )
    return builder.as_markup()


def notify_only_keyboard(checkout_url: str) -> InlineKeyboardMarkup:
    """Used when auto-order rule is disabled — just a link to the cart."""
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🛒 Перейти на сайт", url=checkout_url))
    return builder.as_markup()


def rules_list_keyboard(product_ids: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for pid in product_ids:
        builder.row(
            InlineKeyboardButton(text=f"#{pid}", callback_data=f"rule_toggle:{pid}"),
        )
    return builder.as_markup()
