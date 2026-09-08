from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

from app.bot.auth import is_allowed
from app.bot.formatters import (
    format_big_alert,
    format_cd_alert,
    format_pair_list,
    format_report,
    format_rsi_alert,
    parse_add_args,
)
from app.exchanges.registry import list_supported_exchanges
from app.services.report import build_otchet
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.services.runtime import RuntimeHub

logger = get_logger(__name__)
router = Router()

_hub: "RuntimeHub | None" = None


def set_hub(hub: "RuntimeHub") -> None:
    global _hub
    _hub = hub


def get_hub() -> "RuntimeHub":
    assert _hub is not None
    return _hub


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not is_allowed(message):
        return
    exchanges = ", ".join(list_supported_exchanges())
    await message.answer(
        "Spot Parser бот готов.\n"
        "Команды:\n"
        "/add <Coin> <Exchange> [big] [cd] [big:USD]\n"
        "/remove <Coin> [Exchange]\n"
        "/list\n"
        "/otchet <Coin>\n\n"
        f"Биржи: {exchanges}"
    )


@router.message(Command("add"))
async def cmd_add(message: Message) -> None:
    if not is_allowed(message):
        return
    try:
        coin, exchange, flag_big, flag_cd, threshold = parse_add_args(message.text or "")
        pair = await get_hub().add_or_update_pair(
            coin,
            exchange,
            flag_big=flag_big,
            flag_cd=flag_cd,
            big_threshold_usd=threshold,
        )
        flags = []
        if pair.flag_big:
            flags.append("big" + (f":{pair.big_threshold_usd:g}" if pair.big_threshold_usd else ""))
        if pair.flag_cd:
            flags.append("cd")
        await message.answer(
            f"OK: {pair.exchange} {pair.symbol}\n"
            f"Флаги: {' '.join(flags) or '—'}\n"
            f"Добавлено: {pair.timestamp_added.strftime('%Y-%m-%d %H:%M UTC')}"
        )
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")


@router.message(Command("remove"))
async def cmd_remove(message: Message) -> None:
    if not is_allowed(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /remove <Coin> [Exchange]")
        return
    coin = parts[1]
    exchange = parts[2] if len(parts) >= 3 else None
    try:
        n = await get_hub().remove_pair(coin, exchange)
        if n == 0:
            await message.answer("Ничего не найдено.")
        else:
            scope = exchange or "все биржи"
            await message.answer(f"Удалено записей: {n} ({coin.upper()}, {scope})")
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    if not is_allowed(message):
        return
    pairs = await get_hub().list_pairs()
    await message.answer(format_pair_list(pairs))


@router.message(Command("otchet"))
async def cmd_otchet(message: Message) -> None:
    if not is_allowed(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /otchet <Coin>")
        return
    coin = parts[1]
    try:
        sections = await build_otchet(coin)
        text = format_report(coin, sections)
        # Telegram message limit ~4096
        if len(text) > 4000:
            text = text[:4000] + "\n…"
        await message.answer(text)
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")


async def send_alert(bot: Bot, chat_id: int, alert: dict) -> None:
    t = alert.get("type")
    if t == "big_trade":
        text = format_big_alert(alert)
    elif t == "cd_spike":
        text = format_cd_alert(alert)
    elif t == "rsi_divergence":
        text = format_rsi_alert(alert)
    else:
        text = f"Alert: {alert}"
    try:
        await bot.send_message(chat_id, text)
    except Exception as exc:
        logger.exception("Failed to send telegram alert: %s", exc)


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="add", description="Добавить пару"),
            BotCommand(command="remove", description="Удалить пару"),
            BotCommand(command="list", description="Список пар"),
            BotCommand(command="otchet", description="Отчёт по монете"),
        ]
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
