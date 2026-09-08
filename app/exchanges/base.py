from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Literal, Protocol


Side = Literal["buy", "sell"]


@dataclass(slots=True)
class NormalizedTrade:
    exchange: str
    symbol: str
    trade_id: str
    price: float
    amount: float
    cost: float  # price * amount in quote (USDT)
    side: Side  # taker side
    timestamp: datetime
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class NormalizedCandle:
    exchange: str
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class ExchangeAdapter(Protocol):
    name: str

    async def load_markets(self) -> None: ...

    def normalize_symbol(self, coin: str) -> str:
        """Return exchange symbol like BTC/USDT."""
        ...

    def resolve_symbol(self, coin: str) -> str | None:
        """Return symbol if market exists, else None."""
        ...

    async def watch_trades(self, symbol: str) -> AsyncIterator[list[NormalizedTrade]]: ...

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int = 200
    ) -> list[NormalizedCandle]: ...

    async def close(self) -> None: ...
