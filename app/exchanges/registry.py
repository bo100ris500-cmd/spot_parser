from __future__ import annotations

from app.exchanges.ccxt_adapter import CCXT_IDS, CcxtExchangeAdapter
from app.exchanges.names import ordered_enabled


def create_adapter(name: str) -> CcxtExchangeAdapter:
    return CcxtExchangeAdapter(name.lower())


def list_supported_exchanges() -> list[str]:
    return ordered_enabled(list(CCXT_IDS.keys()))
