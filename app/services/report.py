from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import get_app_config
from app.db.models import DeltaBucket, DeltaState, OhlcCandle, WatchedPair
from app.db.session import get_session_factory

TF_DELTA = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}


async def build_otchet(coin: str) -> list[dict]:
    coin = coin.upper().strip()
    config = get_app_config()
    timeframes = list(config.get("report", "timeframes", default=["15m", "1h", "4h", "1d", "1w"]))
    factory = get_session_factory()
    now = datetime.now(timezone.utc)

    async with factory() as session:
        result = await session.execute(
            select(WatchedPair).where(WatchedPair.coin == coin).order_by(WatchedPair.exchange)
        )
        pairs = list(result.scalars().all())
        sections: list[dict] = []

        for pair in pairs:
            age = now - pair.timestamp_added
            ds = await session.get(DeltaState, pair.id)
            cum_delta = ds.cum_delta if ds else 0.0

            tf_blocks: list[dict] = []
            for tf in timeframes:
                need = TF_DELTA[tf]
                if age < need:
                    continue

                # price change from OHLC
                candles = await session.execute(
                    select(OhlcCandle)
                    .where(OhlcCandle.pair_id == pair.id, OhlcCandle.timeframe == tf)
                    .order_by(OhlcCandle.open_time.desc())
                    .limit(2)
                )
                cands = list(candles.scalars().all())
                if len(cands) < 1:
                    # fallback: try 15m/1h for price even if exact tf sparse
                    continue

                # For interval price: compare close of last closed vs open of interval
                since = now - need
                hist = await session.execute(
                    select(OhlcCandle)
                    .where(
                        OhlcCandle.pair_id == pair.id,
                        OhlcCandle.timeframe == tf,
                        OhlcCandle.open_time >= since - need,
                    )
                    .order_by(OhlcCandle.open_time.asc())
                )
                series = list(hist.scalars().all())
                if len(series) < 2:
                    continue
                price_open = series[0].open
                price_close = series[-1].close
                price_abs = price_close - price_open
                price_pct = (price_abs / price_open * 100.0) if price_open else 0.0

                # delta over interval from buckets
                bucket_q = await session.execute(
                    select(
                        func.coalesce(func.sum(DeltaBucket.buy_vol), 0.0),
                        func.coalesce(func.sum(DeltaBucket.sell_vol), 0.0),
                    ).where(
                        DeltaBucket.pair_id == pair.id,
                        DeltaBucket.bucket_start >= since,
                    )
                )
                buy_vol, sell_vol = bucket_q.one()
                buy_vol = float(buy_vol)
                sell_vol = float(sell_vol)
                delta_abs = buy_vol - sell_vol
                volume = buy_vol + sell_vol
                delta_pct = (delta_abs / volume * 100.0) if volume > 0 else 0.0

                tf_blocks.append(
                    {
                        "tf": tf,
                        "price_abs": price_abs,
                        "price_pct": price_pct,
                        "delta_abs": delta_abs,
                        "delta_pct": delta_pct,
                    }
                )

            sections.append(
                {
                    "exchange": pair.exchange,
                    "symbol": pair.symbol,
                    "cum_delta": cum_delta,
                    "timeframes": tf_blocks,
                }
            )
        return sections
