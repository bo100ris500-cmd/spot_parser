from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.exchanges.names import display_name
from app.services.chart import TF_LABELS


def flags_keyboard(*, big_on: bool, cd_on: bool) -> InlineKeyboardMarkup:
    big_label = f"{'✅' if big_on else '⬜'} большие сделки"
    cd_label = f"{'✅' if cd_on else '⬜'} кумулятивная дельта"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=big_label, callback_data="addflag:big")],
            [InlineKeyboardButton(text=cd_label, callback_data="addflag:cd")],
            [InlineKeyboardButton(text="Подтвердить", callback_data="addflag:ok")],
            [InlineKeyboardButton(text="Отмена", callback_data="addflag:cancel")],
        ]
    )


def check_actions_keyboard(coin: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="большие сделки на всех биржах",
                    callback_data=f"check:big:{coin}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="кумулятивная дельта агрегированная",
                    callback_data=f"check:cd:{coin}",
                )
            ],
        ]
    )


def coins_keyboard(coins: list[str], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=c, callback_data=f"{prefix}:{c}")] for c in coins]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def exchanges_keyboard(
    exchanges: list[str],
    prefix: str,
    *,
    include_all: bool = False,
    include_agg: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ex in exchanges:
        rows.append(
            [InlineKeyboardButton(text=display_name(ex), callback_data=f"{prefix}:{ex}")]
        )
    if include_all:
        rows.append([InlineKeyboardButton(text="все", callback_data=f"{prefix}:all")])
    if include_agg:
        rows.append(
            [InlineKeyboardButton(text="агрегированно", callback_data=f"{prefix}:agg")]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def remove_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Убрать большие сделки", callback_data="rmact:big")],
            [InlineKeyboardButton(text="Убрать кумулятивную дельту", callback_data="rmact:cd")],
            [InlineKeyboardButton(text="Удалить полностью", callback_data="rmact:all")],
            [InlineKeyboardButton(text="Отмена", callback_data="rmact:cancel")],
        ]
    )


def timeframes_keyboard(prefix: str = "cdtf") -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton(text=tf, callback_data=f"{prefix}:{tf}")
        for tf in TF_LABELS[:3]
    ]
    row2 = [
        InlineKeyboardButton(text=tf, callback_data=f"{prefix}:{tf}")
        for tf in TF_LABELS[3:]
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row1,
            row2,
            [InlineKeyboardButton(text="Отмена", callback_data=f"{prefix}:cancel")],
        ]
    )


def skip_interval_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="За всё время", callback_data="cdint:all")],
            [InlineKeyboardButton(text="Отмена", callback_data="cdint:cancel")],
        ]
    )
