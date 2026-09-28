from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.exchanges.base import NormalizedTrade
from app.exchanges.ccxt_adapter import CcxtExchangeAdapter
from app.utils.logging import get_logger
from app.utils.reconnect import PermanentStreamError, with_exponential_backoff

if TYPE_CHECKING:
    from app.services.runtime import RuntimeHub

logger = get_logger(__name__)


class TradeCollector:
    """Per-pair trade stream with independent reconnect and permanent-failure pause."""

    def __init__(
        self,
        adapter: CcxtExchangeAdapter,
        pair_id: int,
        symbol: str,
        hub: "RuntimeHub",
    ) -> None:
        self.adapter = adapter
        self.pair_id = pair_id
        self.symbol = symbol
        self.hub = hub
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._seen: set[str] = set()
        self.paused_reason: str | None = None

    def start(self) -> None:
        self._stop.clear()
        self.paused_reason = None
        self._task = asyncio.create_task(
            self._run(), name=f"trades-{self.adapter.name}-{self.symbol}"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        logger.info(
            "Starting trade stream %s %s (pair_id=%s)",
            self.adapter.name,
            self.symbol,
            self.pair_id,
        )
        cfg = self.hub.config
        base_delay = float(cfg.get("reconnect", "base_delay", default=2.0))
        max_delay = float(cfg.get("reconnect", "max_delay", default=300.0))
        max_attempts = int(cfg.get("reconnect", "max_attempts_before_pause", default=30))

        async def _loop() -> None:
            async for trades in self.adapter.watch_trades(self.symbol):
                if self._stop.is_set():
                    return
                for trade in trades:
                    if trade.trade_id in self._seen:
                        continue
                    self._seen.add(trade.trade_id)
                    if len(self._seen) > 10000:
                        self._seen = set(list(self._seen)[-5000:])
                    await self._handle(trade)

        while not self._stop.is_set():
            try:
                await with_exponential_backoff(
                    _loop,
                    name=f"ws:{self.adapter.name}:{self.symbol}",
                    base_delay=base_delay,
                    max_delay=max_delay,
                    max_attempts=max_attempts,
                )
            except asyncio.CancelledError:
                raise
            except PermanentStreamError as exc:
                self.paused_reason = str(exc)
                logger.error(
                    "Pausing trade stream %s %s: %s",
                    self.adapter.name,
                    self.symbol,
                    exc,
                )
                # Notify hub so pair can be marked / collector removed from hot path
                await self.hub.on_stream_paused(self.pair_id, str(exc))
                return
            except Exception as exc:
                logger.exception(
                    "Trade collector fatal for %s %s: %s",
                    self.adapter.name,
                    self.symbol,
                    exc,
                )
                await asyncio.sleep(5)

    async def _handle(self, trade: NormalizedTrade) -> None:
        await self.hub.on_trade(self.pair_id, trade)
