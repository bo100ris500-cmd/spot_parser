from __future__ import annotations

# Timeframes supported for OHLCV per exchange (spot).
# Missing TF → collector skips instead of spamming errors.
EXCHANGE_TIMEFRAMES: dict[str, set[str]] = {
    "binance": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "bybit": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "bitget": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "okx": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "htx": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "kucoin": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "mexc": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "gate": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "bingx": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
    "hyperliquid": {"1m", "5m", "15m", "1h", "4h", "1d"},
    # Coinbase Advanced Trade: no native 4h / 1w
    "coinbase": {"1m", "5m", "15m", "1h", "1d"},
    "lbank": {"1m", "5m", "15m", "1h", "4h", "1d", "1w"},
}

DEFAULT_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}

# Error substrings that mean the symbol/market is permanently invalid
INVALID_SYMBOL_MARKERS = (
    "invalid symbol",
    "symbol is not found",
    "symbol not found",
    "does not have market symbol",
    "market symbol not found",
    "unknown symbol",
    "not a valid symbol",
    "100204",  # BingX
    "err-code: invalid-symbol",
    "invalid-amount",
)


def supports_timeframe(exchange: str, timeframe: str) -> bool:
    allowed = EXCHANGE_TIMEFRAMES.get(exchange.lower(), DEFAULT_TIMEFRAMES)
    return timeframe in allowed


def is_invalid_symbol_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(m in text for m in INVALID_SYMBOL_MARKERS)
