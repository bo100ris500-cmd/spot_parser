from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator

import ccxt.pro as ccxtpro

from app.exchanges.base import NormalizedCandle, NormalizedTrade, Side
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Map our exchange ids to ccxt class names
CCXT_IDS: dict[str, str] = {
    "binance": "binance",
    "bybit": "bybit",
    "bitget": "bitget",
    "okx": "okx",
    "htx": "htx",
    "kucoin": "kucoin",
    "mexc": "mexc",
    "gate": "gate",
    "bingx": "bingx",
}


def _ms_to_dt(ms: int | float | None) -> datetime:
    if ms is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)


def normalize_side(trade: dict[str, Any]) -> Side:
    """
    Normalize taker side from ccxt unified trade.
    ccxt usually provides trade['side'] as the taker side (buy/sell).
    Fallback: side from info.isBuyerMaker (Binance-style).
    """
    side = trade.get("side")
    if side in ("buy", "sell"):
        return side  # type: ignore[return-value]

    info = trade.get("info") or {}
    # Binance spot: m / isBuyerMaker — True means buyer is maker => taker is sell
    if "m" in info:
        return "sell" if info["m"] else "buy"
    if "isBuyerMaker" in info:
        return "sell" if info["isBuyerMaker"] else "buy"
    if "buyerIsMaker" in info:  # some Bybit payloads
        return "sell" if info["buyerIsMaker"] else "buy"

    # Last resort: treat as buy to avoid dropping the trade; log upstream if needed
    return "buy"


class CcxtExchangeAdapter:
    def __init__(self, name: str) -> None:
        if name not in CCXT_IDS:
            raise ValueError(f"Unsupported exchange: {name}")
        self.name = name
        ccxt_id = CCXT_IDS[name]
        exchange_cls = getattr(ccxtpro, ccxt_id)
        self._exchange = exchange_cls({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        self._markets_loaded = False

    async def load_markets(self) -> None:
        if self._markets_loaded:
            return
        await self._exchange.load_markets()
        self._markets_loaded = True
        logger.info("Loaded markets for %s (%s symbols)", self.name, len(self._exchange.markets))

    def normalize_symbol(self, coin: str) -> str:
        coin = coin.upper().strip()
        if "/" in coin:
            return coin
        return f"{coin}/USDT"

    def resolve_symbol(self, coin: str) -> str | None:
        symbol = self.normalize_symbol(coin)
        if not self._markets_loaded:
            return symbol
        markets = self._exchange.markets or {}
        if symbol in markets:
            return symbol
        # try alternate quote names used by some venues
        for alt in (f"{coin.upper()}/USDT", f"{coin.upper()}/USD"):
            if alt in markets:
                return alt
        return None

    def _to_normalized_trade(self, trade: dict[str, Any], symbol: str) -> NormalizedTrade:
        price = float(trade["price"])
        amount = float(trade["amount"])
        cost = float(trade["cost"]) if trade.get("cost") is not None else price * amount
        trade_id = str(trade.get("id") or f"{trade.get('timestamp')}-{price}-{amount}")
        side = normalize_side(trade)
        return NormalizedTrade(
            exchange=self.name,
            symbol=symbol,
            trade_id=trade_id,
            price=price,
            amount=amount,
            cost=cost,
            side=side,
            timestamp=_ms_to_dt(trade.get("timestamp")),
            raw=trade.get("info"),
        )

    async def watch_trades(self, symbol: str) -> AsyncIterator[list[NormalizedTrade]]:
        await self.load_markets()
        while True:
            trades = await self._exchange.watch_trades(symbol)
            normalized = [self._to_normalized_trade(t, symbol) for t in trades]
            yield normalized

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int = 200
    ) -> list[NormalizedCandle]:
        await self.load_markets()
        # normalize timeframe aliases
        tf = {"1D": "1d", "1W": "1w", "1d": "1d", "1w": "1w"}.get(timeframe, timeframe)
        rows = await self._exchange.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=limit)
        candles: list[NormalizedCandle] = []
        for row in rows:
            ts, o, h, l, c, v = row
            candles.append(
                NormalizedCandle(
                    exchange=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=_ms_to_dt(ts),
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=float(v),
                )
            )
        return candles

    async def close(self) -> None:
        try:
            await self._exchange.close()
        except Exception as exc:
            logger.warning("Error closing %s: %s", self.name, exc)
