from __future__ import annotations

from app.exchanges.ccxt_adapter import CCXT_IDS, CcxtExchangeAdapter


def create_adapter(name: str) -> CcxtExchangeAdapter:
    return CcxtExchangeAdapter(name.lower())


def list_supported_exchanges() -> list[str]:
    return sorted(CCXT_IDS.keys())
