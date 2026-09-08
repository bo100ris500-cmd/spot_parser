from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.analytics.rsi_divergence import detect_regular_divergence
from app.config import AppConfig
from app.db.models import OhlcCandle, SentAlert, WatchedPair
from app.db.session import get_session_factory
from app.exchanges.ccxt_adapter import CcxtExchangeAdapter
from app.notify.publisher import AlertPublisher
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.services.runtime import RuntimeHub

logger = get_logger(__name__)

TF_SECONDS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}


class KlineCollector:
    """Periodically fetch OHLC candles and detect RSI divergences on closed bars."""

    def __init__(
        self,
        config: AppConfig,
        publisher: AlertPublisher,
        hub: "RuntimeHub",
    ) -> None:
        self.config = config
        self.publisher = publisher
        self.hub = hub
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_checked: dict[tuple[int, str], datetime] = {}

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="kline-collector")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        logger.info("Kline collector started")
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Kline tick error: %s", exc)
            await asyncio.sleep(20)

    async def _tick(self) -> None:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(select(WatchedPair))
            pairs = list(result.scalars().all())

        report_tfs = list(self.config.get("report", "timeframes", default=["15m", "1h", "4h", "1d", "1w"]))
        rsi_tfs = list(self.config.get("rsi", "timeframes", default=["1h", "4h", "1d"]))
        all_tfs = sorted(set(report_tfs) | set(rsi_tfs), key=lambda x: TF_SECONDS.get(x, 0))

        for pair in pairs:
            adapter = self.hub.get_adapter(pair.exchange)
            if adapter is None:
                continue
            for tf in all_tfs:
                try:
                    await self._sync_pair_tf(adapter, pair, tf, check_rsi=tf in rsi_tfs)
                except Exception as exc:
                    logger.warning(
                        "Kline sync failed %s %s %s: %s", pair.exchange, pair.symbol, tf, exc
                    )

    async def _sync_pair_tf(
        self,
        adapter: CcxtExchangeAdapter,
        pair: WatchedPair,
        timeframe: str,
        *,
        check_rsi: bool,
    ) -> None:
        candles = await adapter.fetch_ohlcv(pair.symbol, timeframe, limit=200)
        if not candles:
            return

        factory = get_session_factory()
        async with factory() as session:
            for c in candles:
                stmt = (
                    insert(OhlcCandle)
                    .values(
                        pair_id=pair.id,
                        timeframe=timeframe,
                        open_time=c.open_time,
                        open=c.open,
                        high=c.high,
                        low=c.low,
                        close=c.close,
                        volume=c.volume,
                    )
                    .on_conflict_do_update(
                        constraint="uq_ohlc_pair_tf_time",
                        set_={
                            "open": c.open,
                            "high": c.high,
                            "low": c.low,
                            "close": c.close,
                            "volume": c.volume,
                        },
                    )
                )
                await session.execute(stmt)
            await session.commit()

        if not check_rsi:
            return

        # Use only closed candles: drop the last (possibly forming) candle
        closed = candles[:-1] if len(candles) > 1 else []
        if len(closed) < 30:
            return

        last_closed = closed[-1].open_time
        key = (pair.id, timeframe)
        if self._last_checked.get(key) == last_closed:
            return

        # Only alert shortly after candle close (<= ~90s grace via 20s poll)
        now = datetime.now(timezone.utc)
        tf_sec = TF_SECONDS.get(timeframe, 3600)
        close_time = last_closed.timestamp() + tf_sec
        if now.timestamp() - close_time > 90:
            self._last_checked[key] = last_closed
            return

        period = int(self.config.get("rsi", "period", default=14))
        lookback = int(self.config.get("rsi", "swing_lookback", default=8))
        min_dist = int(self.config.get("rsi", "min_swing_distance", default=3))

        signal = detect_regular_divergence(
            [c.high for c in closed],
            [c.low for c in closed],
            [c.close for c in closed],
            timeframe=timeframe,
            rsi_period=period,
            swing_lookback=lookback,
            min_swing_distance=min_dist,
        )
        self._last_checked[key] = last_closed
        if signal is None:
            return

        fingerprint = (
            f"rsi:{pair.id}:{timeframe}:{signal.divergence_type}:{signal.index_a}:{signal.index_b}"
        )
        async with factory() as session:
            exists = await session.execute(
                select(SentAlert).where(
                    SentAlert.pair_id == pair.id,
                    SentAlert.alert_type == "rsi_divergence",
                    SentAlert.fingerprint == fingerprint,
                )
            )
            if exists.scalar_one_or_none():
                return
            session.add(
                SentAlert(
                    pair_id=pair.id,
                    alert_type="rsi_divergence",
                    fingerprint=fingerprint,
                    sent_at=now,
                    payload=signal.divergence_type,
                )
            )
            await session.commit()

        await self.publisher.publish(
            {
                "type": "rsi_divergence",
                "pair_id": pair.id,
                "exchange": pair.exchange,
                "symbol": pair.symbol,
                "coin": pair.coin,
                "timeframe": timeframe,
                "divergence": signal.divergence_type,
                "price_a": signal.price_a,
                "price_b": signal.price_b,
                "rsi_a": signal.rsi_a,
                "rsi_b": signal.rsi_b,
                "ts": now.isoformat(),
                "fingerprint": fingerprint,
            }
        )
        logger.info(
            "RSI %s divergence %s %s %s",
            signal.divergence_type,
            pair.exchange,
            pair.symbol,
            timeframe,
        )
