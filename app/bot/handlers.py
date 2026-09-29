from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.auth import is_allowed
from app.bot.formatters import (
    format_big_alert,
    format_cd_alert,
    format_cd_combo_alert,
    format_check_table,
    format_pair_list,
    format_rsi_alert,
    parse_ticker_exchanges,
)
from app.bot.keyboards import (
    check_actions_keyboard,
    coins_keyboard,
    exchanges_keyboard,
    flags_keyboard,
    remove_actions_keyboard,
    skip_interval_keyboard,
    timeframes_keyboard,
)
from app.bot.states import AddStates, CdStates, CheckStates, CustomCdStates, OtchetStates, RemoveStates
from app.exchanges.names import display_name, ordered_enabled
from app.exchanges.registry import list_supported_exchanges
from app.services.chart import build_cd_series, parse_interval_text, render_cd_chart_png
from app.services.report import (
    build_custom_cd_report,
    build_otchet_v2,
    format_custom_cd_mono,
    format_otchet_mono,
)
from app.utils.errors import friendly_error
from app.utils.logging import get_logger
from app.utils.timefmt import format_dt, now_display

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


def _auth_message(message: Message) -> bool:
    return is_allowed(message)


def _auth_callback(callback: CallbackQuery) -> bool:
    return bool(callback.message and is_allowed(callback.message))


def _msg(callback: CallbackQuery) -> Message:
    assert callback.message is not None
    return callback.message  # type: ignore[return-value]


def _html_pre(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre>{escaped}</pre>"


async def _answer_mono(message: Message, text: str) -> None:
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    await message.answer(_html_pre(text), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /start /list /otchet /custom_cd
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await state.clear()
    exchanges = ", ".join(display_name(x) for x in ordered_enabled(list_supported_exchanges()))
    await message.answer(
        "Spot Parser бот готов.\n"
        "Команды:\n"
        "/check — проверить тикер на биржах\n"
        "/add — добавить отслеживание\n"
        "/remove — удалить отслеживание\n"
        "/list — список пар\n"
        "/cd — график кумулятивной дельты\n"
        "/otchet — текстовый отчёт\n"
        "/custom_cd — отчёт CD за период\n\n"
        f"Биржи: {exchanges}"
    )


@router.message(Command("list"))
async def cmd_list(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await state.clear()
    try:
        pairs = await get_hub().list_pairs()
        await _answer_mono(message, format_pair_list(pairs))
    except Exception as exc:
        await message.answer(friendly_error(exc))


@router.message(Command("otchet"))
async def cmd_otchet(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip():
        await _send_otchet(message, parts[1].strip().split()[0])
        return
    await state.set_state(OtchetStates.waiting_ticker)
    await message.answer("введите тикер")


@router.message(OtchetStates.waiting_ticker)
async def otchet_ticker(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    ticker = (message.text or "").strip()
    if not ticker or ticker.startswith("/"):
        await message.answer("введите тикер")
        return
    await state.clear()
    await _send_otchet(message, ticker.split()[0])


async def _send_otchet(message: Message, coin: str) -> None:
    try:
        data = await build_otchet_v2(coin, get_hub())
        await _answer_mono(message, format_otchet_mono(data))
    except Exception as exc:
        await message.answer(friendly_error(exc))


@router.message(Command("custom_cd"))
async def cmd_custom_cd(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip():
        await state.update_data(coin=parts[1].strip().split()[0].upper())
        await state.set_state(CustomCdStates.waiting_period)
        now_s = format_dt(now_display())
        await message.answer(
            f"введите период\nТекущее время: {now_s}\n"
            "Формат: 2026-09-08 20:00 - 2026-09-10 12:00 или 6h / 1d"
        )
        return
    await state.set_state(CustomCdStates.waiting_ticker)
    await message.answer("введите тикер")


@router.message(CustomCdStates.waiting_ticker)
async def custom_cd_ticker(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    ticker = (message.text or "").strip()
    if not ticker or ticker.startswith("/"):
        await message.answer("введите тикер")
        return
    await state.update_data(coin=ticker.split()[0].upper())
    await state.set_state(CustomCdStates.waiting_period)
    now_s = format_dt(now_display())
    await message.answer(
        f"введите период\nТекущее время: {now_s}\n"
        "Формат: 2026-09-08 20:00 - 2026-09-10 12:00 или 6h / 1d"
    )


@router.message(CustomCdStates.waiting_period)
async def custom_cd_period(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    data = await state.get_data()
    coin = data.get("coin")
    if not coin:
        await state.clear()
        await message.answer("Сессия сброшена. Начните снова: /custom_cd")
        return
    try:
        from datetime import datetime, timezone

        start, end = parse_interval_text(
            message.text or "",
            observation_start=datetime.now(timezone.utc),
        )
        if start is None:
            raise ValueError(
                "Укажите период, например: 6h или 2026-09-08 20:00 - 2026-09-10 12:00"
            )
        if end is None:
            end = datetime.now(timezone.utc)
        report = await build_custom_cd_report(coin, get_hub(), start, end)
        await state.clear()
        await _answer_mono(message, format_custom_cd_mono(report))
    except Exception as exc:
        await message.answer(friendly_error(exc))


# ---------------------------------------------------------------------------
# /check
# ---------------------------------------------------------------------------


@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip():
        await _run_check(message, parts[1].strip())
        return
    await state.set_state(CheckStates.waiting_ticker)
    await message.answer("введите тикер")


@router.message(CheckStates.waiting_ticker)
async def check_ticker_entered(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    ticker = (message.text or "").strip()
    if not ticker or ticker.startswith("/"):
        await message.answer("введите тикер")
        return
    await state.clear()
    await _run_check(message, ticker)


async def _run_check(message: Message, ticker: str) -> None:
    coin = ticker.split()[0].upper()
    try:
        await message.answer(f"Ищу {coin} на биржах…")
        rows = await get_hub().check_volumes(coin)
        text = format_check_table(coin, rows)
        if not rows:
            await _answer_mono(message, text)
            return
        await message.answer(
            _html_pre(text),
            parse_mode="HTML",
            reply_markup=check_actions_keyboard(coin),
        )
    except Exception as exc:
        await message.answer(friendly_error(exc))


@router.callback_query(F.data.startswith("check:"))
async def check_action(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    await callback.answer()
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    _, action, coin = parts
    hub = get_hub()
    try:
        rows = await hub.check_volumes(coin)
        if not rows:
            await callback.message.answer(f"Токен {coin} не найден на биржах.")
            return
        added: list[str] = []
        if action == "big":
            for r in rows:
                pair = await hub.add_or_update_pair(coin, r["exchange"], flag_big=True)
                added.append(display_name(pair.exchange))
            await callback.message.answer(
                f"{coin} {' '.join(added)} [big] успешно добавлен"
            )
        elif action == "cd":
            for r in rows:
                pair = await hub.add_or_update_pair(coin, r["exchange"], flag_cd=True)
                added.append(display_name(pair.exchange))
            await callback.message.answer(
                f"{coin} {' '.join(added)} [cd] успешно добавлен"
            )
    except Exception as exc:
        await callback.message.answer(friendly_error(exc))


# ---------------------------------------------------------------------------
# /add
# ---------------------------------------------------------------------------


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip():
        # Allow one-shot: /add BTC Binance Bybit
        try:
            coin, exchanges = parse_ticker_exchanges(parts[1])
        except Exception as exc:
            await message.answer(friendly_error(exc))
            return
        await state.set_state(AddStates.choosing_flags)
        await state.update_data(coin=coin, exchanges=exchanges, flag_big=False, flag_cd=False)
        await message.answer(
            "выберите действия отслеживания",
            reply_markup=flags_keyboard(big_on=False, cd_on=False),
        )
        return
    await state.set_state(AddStates.waiting_ticker_exchanges)
    await message.answer("Введите тикер и биржу")


@router.message(AddStates.waiting_ticker_exchanges)
async def add_ticker_exchanges(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    try:
        coin, exchanges = parse_ticker_exchanges(message.text or "")
    except Exception as exc:
        await message.answer(friendly_error(exc))
        return
    await state.set_state(AddStates.choosing_flags)
    await state.update_data(coin=coin, exchanges=exchanges, flag_big=False, flag_cd=False)
    await message.answer(
        "выберите действия отслеживания",
        reply_markup=flags_keyboard(big_on=False, cd_on=False),
    )


@router.callback_query(AddStates.choosing_flags, F.data.startswith("addflag:"))
async def add_flags_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    action = (callback.data or "").split(":", 1)[1]
    data = await state.get_data()
    big = bool(data.get("flag_big"))
    cd = bool(data.get("flag_cd"))

    if action == "cancel":
        await state.clear()
        await callback.answer("Отменено")
        await callback.message.edit_text("Добавление отменено.")
        return

    if action == "big":
        big = not big
        await state.update_data(flag_big=big)
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=flags_keyboard(big_on=big, cd_on=cd))
        return

    if action == "cd":
        cd = not cd
        await state.update_data(flag_cd=cd)
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=flags_keyboard(big_on=big, cd_on=cd))
        return

    if action == "ok":
        if not big and not cd:
            await callback.answer("Выберите хотя бы одно действие", show_alert=True)
            return
        await callback.answer()
        coin = data["coin"]
        exchanges = list(data["exchanges"])
        hub = get_hub()
        ok_exchanges: list[str] = []
        errors: list[str] = []
        for ex in exchanges:
            try:
                await hub.add_or_update_pair(coin, ex, flag_big=big, flag_cd=cd)
                ok_exchanges.append(display_name(ex))
            except Exception as exc:
                errors.append(f"{display_name(ex)}: {friendly_error(exc)}")
        await state.clear()
        flags = []
        if big:
            flags.append("big")
        if cd:
            flags.append("cd")
        flag_s = " ".join(f"[{f}]" for f in flags)
        if ok_exchanges:
            await callback.message.answer(
                f"{coin} {' '.join(ok_exchanges)} {flag_s} успешно добавлен"
            )
        if errors:
            await callback.message.answer("Не удалось:\n" + "\n".join(errors))
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return


# ---------------------------------------------------------------------------
# /remove
# ---------------------------------------------------------------------------


@router.message(Command("remove"))
async def cmd_remove(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await state.clear()
    try:
        pairs = await get_hub().list_pairs()
        coins = sorted({p.coin for p in pairs})
        if not coins:
            await message.answer("Список пуст — удалять нечего.")
            return
        await state.set_state(RemoveStates.choosing_coin)
        await message.answer(
            "Выберите монету для удаления:",
            reply_markup=coins_keyboard(coins, "rmcoin"),
        )
    except Exception as exc:
        await message.answer(friendly_error(exc))


@router.callback_query(F.data.startswith("rmcoin:"))
async def remove_coin(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    coin = (callback.data or "").split(":", 1)[1]
    if coin == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("Удаление отменено.")
        return
    await callback.answer()
    pairs = await get_hub().get_pairs_for_coin(coin)
    exchanges = [p.exchange for p in pairs]
    await state.set_state(RemoveStates.choosing_exchange)
    await state.update_data(coin=coin)
    await callback.message.edit_text(
        f"{coin}: выберите биржу",
        reply_markup=exchanges_keyboard(exchanges, "rmex", include_all=True),
    )


@router.callback_query(F.data.startswith("rmex:"))
async def remove_exchange(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    token = (callback.data or "").split(":", 1)[1]
    if token == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("Удаление отменено.")
        return
    await callback.answer()
    await state.update_data(exchange=None if token == "all" else token)
    await state.set_state(RemoveStates.choosing_action)
    scope = "все биржи" if token == "all" else display_name(token)
    data = await state.get_data()
    await callback.message.edit_text(
        f"{data['coin']} / {scope}: выберите действие",
        reply_markup=remove_actions_keyboard(),
    )


@router.callback_query(F.data.startswith("rmact:"))
async def remove_action(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    action = (callback.data or "").split(":", 1)[1]
    if action == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("Удаление отменено.")
        return
    data = await state.get_data()
    coin = data.get("coin")
    exchange = data.get("exchange")
    await callback.answer()
    try:
        if action == "big":
            n = await get_hub().remove_pairs(coin, exchange, clear_big=True)
            label = "большие сделки"
        elif action == "cd":
            n = await get_hub().remove_pairs(coin, exchange, clear_cd=True)
            label = "кумулятивная дельта"
        else:
            n = await get_hub().remove_pairs(coin, exchange, delete_fully=True)
            label = "полностью"
        scope = "все биржи" if not exchange else display_name(exchange)
        await state.clear()
        if n == 0:
            await callback.message.edit_text("Ничего не найдено.")
        else:
            await callback.message.edit_text(
                f"Удалено ({label}): {coin} / {scope} — записей: {n}"
            )
    except Exception as exc:
        await state.clear()
        await callback.message.answer(friendly_error(exc))


# ---------------------------------------------------------------------------
# /cd — cumulative delta chart
# ---------------------------------------------------------------------------


@router.message(Command("cd"))
async def cmd_cd(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await state.clear()
    try:
        coins = await get_hub().list_coins_with_flag(flag_cd=True)
        if not coins:
            await message.answer("Нет пар с отслеживанием кумулятивной дельты. Добавьте через /add или /check.")
            return
        await state.set_state(CdStates.choosing_coin)
        await message.answer(
            "Выберите монету:",
            reply_markup=coins_keyboard(coins, "cdcoin"),
        )
    except Exception as exc:
        await message.answer(friendly_error(exc))


@router.callback_query(F.data.startswith("cdcoin:"))
async def cd_coin(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    coin = (callback.data or "").split(":", 1)[1]
    if coin == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("Отменено.")
        return
    await callback.answer()
    pairs = await get_hub().get_pairs_for_coin(coin, flag_cd=True)
    exchanges = [p.exchange for p in pairs]
    await state.set_state(CdStates.choosing_exchange)
    await state.update_data(coin=coin)
    await callback.message.edit_text(
        f"{coin}: выберите биржу",
        reply_markup=exchanges_keyboard(exchanges, "cdex", include_agg=True),
    )


@router.callback_query(F.data.startswith("cdex:"))
async def cd_exchange(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    token = (callback.data or "").split(":", 1)[1]
    if token == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("Отменено.")
        return
    await callback.answer()
    exchange = None if token == "agg" else token
    await state.update_data(exchange=exchange)
    await state.set_state(CdStates.choosing_tf)
    label = "агрегированно" if exchange is None else display_name(exchange)
    data = await state.get_data()
    await callback.message.edit_text(
        f"{data['coin']} / {label}: выберите таймфрейм",
        reply_markup=timeframes_keyboard("cdtf"),
    )


@router.callback_query(F.data.startswith("cdtf:"))
async def cd_tf(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    tf = (callback.data or "").split(":", 1)[1]
    if tf == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("Отменено.")
        return
    await callback.answer()
    await state.update_data(timeframe=tf)
    await state.set_state(CdStates.waiting_interval)
    await callback.message.edit_text(
        "Введите интервал (например `2026-09-08 20:00 - 2026-09-10 12:00` или `6h`), "
        "либо нажмите «За всё время».",
        reply_markup=skip_interval_keyboard(),
    )


@router.callback_query(F.data.startswith("cdint:"))
async def cd_interval_btn(callback: CallbackQuery, state: FSMContext) -> None:
    if not _auth_callback(callback):
        return
    token = (callback.data or "").split(":", 1)[1]
    if token == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("Отменено.")
        return
    await callback.answer()
    await _send_cd_chart(callback.message, state, interval_text="")


@router.message(CdStates.waiting_interval)
async def cd_interval_text(message: Message, state: FSMContext) -> None:
    if not _auth_message(message):
        return
    await _send_cd_chart(message, state, interval_text=message.text or "")


async def _send_cd_chart(message: Message, state: FSMContext, interval_text: str) -> None:
    data = await state.get_data()
    coin = data.get("coin")
    exchange = data.get("exchange")
    timeframe = data.get("timeframe")
    if not coin or not timeframe:
        await state.clear()
        await message.answer("Сессия сброшена. Начните снова: /cd")
        return
    try:
        # Need observation start for interval parsing defaults
        pairs = await get_hub().get_pairs_for_coin(coin, flag_cd=True)
        if exchange:
            pairs = [p for p in pairs if p.exchange == exchange]
        if not pairs:
            raise ValueError("Нет данных по выбранной паре")
        obs = min((p.cd_started_at or p.timestamp_added) for p in pairs)
        range_start, range_end = parse_interval_text(interval_text, observation_start=obs)
        series = await build_cd_series(
            coin=coin,
            exchange=exchange,
            timeframe=timeframe,
            range_start=range_start,
            range_end=range_end,
        )
        png = render_cd_chart_png(series)
        await state.clear()
        await message.answer_photo(
            BufferedInputFile(png, filename=f"{coin}_{timeframe}.png"),
            caption=f"{series['title']}\n{series['subtitle']}",
        )
    except Exception as exc:
        await message.answer(friendly_error(exc))


# ---------------------------------------------------------------------------
# Alerts / setup
# ---------------------------------------------------------------------------

_rsi_bot: Bot | None = None
_rsi_chat_id: int | None = None


def set_rsi_bot(bot: Bot | None, chat_id: int | None) -> None:
    global _rsi_bot, _rsi_chat_id
    _rsi_bot = bot
    _rsi_chat_id = chat_id


async def send_alert(bot: Bot, chat_id: int, alert: dict) -> None:
    from app.notify.pushover import send_pushover
    from app.notify.telegram_outbox import (
        PRIO_BIG,
        PRIO_CD,
        PRIO_COMBO,
        PRIO_OTHER,
        PRIO_RSI,
        get_outbox,
    )

    t = alert.get("type")
    if t == "big_trade":
        text = format_big_alert(alert)
        target_bot, target_chat = bot, chat_id
        priority = PRIO_BIG
    elif t == "cd_spike":
        text = format_cd_alert(alert)
        target_bot, target_chat = bot, chat_id
        priority = PRIO_CD
    elif t == "cd_combo":
        text = format_cd_combo_alert(alert)
        target_bot, target_chat = bot, chat_id
        priority = PRIO_COMBO
    elif t == "rsi_divergence":
        text = format_rsi_alert(alert)
        if _rsi_bot is not None and _rsi_chat_id is not None:
            target_bot, target_chat = _rsi_bot, _rsi_chat_id
        else:
            target_bot, target_chat = bot, chat_id
        priority = PRIO_RSI
    else:
        text = f"Alert: {alert}"
        target_bot, target_chat = bot, chat_id
        priority = PRIO_OTHER

    outbox = get_outbox()
    await outbox.send(target_bot, target_chat, text, priority=priority)

    if alert.get("pushover"):
        try:
            await send_pushover(
                title=f"КОМБО {alert.get('coin', '')} {alert.get('exchange', '')}",
                message=text,
                priority=int(alert.get("pushover_priority") or 2),
            )
        except Exception as exc:
            logger.exception("Pushover failed: %s", exc)


async def setup_bot_commands(bot: Bot) -> None:
    from aiogram.types import BotCommand

    await bot.set_my_commands(
        [
            BotCommand(command="check", description="Проверить тикер на биржах"),
            BotCommand(command="add", description="Добавить отслеживание"),
            BotCommand(command="remove", description="Удалить отслеживание"),
            BotCommand(command="list", description="Список пар"),
            BotCommand(command="cd", description="График кумулятивной дельты"),
            BotCommand(command="otchet", description="Отчёт по монете"),
            BotCommand(command="custom_cd", description="Отчёт CD за период"),
        ]
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp
