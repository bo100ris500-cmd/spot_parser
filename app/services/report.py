from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.config import get_app_config
from app.db.models import DeltaBucket, DeltaState, OhlcCandle, WatchedPair
from app.db.session import get_session_factory
from app.exchanges.names import display_name
from app.utils.timefmt import format_dt

if TYPE_CHECKING:
    from app.services.runtime import RuntimeHub

TF_DELTA = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}


def _fmt_num(v: float) -> str:
    """Format like +63 516,2 / −3 731,4"""
    sign = "+" if v > 0 else ("−" if v < 0 else "")
    abs_v = abs(v)
    if abs_v >= 1000:
        # space as thousands separator, comma decimals
        int_part = int(abs_v)
        frac = abs_v - int_part
        int_s = f"{int_part:,}".replace(",", " ")
        if frac >= 0.05 or (frac > 0 and abs_v < 10000):
            return f"{sign}{int_s},{int(round(frac * 10)):d}"
        return f"{sign}{int_s}"
    if abs_v >= 1:
        return f"{sign}{abs_v:.1f}".replace(".", ",")
    return f"{sign}{abs_v:.4g}".replace(".", ",")


def _fmt_pct(v: float) -> str:
    sign = "+" if v > 0 else ("−" if v < 0 else "")
    return f"{sign}{abs(v):.2f}%".replace(".", ",")


async def _delta_for_pair(
    session,
    pair_id: int,
    since: datetime,
    until: datetime | None = None,
) -> tuple[float, float, float]:
    """Return (delta_abs base, volume base, delta from notional if available as proxy)."""
    q = select(
        func.coalesce(func.sum(DeltaBucket.buy_vol), 0.0),
        func.coalesce(func.sum(DeltaBucket.sell_vol), 0.0),
        func.coalesce(func.sum(DeltaBucket.notional_sum), 0.0),
    ).where(DeltaBucket.pair_id == pair_id, DeltaBucket.bucket_start >= since)
    if until is not None:
        q = q.where(DeltaBucket.bucket_start < until)
    buy_vol, sell_vol, notional = (await session.execute(q)).one()
    buy_vol, sell_vol = float(buy_vol), float(sell_vol)
    delta = buy_vol - sell_vol
    volume = buy_vol + sell_vol
    return delta, volume, float(notional)


async def _price_change(
    session,
    pair_id: int,
    tf: str,
    since: datetime,
) -> tuple[float, float] | None:
    hist = await session.execute(
        select(OhlcCandle)
        .where(
            OhlcCandle.pair_id == pair_id,
            OhlcCandle.timeframe == tf,
            OhlcCandle.open_time >= since - TF_DELTA.get(tf, timedelta(hours=1)),
        )
        .order_by(OhlcCandle.open_time.asc())
    )
    series = list(hist.scalars().all())
    if len(series) < 1:
        return None
    # Prefer candles within window
    in_win = [c for c in series if c.open_time >= since]
    if len(in_win) >= 2:
        o, c = in_win[0].open, in_win[-1].close
    elif len(series) >= 2:
        o, c = series[0].open, series[-1].close
    else:
        o, c = series[0].open, series[0].close
    if not o:
        return None
    abs_ch = c - o
    pct = abs_ch / o * 100.0
    return abs_ch, pct


async def build_otchet_v2(coin: str, hub: "RuntimeHub") -> dict[str, Any]:
    """
    Build structured report for all spot-supporting exchanges.
    Uses DB for watched pairs; zeros for others.
    """
    coin = coin.upper().strip()
    config = get_app_config()
    timeframes = list(config.get("report", "timeframes", default=["15m", "1h", "4h", "1d", "1w"]))
    now = datetime.now(timezone.utc)
    factory = get_session_factory()

    spot_exchanges = await hub.exchanges_with_spot(coin)
    if not spot_exchanges:
        return {"coin": coin, "timeframes": [], "cumulative": None, "empty": True}

    async with factory() as session:
        result = await session.execute(
            select(WatchedPair).where(WatchedPair.coin == coin)
        )
        watched = {p.exchange: p for p in result.scalars().all()}

        # Earliest tracking start among CD pairs (or any)
        starts = []
        for p in watched.values():
            starts.append(p.cd_started_at or p.timestamp_added)
        track_start = min(starts) if starts else now

        tf_blocks: list[dict[str, Any]] = []
        monitoring_age = now - track_start if starts else timedelta(0)

        for tf in timeframes:
            need = TF_DELTA[tf]
            # Skip TF entirely if coin hasn't been monitored long enough
            if monitoring_age < need:
                continue
            since = now - need
            rows: list[dict[str, Any]] = []
            price_pcts: list[float] = []
            for exchange, symbol in spot_exchanges:
                pair = watched.get(exchange)
                delta_abs = 0.0
                delta_pct = 0.0
                price_pct = 0.0
                if pair is not None:
                    age = now - (pair.cd_started_at or pair.timestamp_added)
                    if age >= need:
                        d, vol, _ = await _delta_for_pair(session, pair.id, since)
                        delta_abs = d
                        delta_pct = (d / vol * 100.0) if vol > 0 else 0.0
                        pc = await _price_change(session, pair.id, tf, since)
                        if pc:
                            price_pct = pc[1]
                            price_pcts.append(price_pct)
                rows.append(
                    {
                        "exchange": exchange,
                        "symbol": symbol,
                        "delta_abs": delta_abs,
                        "delta_pct": delta_pct,
                        "price_pct": price_pct,
                        "watched": pair is not None,
                    }
                )
            # Aggregate
            agg_delta = sum(r["delta_abs"] for r in rows)
            weights = [abs(r["delta_abs"]) for r in rows]
            wsum = sum(weights)
            if wsum > 0:
                agg_pct = sum(r["delta_pct"] * abs(r["delta_abs"]) for r in rows) / wsum
            else:
                agg_pct = 0.0
            header_price_pct = sum(price_pcts) / len(price_pcts) if price_pcts else 0.0
            rows.sort(key=lambda r: abs(r["delta_abs"]), reverse=True)
            tf_blocks.append(
                {
                    "tf": tf,
                    "price_pct": header_price_pct,
                    "agg_delta": agg_delta,
                    "agg_pct": agg_pct,
                    "rows": rows,
                }
            )

        # Cumulative since tracking start
        cum_rows: list[dict[str, Any]] = []
        cum_price_pcts: list[float] = []
        for exchange, symbol in spot_exchanges:
            pair = watched.get(exchange)
            delta_abs = 0.0
            delta_pct = 0.0
            price_pct = 0.0
            if pair is not None:
                start = pair.cd_started_at or pair.timestamp_added
                d, vol, _ = await _delta_for_pair(session, pair.id, start)
                ds = await session.get(DeltaState, pair.id)
                if ds:
                    delta_abs = ds.cum_delta
                else:
                    delta_abs = d
                delta_pct = (d / vol * 100.0) if vol > 0 else 0.0
                # price since start using finest available TF
                for tf_try in ("15m", "1h", "1d"):
                    pc = await _price_change(session, pair.id, tf_try, start)
                    if pc:
                        price_pct = pc[1]
                        cum_price_pcts.append(price_pct)
                        break
            cum_rows.append(
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "delta_abs": delta_abs,
                    "delta_pct": delta_pct,
                    "price_pct": price_pct,
                }
            )
        cum_rows.sort(key=lambda r: abs(r["delta_abs"]), reverse=True)
        agg_delta = sum(r["delta_abs"] for r in cum_rows)
        weights = [abs(r["delta_abs"]) for r in cum_rows]
        wsum = sum(weights)
        agg_pct = (
            sum(r["delta_pct"] * abs(r["delta_abs"]) for r in cum_rows) / wsum if wsum else 0.0
        )
        header_price = sum(cum_price_pcts) / len(cum_price_pcts) if cum_price_pcts else 0.0

        return {
            "coin": coin,
            "empty": False,
            "timeframes": tf_blocks,
            "cumulative": {
                "since": track_start,
                "price_pct": header_price,
                "agg_delta": agg_delta,
                "agg_pct": agg_pct,
                "rows": cum_rows,
            },
        }


async def build_custom_cd_report(
    coin: str,
    hub: "RuntimeHub",
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """
    Custom interval report. Uses DB buckets when available;
    otherwise live-fetches trades/OHLCV without persisting.
    """
    coin = coin.upper().strip()
    now = datetime.now(timezone.utc)
    end = min(end, now)
    factory = get_session_factory()
    spot_exchanges = await hub.exchanges_with_spot(coin)

    async with factory() as session:
        result = await session.execute(select(WatchedPair).where(WatchedPair.coin == coin))
        watched = {p.exchange: p for p in result.scalars().all()}

    rows: list[dict[str, Any]] = []
    for exchange, symbol in spot_exchanges:
        pair = watched.get(exchange)
        delta_abs = 0.0
        delta_pct = 0.0
        price_pct = 0.0
        used_live = False

        track_start = None
        if pair is not None:
            track_start = pair.cd_started_at or pair.timestamp_added

        need_live = pair is None or track_start is None or start < track_start

        if pair is not None and not need_live:
            async with factory() as session:
                d, vol, _ = await _delta_for_pair(session, pair.id, start, end)
                delta_abs = d
                delta_pct = (d / vol * 100.0) if vol > 0 else 0.0
                # price from stored candles
                for tf_try in ("15m", "1h", "1d"):
                    pc = await _price_change(session, pair.id, tf_try, start)
                    if pc:
                        price_pct = pc[1]
                        break
        else:
            used_live = True
            adapter = hub.get_adapter(exchange)
            if adapter is None:
                rows.append(
                    {
                        "exchange": exchange,
                        "delta_abs": 0.0,
                        "delta_pct": 0.0,
                        "price_pct": 0.0,
                        "live": True,
                    }
                )
                continue
            # Live OHLCV for price (no DB write)
            try:
                since_ms = int(start.timestamp() * 1000)
                candles = await adapter.fetch_ohlcv(symbol, "15m", since=since_ms, limit=500)
                candles = [c for c in candles if start <= c.open_time < end]
                if len(candles) >= 2 and candles[0].open:
                    price_pct = (candles[-1].close / candles[0].open - 1.0) * 100.0
            except Exception:
                pass
            # Live trades for delta (no DB write)
            try:
                trades = await adapter.fetch_trades_range(symbol, int(start.timestamp() * 1000))
                buy = sell = 0.0
                for t in trades:
                    if t.timestamp < start or t.timestamp >= end:
                        continue
                    if t.side == "buy":
                        buy += t.amount
                    else:
                        sell += t.amount
                delta_abs = buy - sell
                vol = buy + sell
                delta_pct = (delta_abs / vol * 100.0) if vol > 0 else 0.0
            except Exception:
                pass

        rows.append(
            {
                "exchange": exchange,
                "delta_abs": delta_abs,
                "delta_pct": delta_pct,
                "price_pct": price_pct,
                "live": used_live,
            }
        )

    rows.sort(key=lambda r: abs(r["delta_abs"]), reverse=True)
    agg_delta = sum(r["delta_abs"] for r in rows)
    weights = [abs(r["delta_abs"]) for r in rows]
    wsum = sum(weights)
    agg_pct = sum(r["delta_pct"] * abs(r["delta_abs"]) for r in rows) / wsum if wsum else 0.0
    price_vals = [r["price_pct"] for r in rows if r["price_pct"]]
    header_price = sum(price_vals) / len(price_vals) if price_vals else 0.0

    return {
        "coin": coin,
        "start": start,
        "end": end,
        "price_pct": header_price,
        "agg_delta": agg_delta,
        "agg_pct": agg_pct,
        "rows": rows,
    }


def format_otchet_mono(data: dict[str, Any]) -> str:
    if data.get("empty"):
        return f"По {data.get('coin', '?')} спотовых пар на биржах не найдено."

    coin = data["coin"]
    lines: list[str] = [f"Отчёт по {coin}"]

    def _row(name: str, delta: float, pct: float) -> str:
        return f"{name:<12}| {_fmt_num(delta):>12} | {_fmt_pct(pct):>9}"

    def _block(title: str, price_pct: float, agg_d: float, agg_p: float, rows: list[dict]) -> None:
        lines.append("")
        lines.append(f"{title} {_fmt_pct(price_pct)}")
        lines.append(_row("агр", agg_d, agg_p))
        for r in rows:
            lines.append(_row(display_name(r["exchange"]), r["delta_abs"], r["delta_pct"]))

    for block in data["timeframes"]:
        _block(
            block["tf"],
            block["price_pct"],
            block["agg_delta"],
            block["agg_pct"],
            block["rows"],
        )

    cum = data.get("cumulative")
    if cum:
        since_s = format_dt(cum["since"])
        _block(
            f"Накопленная дельта с {since_s}",
            cum["price_pct"],
            cum["agg_delta"],
            cum["agg_pct"],
            cum["rows"],
        )

    return "\n".join(lines)


def format_custom_cd_mono(data: dict[str, Any]) -> str:
    coin = data["coin"]
    start_s = format_dt(data["start"])
    end_s = format_dt(data["end"])

    def _row(name: str, delta: float, pct: float, mark: str = "") -> str:
        return f"{name:<12}| {_fmt_num(delta):>12} | {_fmt_pct(pct):>9}{mark}"

    lines = [
        f"Отчёт по {coin}",
        f"Интервал {start_s} — {end_s}",
        f"Изменение цены {_fmt_pct(data['price_pct'])}",
        _row("агр", data["agg_delta"], data["agg_pct"]),
    ]
    for r in data["rows"]:
        mark = " *" if r.get("live") else ""
        lines.append(_row(display_name(r["exchange"]), r["delta_abs"], r["delta_pct"], mark))
    if any(r.get("live") for r in data["rows"]):
        lines.append("")
        lines.append("* данные подтянуты без сохранения в БД")
    return "\n".join(lines)


# Backward-compat wrapper used by old tests/handlers
async def build_otchet(coin: str) -> list[dict]:
    config = get_app_config()
    timeframes = list(config.get("report", "timeframes", default=["15m", "1h", "4h", "1d", "1w"]))
    factory = get_session_factory()
    now = datetime.now(timezone.utc)
    async with factory() as session:
        result = await session.execute(
            select(WatchedPair).where(WatchedPair.coin == coin.upper()).order_by(WatchedPair.exchange)
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
                since = now - need
                pc = await _price_change(session, pair.id, tf, since)
                if not pc:
                    continue
                d, vol, _ = await _delta_for_pair(session, pair.id, since)
                tf_blocks.append(
                    {
                        "tf": tf,
                        "price_abs": pc[0],
                        "price_pct": pc[1],
                        "delta_abs": d,
                        "delta_pct": (d / vol * 100.0) if vol else 0.0,
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
