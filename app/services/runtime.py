from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.analytics.big_trade import BigTradeDetector
from app.analytics.cum_delta import CumDeltaEngine
from app.collectors.kline_collector import KlineCollector
from app.collectors.trade_collector import TradeCollector
from app.config import AppConfig
from app.db.models import DeltaBucket, DeltaState, OhlcCandle, SentAlert, WatchedPair
from app.db.session import get_session_factory
from app.exchanges.base import NormalizedTrade
from app.exchanges.ccxt_adapter import CcxtExchangeAdapter
from app.exchanges.names import display_name
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

    def list_adapter_names(self) -> list[str]:
        return list(self.adapters.keys())

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
        # Need a live trade stream if either tracking mode is on
        if not pair.flag_big and not pair.flag_cd:
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
        reset_cd: bool | None = None,
    ) -> WatchedPair:
        coin = coin.upper().strip()
        exchange = exchange.lower().strip()
        adapter = self.get_adapter(exchange)
        if adapter is None:
            raise ValueError(f"Биржа не поддерживается или не инициализирована: {display_name(exchange)}")

        symbol = adapter.resolve_symbol(coin)
        if symbol is None:
            quotes = "/".join(["USDT", "USD", "USDC"])
            raise ValueError(f"Пара {coin} ({quotes}) не найдена на {display_name(exchange)}")

        now = datetime.now(timezone.utc)
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
                        timestamp_added=now,
                        big_started_at=now if flag_big else None,
                        cd_started_at=now if flag_cd else None,
                    )
                    session.add(pair)
                    await session.flush()
                    session.add(DeltaState(pair_id=pair.id, cum_delta=0.0, updated_at=now))
                    await session.commit()
                    await session.refresh(pair)
                    await self._activate_pair(pair, cum_delta=0.0)
                else:
                    enabling_cd = flag_cd and not pair.flag_cd
                    enabling_big = flag_big and not pair.flag_big
                    do_reset_cd = reset_cd if reset_cd is not None else enabling_cd

                    if flag_big:
                        pair.flag_big = True
                        if enabling_big or pair.big_started_at is None:
                            pair.big_started_at = now
                    if flag_cd:
                        pair.flag_cd = True
                        if enabling_cd or pair.cd_started_at is None:
                            pair.cd_started_at = now
                    if big_threshold_usd is not None:
                        pair.big_threshold_usd = big_threshold_usd
                    pair.updated_at = now

                    if do_reset_cd and flag_cd:
                        await self._reset_cd_data(session, pair.id, now)
                        self.cd.reset_pair(pair.id, cum_delta=0.0)
                        cum = 0.0
                    else:
                        ds = await session.get(DeltaState, pair.id)
                        cum = ds.cum_delta if ds else 0.0

                    await session.commit()
                    await session.refresh(pair)
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
                    if pair.flag_big or pair.flag_cd:
                        if pair.id not in self.collectors:
                            await self._activate_pair(pair, cum_delta=cum)
                    else:
                        await self._stop_collector(pair.id)
                return pair

    async def _reset_cd_data(self, session, pair_id: int, now: datetime) -> None:
        await session.execute(delete(DeltaBucket).where(DeltaBucket.pair_id == pair_id))
        ds = await session.get(DeltaState, pair_id)
        if ds is None:
            session.add(DeltaState(pair_id=pair_id, cum_delta=0.0, updated_at=now))
        else:
            ds.cum_delta = 0.0
            ds.updated_at = now

    async def _stop_collector(self, pair_id: int) -> None:
        collector = self.collectors.pop(pair_id, None)
        if collector:
            await collector.stop()

    async def _detach_pair_runtime(self, pair_id: int) -> None:
        await self._stop_collector(pair_id)
        self.big.clear_pair(pair_id)
        self.cd.clear_pair(pair_id)

    async def remove_pairs(
        self,
        coin: str,
        exchange: str | None = None,
        *,
        clear_big: bool = False,
        clear_cd: bool = False,
        delete_fully: bool = False,
    ) -> int:
        """
        Remove tracking for coin[/exchange].
        - delete_fully: delete DB rows
        - clear_big / clear_cd: turn off flags; delete row if both false
        """
        coin = coin.upper().strip()
        factory = get_session_factory()
        affected = 0
        async with self._lock:
            async with factory() as session:
                q = select(WatchedPair).where(WatchedPair.coin == coin)
                if exchange:
                    q = q.where(WatchedPair.exchange == exchange.lower().strip())
                result = await session.execute(q)
                pairs = list(result.scalars().all())
                ids_to_delete: list[int] = []
                for pair in pairs:
                    affected += 1
                    if delete_fully or (clear_big and clear_cd):
                        ids_to_delete.append(pair.id)
                        continue
                    if clear_big:
                        pair.flag_big = False
                        pair.big_started_at = None
                        pair.big_threshold_usd = None
                    if clear_cd:
                        pair.flag_cd = False
                        pair.cd_started_at = None
                        await self._reset_cd_data(session, pair.id, datetime.now(timezone.utc))
                        self.cd.reset_pair(pair.id, 0.0)
                    if not pair.flag_big and not pair.flag_cd:
                        ids_to_delete.append(pair.id)
                    else:
                        pair.updated_at = datetime.now(timezone.utc)
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
                            cum_delta=self.cd.get_cum_delta(pair.id),
                        )
                        if not pair.flag_big and not pair.flag_cd:
                            await self._stop_collector(pair.id)

                if ids_to_delete:
                    for pid in ids_to_delete:
                        await self._detach_pair_runtime(pid)
                    # Core delete via SQL to avoid ORM blank-out of DeltaState PK
                    await session.execute(delete(SentAlert).where(SentAlert.pair_id.in_(ids_to_delete)))
                    await session.execute(delete(OhlcCandle).where(OhlcCandle.pair_id.in_(ids_to_delete)))
                    await session.execute(delete(DeltaBucket).where(DeltaBucket.pair_id.in_(ids_to_delete)))
                    await session.execute(delete(DeltaState).where(DeltaState.pair_id.in_(ids_to_delete)))
                    await session.execute(delete(WatchedPair).where(WatchedPair.id.in_(ids_to_delete)))

                await session.commit()
        return affected

    async def remove_pair(self, coin: str, exchange: str | None = None) -> int:
        """Backward-compatible full delete."""
        return await self.remove_pairs(coin, exchange, delete_fully=True)

    async def list_pairs(self) -> list[WatchedPair]:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(WatchedPair).order_by(WatchedPair.coin, WatchedPair.exchange)
            )
            return list(result.scalars().all())

    async def list_coins_with_flag(self, *, flag_big: bool = False, flag_cd: bool = False) -> list[str]:
        factory = get_session_factory()
        async with factory() as session:
            q = select(WatchedPair.coin).distinct().order_by(WatchedPair.coin)
            if flag_big:
                q = q.where(WatchedPair.flag_big.is_(True))
            if flag_cd:
                q = q.where(WatchedPair.flag_cd.is_(True))
            result = await session.execute(q)
            return [row[0] for row in result.all()]

    async def get_pairs_for_coin(
        self, coin: str, *, flag_cd: bool | None = None, flag_big: bool | None = None
    ) -> list[WatchedPair]:
        coin = coin.upper().strip()
        factory = get_session_factory()
        async with factory() as session:
            q = select(WatchedPair).where(WatchedPair.coin == coin).order_by(WatchedPair.exchange)
            if flag_cd is not None:
                q = q.where(WatchedPair.flag_cd.is_(flag_cd))
            if flag_big is not None:
                q = q.where(WatchedPair.flag_big.is_(flag_big))
            result = await session.execute(q)
            return list(result.scalars().all())

    async def check_volumes(self, coin: str) -> list[dict[str, Any]]:
        """Return exchanges where coin exists, sorted by quote volume desc."""
        coin = coin.upper().strip()
        rows: list[dict[str, Any]] = []

        async def _one(name: str, adapter: CcxtExchangeAdapter) -> dict[str, Any] | None:
            try:
                symbol = adapter.resolve_symbol(coin)
                if not symbol:
                    return None
                vol = await adapter.fetch_quote_volume(symbol)
                return {
                    "exchange": name,
                    "symbol": symbol,
                    "volume": float(vol) if vol is not None else 0.0,
                }
            except Exception as exc:
                logger.warning("check volume %s %s: %s", name, coin, exc)
                return None

        tasks = [_one(name, adapter) for name, adapter in self.adapters.items()]
        results = await asyncio.gather(*tasks)
        for item in results:
            if item is not None:
                rows.append(item)
        rows.sort(key=lambda r: r["volume"], reverse=True)
        return rows

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
