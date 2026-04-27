"""
Notification helpers — format and send Telegram messages.

notify_stock_event is injected into the orchestrator as notify_fn.
"""

from __future__ import annotations

from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

import structlog

from avtozyabr.bot.keyboards import notify_only_keyboard, stock_alert_keyboard
from avtozyabr.models import StockEvent

log = structlog.get_logger(__name__)


def _format_price(price: Decimal | None) -> str:
    if price is None:
        return "цена неизвестна"
    return f"{price:,.0f} ₽".replace(",", " ")


def _stock_message(event: StockEvent) -> str:
    variant = event.variant
    lines = [
        f"🟢 <b>Товар появился в наличии!</b>",
        f"",
        f"<b>{event.product_name}</b>",
        f"Вариант: {variant.variant_name}",
        f"Цена: {_format_price(variant.price)}",
        f"",
        f'<a href="{event.product_url}">Открыть товар</a>',
    ]
    return "\n".join(lines)


async def notify_stock_event(
    bot: Bot,
    chat_ids: list[int],
    event: StockEvent,
    attempt_id: int | None,
    checkout_url: str,
) -> int | None:
    """
    Send stock alert to all allowed users.
    Returns telegram message_id of the last sent message (used for callback tracking).
    """
    text = _stock_message(event)
    keyboard = (
        stock_alert_keyboard(attempt_id, checkout_url)
        if attempt_id is not None
        else notify_only_keyboard(checkout_url)
    )

    last_msg_id: int | None = None
    for chat_id in chat_ids:
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )
            last_msg_id = msg.message_id
            log.info("bot.notification_sent", chat_id=chat_id, attempt_id=attempt_id)
        except TelegramBadRequest as exc:
            log.error("bot.notification_failed", chat_id=chat_id, error=str(exc))

    return last_msg_id


async def send_plain(bot: Bot, chat_ids: list[int], text: str) -> None:
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except TelegramBadRequest as exc:
            log.error("bot.send_plain_failed", chat_id=chat_id, error=str(exc))
