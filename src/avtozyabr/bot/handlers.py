"""
aiogram 3 handlers.

Commands:
  /start         — welcome + status
  /status        — poller health + last poll time
  /wishlist      — list tracked products
  /rules         — show/edit auto-order rules
  /pause         — pause all polling
  /resume        — resume polling
  /kill          — emergency stop (cancel all pending orders)
  /setcookies    — paste new session cookies (JSON or Netscape)

Callbacks:
  confirm:<attempt_id>    — user confirms order
  cancel:<attempt_id>     — user cancels order
  rule_toggle:<product_id>
"""

from __future__ import annotations

import json
from functools import partial

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import structlog

from avtozyabr.orchestrator.order import (
    get_attempt_by_msg,
    handle_user_cancel,
    handle_user_confirm,
)

log = structlog.get_logger(__name__)

router = Router()

# Injected at startup (avoids circular imports)
_state: dict = {}


def setup_handlers(pool, client, allowed_users: list[int], paused_flag: list[bool]) -> None:
    """Attach shared state so handlers can access them without globals."""
    _state["pool"] = pool
    _state["client"] = client
    _state["allowed_users"] = set(allowed_users)
    _state["paused"] = paused_flag  # mutable list used as pointer


def _is_allowed(user_id: int) -> bool:
    return not _state.get("allowed_users") or user_id in _state["allowed_users"]


# ── Auth middleware (simple filter) ───────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    await message.answer(
        "👋 <b>Авто-Зябр</b> — монитор вишлиста Золотого Яблока\n\n"
        "Команды:\n"
        "/status — состояние бота\n"
        "/wishlist — отслеживаемые товары\n"
        "/rules — правила автозаказа\n"
        "/pause /resume — пауза поллинга\n"
        "/kill — экстренная остановка\n"
        "/setcookies — обновить сессию ЗЯ",
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    pool = _state.get("pool")
    client = _state.get("client")
    paused = _state.get("paused", [False])[0]

    status_icon = "⏸" if paused else "▶️"
    auth_ok = await client.is_authenticated() if client else False
    auth_icon = "✅" if auth_ok else "❌"

    if pool:
        async with pool.acquire() as conn:
            item_count = await conn.fetchval("SELECT COUNT(*) FROM wishlist_items WHERE is_active")
            pending_count = await conn.fetchval(
                "SELECT COUNT(*) FROM order_attempts WHERE status IN ('pending','cart_added','awaiting_confirm')"
            )
    else:
        item_count = pending_count = "?"

    await message.answer(
        f"{status_icon} Поллинг: {'на паузе' if paused else 'активен'}\n"
        f"{auth_icon} Авторизация ZY: {'OK' if auth_ok else 'НЕТ'}\n"
        f"📦 Товаров в вишлисте: {item_count}\n"
        f"⏳ Активных заказов: {pending_count}",
        parse_mode="HTML",
    )


@router.message(Command("wishlist"))
async def cmd_wishlist(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    pool = _state.get("pool")
    if not pool:
        await message.answer("БД не подключена")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT product_id, name, url FROM wishlist_items WHERE is_active ORDER BY name LIMIT 50"
        )
    if not rows:
        await message.answer("Вишлист пуст. Добавьте товары на сайте ЗЯ.")
        return
    lines = [f"📋 <b>Вишлист ({len(rows)} тов.):</b>"]
    for row in rows:
        lines.append(f'• <a href="{row["url"]}">{row["name"]}</a> #{row["product_id"]}')
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    pool = _state.get("pool")
    if not pool:
        await message.answer("БД не подключена")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT w.product_id, w.name, r.enabled, r.max_price, r.require_confirm
            FROM wishlist_items w
            LEFT JOIN auto_order_rules r ON r.product_id = w.product_id
            WHERE w.is_active
            ORDER BY w.name
            LIMIT 30
            """
        )
    if not rows:
        await message.answer("Нет активных товаров")
        return
    lines = ["⚙️ <b>Правила автозаказа:</b>", ""]
    for row in rows:
        icon = "✅" if row["enabled"] else "⏸"
        confirm = "🔔 с подтверждением" if row["require_confirm"] or row["require_confirm"] is None else "⚡ авто"
        price = f"до {row['max_price']} ₽" if row["max_price"] else "любая цена"
        lines.append(f"{icon} <b>{row['name']}</b> — {price}, {confirm}")
    lines.append("")
    lines.append("Для изменения используйте SQL или пришлите:\n<code>/setrule &lt;product_id&gt; max_price=1000 confirm=yes</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("setrule"))
async def cmd_setrule(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    pool = _state.get("pool")
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /setrule &lt;product_id&gt; [max_price=N] [confirm=yes|no] [enabled=yes|no]", parse_mode="HTML")
        return

    try:
        product_id = int(parts[1])
    except ValueError:
        await message.answer("product_id должен быть числом")
        return

    kwargs: dict = {}
    for part in parts[2:]:
        if "=" in part:
            k, v = part.split("=", 1)
            if k == "max_price":
                kwargs["max_price"] = float(v)
            elif k == "confirm":
                kwargs["require_confirm"] = v.lower() in ("yes", "1", "true")
            elif k == "enabled":
                kwargs["enabled"] = v.lower() in ("yes", "1", "true")

    if not pool:
        await message.answer("БД не подключена")
        return

    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM wishlist_items WHERE product_id = $1 AND is_active", product_id
        )
        if not exists:
            await message.answer(f"Товар #{product_id} не найден в вишлисте")
            return

        await conn.execute(
            """
            INSERT INTO auto_order_rules (product_id, max_price, require_confirm, enabled)
            VALUES ($1, $2, COALESCE($3, TRUE), COALESCE($4, TRUE))
            ON CONFLICT (product_id) DO UPDATE
                SET max_price = COALESCE($2, auto_order_rules.max_price),
                    require_confirm = COALESCE($3, auto_order_rules.require_confirm),
                    enabled = COALESCE($4, auto_order_rules.enabled),
                    updated_at = now()
            """,
            product_id,
            kwargs.get("max_price"),
            kwargs.get("require_confirm"),
            kwargs.get("enabled"),
        )
    await message.answer(f"✅ Правило для #{product_id} обновлено")


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    _state["paused"][0] = True
    await message.answer("⏸ Поллинг на паузе. /resume для возобновления.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    _state["paused"][0] = False
    await message.answer("▶️ Поллинг возобновлён.")


@router.message(Command("kill"))
async def cmd_kill(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    pool = _state.get("pool")
    _state["paused"][0] = True
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE order_attempts SET status = 'cancelled', updated_at = now()
                WHERE status IN ('pending','cart_added','awaiting_confirm')
                RETURNING id
                """
            )
        await message.answer(f"🛑 Стоп. Поллинг остановлен, отменено заказов: {len(rows)}")
    else:
        await message.answer("🛑 Поллинг остановлен")


@router.message(Command("setcookies"))
async def cmd_setcookies(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return
    client = _state.get("client")
    if not client:
        await message.answer("Клиент ZY не инициализирован")
        return

    # Expect JSON object after the command: /setcookies {"sessionid": "abc", ...}
    text = (message.text or "").split(None, 1)
    if len(text) < 2:
        await message.answer(
            "Передайте cookies JSON-объектом:\n"
            "<code>/setcookies {\"sessionid\": \"...\", \"csrftoken\": \"...\"}</code>",
            parse_mode="HTML",
        )
        return
    try:
        cookies = json.loads(text[1])
        if not isinstance(cookies, dict):
            await message.answer("❌ JSON должен быть объектом {name: value, ...}")
            return
        persisted = client.set_cookies(cookies)
        ok = await client.is_authenticated()
        persist_note = "💾 сохранено на диск" if persisted else "⚠️ НЕ сохранено (нет ZY_COOKIES_FILE)"
        if ok:
            await message.answer(f"✅ Cookies приняты, авторизация OK\n{persist_note}")
        else:
            await message.answer(f"⚠️ Cookies приняты, но авторизация НЕ прошла — проверьте значения\n{persist_note}")
    except json.JSONDecodeError:
        await message.answer("❌ Невалидный JSON")


# ── Callback handlers ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm:"))
async def callback_confirm(query: CallbackQuery) -> None:
    if not _is_allowed(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return

    attempt_id = int(query.data.split(":")[1])
    pool = _state.get("pool")
    client = _state.get("client")

    if pool:
        await handle_user_confirm(attempt_id, pool)

    checkout_url = await client.get_checkout_url() if client else "#"
    await query.answer("Заказ подтверждён! Открываем корзину...")
    await query.message.edit_text(
        query.message.text + "\n\n✅ <b>Подтверждено</b>",
        parse_mode="HTML",
        reply_markup=None,
    )
    # Send checkout link as separate message for easy tap
    await query.message.answer(
        f'<a href="{checkout_url}">👉 Перейти к оплате</a>',
        parse_mode="HTML",
    )
    log.info("bot.user_confirmed", attempt_id=attempt_id, user_id=query.from_user.id)


@router.callback_query(F.data.startswith("cancel:"))
async def callback_cancel(query: CallbackQuery) -> None:
    if not _is_allowed(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return

    attempt_id = int(query.data.split(":")[1])
    pool = _state.get("pool")
    if pool:
        await handle_user_cancel(attempt_id, pool)

    await query.answer("Заказ отменён")
    await query.message.edit_text(
        query.message.text + "\n\n❌ <b>Отменено</b>",
        parse_mode="HTML",
        reply_markup=None,
    )
    log.info("bot.user_cancelled", attempt_id=attempt_id, user_id=query.from_user.id)
