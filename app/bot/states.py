from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddStates(StatesGroup):
    waiting_ticker_exchanges = State()
    choosing_flags = State()


class CheckStates(StatesGroup):
    waiting_ticker = State()


class RemoveStates(StatesGroup):
    choosing_coin = State()
    choosing_exchange = State()
    choosing_action = State()


class CdStates(StatesGroup):
    choosing_coin = State()
    choosing_exchange = State()
    choosing_tf = State()
    waiting_interval = State()
