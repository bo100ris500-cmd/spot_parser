from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.analytics.big_trade import BigTradeDetector
from app.analytics.cum_delta import CumDeltaEngine
from app.collectors.kline_collector import KlineCollector
from app.collectors.trade_collector import TradeCollector
from app.config import AppConfig
from app.db.models import DeltaBucket, DeltaState, WatchedPair
from app.db.session import get_session_factory
from app.exchanges.base import NormalizedTrade
from app.exchanges.ccxt_adapter import CcxtExchangeAdapter
from app.exchanges.registry import create_adapter
from app.notify.publisher import AlertPublisher
from app.utils.logging import get_logger

logger = get_logger(__name__)


class RuntimeHub:
    """Central runtime: adapters, collectors, analytics wiring."""

    def __init__(self, config: AppConfig, publisher: AlertPublisher) -> None:
        self.config = config
        self.publisher = publisher
        self.big = BigTradeDetector(config, publisher)
        self.cd = CumDeltaEngine(config, publisher)
        self.adapters: dict[str, CcxtExchangeAdapter] = {}
        self.collectors: dict[int, TradeCollector] = {}
        self.kline_collector = KlineCollector(config, publisher, self)
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        enabled = list(self.config.get("exchanges", "enabled", default=[]))
        for name in enabled:
            try:
                adapter = create_adapter(name)
                await adapter.load_markets()
                self.adapters[name] = adapter
            except Exception as exc:
                logger.error("Failed to init exchange %s: %s", name, exc)

        await self._restore_pairs()
        self.kline_collector.start()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="db-flush")
        logger.info("RuntimeHub started with %s adapters", len(self.adapters))

    async def stop(self) -> None:
        await self.kline_collector.stop()
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        for c in list(self.collectors.values()):
            await c.stop()
        for adapter in self.adapters.values():
            await adapter.close()

    def get_adapter(self, exchange: str) -> CcxtExchangeAdapter | None:
        return self.adapters.get(exchange.lower())

    async def _restore_pairs(self) -> None:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(select(WatchedPair))
            pairs = list(result.scalars().all())
            for pair in pairs:
                ds = await session.get(DeltaState, pair.id)
                cum = ds.cum_delta if ds else 0.0
                await self._activate_pair(pair, cum_delta=cum)

    async def _activate_pair(self, pair: WatchedPair, cum_delta: float = 0.0) -> None:
        self.big.set_pair_meta(
            pair.id,
            exchange=pair.exchange,
            symbol=pair.symbol,
            coin=pair.coin,
            enabled=pair.flag_big,
            threshold_usd=pair.big_threshold_usd,
        )
        self.cd.set_pair_meta(
            pair.id,
            exchange=pair.exchange,
            symbol=pair.symbol,
            coin=pair.coin,
            alerts_enabled=pair.flag_cd,
            cum_delta=cum_delta,
        )
        if pair.id in self.collectors:
            return
        adapter = self.get_adapter(pair.exchange)
        if adapter is None:
            logger.warning("No adapter for %s, pair %s not streaming", pair.exchange, pair.symbol)
            return
        collector = TradeCollector(adapter, pair.id, pair.symbol, self)
        self.collectors[pair.id] = collector
        collector.start()

    async def on_trade(self, pair_id: int, trade: NormalizedTrade) -> None:
        await self.big.on_trade(pair_id, trade)
        await self.cd.on_trade(pair_id, trade)

    async def add_or_update_pair(
        self,
        coin: str,
        exchange: str,
        *,
        flag_big: bool = False,
        flag_cd: bool = False,
        big_threshold_usd: float | None = None,
    ) -> WatchedPair:
        coin = coin.upper().strip()
        exchange = exchange.lower().strip()
        adapter = self.get_adapter(exchange)
        if adapter is None:
            raise ValueError(f"Биржа не поддерживается или не инициализирована: {exchange}")

        symbol = adapter.resolve_symbol(coin)
        if symbol is None:
            raise ValueError(f"Пара {coin}/USDT не найдена на {exchange}")

        factory = get_session_factory()
        async with self._lock:
            async with factory() as session:
                result = await session.execute(
                    select(WatchedPair).where(
                        WatchedPair.coin == coin, WatchedPair.exchange == exchange
                    )
                )
                pair = result.scalar_one_or_none()
                if pair is None:
                    pair = WatchedPair(
                        coin=coin,
                        exchange=exchange,
                        symbol=symbol,
                        flag_big=flag_big,
                        flag_cd=flag_cd,
                        big_threshold_usd=big_threshold_usd,
                        timestamp_added=datetime.now(timezone.utc),
                    )
                    session.add(pair)
                    await session.flush()
                    session.add(DeltaState(pair_id=pair.id, cum_delta=0.0, updated_at=datetime.now(timezone.utc)))
                    await session.commit()
                    await session.refresh(pair)
                    await self._activate_pair(pair, cum_delta=0.0)
                else:
                    # update flags without resetting delta
                    if flag_big:
                        pair.flag_big = True
                    if flag_cd:
                        pair.flag_cd = True
                    if big_threshold_usd is not None:
                        pair.big_threshold_usd = big_threshold_usd
                    pair.updated_at = datetime.now(timezone.utc)
                    await session.commit()
                    await session.refresh(pair)
                    ds = await session.get(DeltaState, pair.id)
                    cum = ds.cum_delta if ds else 0.0
                    self.big.set_pair_meta(
                        pair.id,
                        exchange=pair.exchange,
                        symbol=pair.symbol,
                        coin=pair.coin,
                        enabled=pair.flag_big,
                        threshold_usd=pair.big_threshold_usd,
                    )
                    self.cd.set_pair_meta(
                        pair.id,
                        exchange=pair.exchange,
                        symbol=pair.symbol,
                        coin=pair.coin,
                        alerts_enabled=pair.flag_cd,
                        cum_delta=cum,
                    )
                    if pair.id not in self.collectors:
                        await self._activate_pair(pair, cum_delta=cum)
                return pair

    async def remove_pair(self, coin: str, exchange: str | None = None) -> int:
        coin = coin.upper().strip()
        factory = get_session_factory()
        removed = 0
        async with self._lock:
            async with factory() as session:
                q = select(WatchedPair).where(WatchedPair.coin == coin)
                if exchange:
                    q = q.where(WatchedPair.exchange == exchange.lower().strip())
                result = await session.execute(q)
                pairs = list(result.scalars().all())
                for pair in pairs:
                    collector = self.collectors.pop(pair.id, None)
                    if collector:
                        await collector.stop()
                    await session.delete(pair)
                    removed += 1
                await session.commit()
        return removed

    async def list_pairs(self) -> list[WatchedPair]:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(select(WatchedPair).order_by(WatchedPair.exchange, WatchedPair.coin))
            return list(result.scalars().all())

    async def _flush_loop(self) -> None:
        interval = float(self.config.get("flush_interval_sec", default=2))
        while True:
            try:
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Flush error: %s", exc)
            await asyncio.sleep(interval)

    async def _flush_once(self) -> None:
        dirty = self.cd.pop_dirty_states()
        buckets = self.cd.pop_buckets()
        if not dirty and not buckets:
            return
        factory = get_session_factory()
        now = datetime.now(timezone.utc)
        async with factory() as session:
            for pair_id, cum in dirty.items():
                stmt = (
                    insert(DeltaState)
                    .values(pair_id=pair_id, cum_delta=cum, updated_at=now)
                    .on_conflict_do_update(
                        index_elements=["pair_id"],
                        set_={"cum_delta": cum, "updated_at": now},
                    )
                )
                await session.execute(stmt)

            for b in buckets:
                existing = await session.execute(
                    select(DeltaBucket).where(
                        DeltaBucket.pair_id == b["pair_id"],
                        DeltaBucket.bucket_start == b["bucket_start"],
                        DeltaBucket.bucket_seconds == b["bucket_seconds"],
                    )
                )
                row = existing.scalar_one_or_none()
                if row is None:
                    session.add(DeltaBucket(**b))
                else:
                    row.buy_vol += b["buy_vol"]
                    row.sell_vol += b["sell_vol"]
                    row.trade_count += b["trade_count"]
                    row.notional_sum += b["notional_sum"]
            await session.commit()
