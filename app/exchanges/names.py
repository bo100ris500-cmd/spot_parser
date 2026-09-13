from __future__ import annotations

# Canonical ids → display labels (as shown to the user)
EXCHANGE_DISPLAY: dict[str, str] = {
    "binance": "Binance",
    "bybit": "Bybit",
    "bitget": "Bitget",
    "okx": "OKX",
    "htx": "HTX",
    "kucoin": "KuCoin",
    "mexc": "MEXC",
    "gate": "Gate",
    "bingx": "BingX",
    "hyperliquid": "Hyperliquid",
    "coinbase": "Coinbase",
    "lbank": "Lbank",
}

# Preferred quote currency order per exchange
QUOTE_PREFERENCE: dict[str, list[str]] = {
    "hyperliquid": ["USDC", "USDT", "USD"],
    "coinbase": ["USD", "USDT", "USDC"],
    "lbank": ["USDT", "USD", "USDC"],
}
DEFAULT_QUOTES = ["USDT", "USD", "USDC"]

# User aliases → canonical id
_ALIASES: dict[str, str] = {
    "binance": "binance",
    "bybit": "bybit",
    "bitget": "bitget",
    "okx": "okx",
    "htx": "htx",
    "huobi": "htx",
    "kucoin": "kucoin",
    "ku": "kucoin",
    "mexc": "mexc",
    "gate": "gate",
    "gateio": "gate",
    "bingx": "bingx",
    "hyperliquid": "hyperliquid",
    "hyper": "hyperliquid",
    "hl": "hyperliquid",
    "coinbase": "coinbase",
    "cb": "coinbase",
    "lbank": "lbank",
}


def display_name(exchange: str) -> str:
    return EXCHANGE_DISPLAY.get(exchange.lower(), exchange.capitalize())


def normalize_exchange(token: str) -> str | None:
    key = token.strip().lower().replace(" ", "")
    return _ALIASES.get(key)


def quotes_for(exchange: str) -> list[str]:
    return QUOTE_PREFERENCE.get(exchange.lower(), DEFAULT_QUOTES)


def ordered_enabled(enabled: list[str]) -> list[str]:
    """Keep known display order, append unknowns at the end."""
    order = list(EXCHANGE_DISPLAY.keys())
    seen = set()
    out: list[str] = []
    for name in order:
        if name in enabled and name not in seen:
            out.append(name)
            seen.add(name)
    for name in enabled:
        if name not in seen:
            out.append(name)
            seen.add(name)
    return out
