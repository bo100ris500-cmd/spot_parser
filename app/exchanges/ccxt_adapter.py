from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator

import ccxt.pro as ccxtpro

from app.exchanges.base import NormalizedCandle, NormalizedTrade, Side
from app.exchanges.capabilities import supports_timeframe
from app.exchanges.names import quotes_for
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
    "hyperliquid": "hyperliquid",
    "coinbase": "coinbase",
    "lbank": "lbank",
}


def _ms_to_dt(ms: int | float | None) -> datetime:
    if ms is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)


def normalize_side(trade: dict[str, Any]) -> Side:
    side = trade.get("side")
    if side in ("buy", "sell"):
        return side  # type: ignore[return-value]

    info = trade.get("info") or {}
    if "m" in info:
        return "sell" if info["m"] else "buy"
    if "isBuyerMaker" in info:
        return "sell" if info["isBuyerMaker"] else "buy"
    if "buyerIsMaker" in info:
        return "sell" if info["buyerIsMaker"] else "buy"
    return "buy"


class CcxtExchangeAdapter:
    def __init__(self, name: str) -> None:
        if name not in CCXT_IDS:
            raise ValueError(f"Unsupported exchange: {name}")
        self.name = name
        ccxt_id = CCXT_IDS[name]
        exchange_cls = getattr(ccxtpro, ccxt_id)
        options: dict[str, Any] = {"defaultType": "spot"}
        if name == "hyperliquid":
            options["defaultType"] = "spot"
        self._exchange = exchange_cls({"enableRateLimit": True, "options": options})
        self._markets_loaded = False

    async def load_markets(self, reload: bool = False) -> None:
        if self._markets_loaded and not reload:
            return
        await self._exchange.load_markets(reload)
        self._markets_loaded = True
        logger.info("Loaded markets for %s (%s symbols)", self.name, len(self._exchange.markets))

    def normalize_symbol(self, coin: str) -> str:
        coin = coin.upper().strip()
        if "/" in coin:
            return coin
        quote = quotes_for(self.name)[0]
        return f"{coin}/{quote}"

    def resolve_symbol(self, coin: str) -> str | None:
        """Return active spot symbol if present on the exchange."""
        coin = coin.upper().strip()
        if "/" in coin:
            candidates = [coin]
        else:
            candidates = [f"{coin}/{q}" for q in quotes_for(self.name)]
        if not self._markets_loaded:
            return candidates[0]
        markets = self._exchange.markets or {}
        for symbol in candidates:
            m = markets.get(symbol)
            if not m:
                continue
            if m.get("active") is False:
                continue
            if m.get("spot") is True:
                return symbol
            mtype = m.get("type")
            if mtype == "spot":
                return symbol
            if m.get("swap") is not True and mtype not in ("swap", "future", "option"):
                return symbol
        return None

    def supports_timeframe(self, timeframe: str) -> bool:
        if not supports_timeframe(self.name, timeframe):
            return False
        # Also respect ccxt timeframes map when available
        tfs = getattr(self._exchange, "timeframes", None) or {}
        if tfs and timeframe not in tfs:
            # Coinbase etc. may use different keys; trust our capability table
            return supports_timeframe(self.name, timeframe)
        return True

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
        markets = self._exchange.markets or {}
        m = markets.get(symbol)
        if not m or m.get("active") is False:
            raise ValueError(f"invalid symbol {symbol}")
        while True:
            trades = await self._exchange.watch_trades(symbol)
            normalized = [self._to_normalized_trade(t, symbol) for t in trades]
            yield normalized

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int = 200
    ) -> list[NormalizedCandle]:
        await self.load_markets()
        if not self.supports_timeframe(timeframe):
            return []
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

    async def fetch_trades_range(
        self, symbol: str, since_ms: int, limit: int = 1000
    ) -> list[NormalizedTrade]:
        """Best-effort historical trades (not persisted)."""
        await self.load_markets()
        try:
            rows = await self._exchange.fetch_trades(symbol, since=since_ms, limit=limit)
        except Exception as exc:
            logger.warning("fetch_trades failed %s %s: %s", self.name, symbol, exc)
            return []
        return [self._to_normalized_trade(t, symbol) for t in rows]

    async def fetch_quote_volume(self, symbol: str) -> float | None:
        await self.load_markets()
        try:
            ticker = await self._exchange.fetch_ticker(symbol)
        except Exception as exc:
            logger.warning("fetch_ticker failed %s %s: %s", self.name, symbol, exc)
            return None
        for key in ("quoteVolume", "baseVolume"):
            val = ticker.get(key)
            if val is not None:
                try:
                    vol = float(val)
                except (TypeError, ValueError):
                    continue
                if key == "baseVolume":
                    last = ticker.get("last") or ticker.get("close")
                    if last:
                        vol = vol * float(last)
                    else:
                        continue
                return vol
        info = ticker.get("info") or {}
        for key in ("quoteVolume", "volValue", "turnover24h", "volume24h", "volume_24h"):
            if key in info and info[key] is not None:
                try:
                    return float(info[key])
                except (TypeError, ValueError):
                    continue
        return None

    async def close(self) -> None:
        try:
            await self._exchange.close()
        except Exception as exc:
            logger.warning("Error closing %s: %s", self.name, exc)
