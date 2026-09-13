from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from sqlalchemy import select

from app.config import get_app_config
from app.db.models import DeltaBucket, OhlcCandle, WatchedPair
from app.db.session import get_session_factory
from app.exchanges.names import display_name
from app.utils.timefmt import DISPLAY_TZ, format_dt, to_display

TF_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}

TF_LABELS = ["1m", "5m", "15m", "1h", "4h", "1d"]


def align_ceil(dt: datetime, tf_sec: int) -> datetime:
    """Next whole TF boundary at or after dt (keep if already aligned)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = int(dt.timestamp())
    if ts % tf_sec == 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime.fromtimestamp(((ts // tf_sec) + 1) * tf_sec, tz=timezone.utc)


def align_floor(dt: datetime, tf_sec: int) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = int(dt.timestamp())
    aligned = (ts // tf_sec) * tf_sec
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def parse_interval_text(
    text: str,
    *,
    observation_start: datetime,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """
    Parse optional range.
    Returns (start, end) in UTC, or (None, None) for 'all time'.
    """
    raw = (text or "").strip().lower()
    now = now or datetime.now(timezone.utc)
    if not raw or raw in {"все", "all", "skip", "-", "пропуск", "пропустить"}:
        return None, None

    # duration: 6h / 2d / 30m
    if raw[-1] in "hdm" and raw[:-1].replace(".", "", 1).isdigit():
        num = float(raw[:-1])
        unit = raw[-1]
        delta = {
            "m": timedelta(minutes=num),
            "h": timedelta(hours=num),
            "d": timedelta(days=num),
        }[unit]
        return now - delta, now

    # "from - to" (require spaces around separator to keep ISO dates intact)
    if " - " in text or " — " in text:
        sep = " — " if " — " in text else " - "
        parts = text.split(sep, 1)
        from app.utils.timefmt import parse_user_dt

        start = parse_user_dt(parts[0].strip())
        end = parse_user_dt(parts[1].strip())
        return start, end

    from app.utils.timefmt import parse_user_dt

    start = parse_user_dt(text)
    return start, now


async def build_cd_series(
    *,
    coin: str,
    exchange: str | None,
    timeframe: str,
    range_start: datetime | None,
    range_end: datetime | None,
) -> dict[str, Any]:
    """
    Build aligned series for chart.
    exchange=None → aggregate all CD-tracked exchanges for the coin.
    """
    if timeframe not in TF_SECONDS:
        raise ValueError(f"Неизвестный таймфрейм: {timeframe}")
    tf_sec = TF_SECONDS[timeframe]
    coin = coin.upper().strip()
    factory = get_session_factory()
    now = datetime.now(timezone.utc)

    async with factory() as session:
        q = select(WatchedPair).where(WatchedPair.coin == coin, WatchedPair.flag_cd.is_(True))
        if exchange:
            q = q.where(WatchedPair.exchange == exchange.lower())
        result = await session.execute(q)
        pairs = list(result.scalars().all())
        if not pairs:
            raise ValueError(f"Нет отслеживания кумулятивной дельты для {coin}")

        # Observation start = earliest cd_started_at (or timestamp_added)
        starts = []
        for p in pairs:
            starts.append(p.cd_started_at or p.timestamp_added)
        obs_start = min(starts)

        chart_start = align_ceil(obs_start, tf_sec)
        chart_end = align_floor(now, tf_sec)

        if range_start is not None:
            chart_start = max(chart_start, align_ceil(range_start, tf_sec))
        if range_end is not None:
            chart_end = min(chart_end, align_floor(range_end, tf_sec))

        if chart_end <= chart_start:
            raise ValueError("Недостаточно данных за выбранный интервал")

        pair_ids = [p.id for p in pairs]

        # Load 1-minute buckets and aggregate to TF
        buckets = await session.execute(
            select(DeltaBucket)
            .where(
                DeltaBucket.pair_id.in_(pair_ids),
                DeltaBucket.bucket_start >= chart_start - timedelta(seconds=tf_sec),
                DeltaBucket.bucket_start < chart_end,
            )
            .order_by(DeltaBucket.bucket_start.asc())
        )
        bucket_rows = list(buckets.scalars().all())

        # Prefer OHLC from primary (first by volume of data) exchange for price
        price_pair = pairs[0]
        if exchange:
            price_pair = pairs[0]
        else:
            # Prefer binance if present
            for pref in ("binance", "bybit", "okx"):
                found = next((p for p in pairs if p.exchange == pref), None)
                if found:
                    price_pair = found
                    break

        # Use finest available OHLC timeframe <= requested
        ohlc_tf = timeframe if timeframe in ("1m", "5m", "15m", "1h", "4h", "1d") else "15m"
        candles_q = await session.execute(
            select(OhlcCandle)
            .where(
                OhlcCandle.pair_id == price_pair.id,
                OhlcCandle.timeframe == ohlc_tf,
                OhlcCandle.open_time >= chart_start - timedelta(seconds=tf_sec),
                OhlcCandle.open_time < chart_end,
            )
            .order_by(OhlcCandle.open_time.asc())
        )
        candles = list(candles_q.scalars().all())

        # If no candles for exact TF, try 15m / 1h fallbacks and resample conceptually via close
        if not candles:
            for alt in ("1m", "5m", "15m", "1h", "4h", "1d"):
                if alt == ohlc_tf:
                    continue
                candles_q = await session.execute(
                    select(OhlcCandle)
                    .where(
                        OhlcCandle.pair_id == price_pair.id,
                        OhlcCandle.timeframe == alt,
                        OhlcCandle.open_time >= chart_start - timedelta(seconds=tf_sec),
                        OhlcCandle.open_time < chart_end,
                    )
                    .order_by(OhlcCandle.open_time.asc())
                )
                candles = list(candles_q.scalars().all())
                if candles:
                    ohlc_tf = alt
                    break

    # Build time grid
    times: list[datetime] = []
    t = chart_start
    while t < chart_end:
        times.append(t)
        t = t + timedelta(seconds=tf_sec)
    if not times:
        raise ValueError("Недостаточно данных для графика")

    # Aggregate delta per TF bar
    delta_by_bar: dict[int, float] = {int(tt.timestamp()): 0.0 for tt in times}
    vol_by_bar: dict[int, float] = {int(tt.timestamp()): 0.0 for tt in times}
    for b in bucket_rows:
        ts = int(b.bucket_start.timestamp())
        bar_ts = (ts // tf_sec) * tf_sec
        if bar_ts not in delta_by_bar:
            continue
        d = float(b.buy_vol) - float(b.sell_vol)
        v = float(b.buy_vol) + float(b.sell_vol)
        delta_by_bar[bar_ts] += d
        vol_by_bar[bar_ts] += v

    # Price closes per bar (from candles)
    price_by_bar: dict[int, float] = {}
    ohlc_map: dict[int, tuple[float, float, float, float]] = {}
    for c in candles:
        ts = int(c.open_time.timestamp())
        bar_ts = (ts // tf_sec) * tf_sec
        price_by_bar[bar_ts] = float(c.close)
        if bar_ts in ohlc_map:
            o, h, l, _cl = ohlc_map[bar_ts]
            ohlc_map[bar_ts] = (o, max(h, c.high), min(l, c.low), float(c.close))
        else:
            ohlc_map[bar_ts] = (float(c.open), float(c.high), float(c.low), float(c.close))

    # Forward-fill price
    last_price: float | None = None
    cum = 0.0
    cum_vol = 0.0
    cum_deltas: list[float] = []
    cd_pcts: list[float] = []
    price_pcts: list[float] = []
    price_closes: list[float] = []
    ohlc_rows: list[tuple[float, float, float, float]] = []

    base_price: float | None = None
    for tt in times:
        key = int(tt.timestamp())
        cum += delta_by_bar.get(key, 0.0)
        cum_vol += vol_by_bar.get(key, 0.0)
        cum_deltas.append(cum)
        # CD %: imbalance vs cumulative absolute volume since chart start
        cd_pcts.append((cum / cum_vol * 100.0) if cum_vol > 0 else 0.0)

        if key in price_by_bar:
            last_price = price_by_bar[key]
        if last_price is None and key in ohlc_map:
            last_price = ohlc_map[key][3]
        if last_price is None:
            price_closes.append(float("nan"))
            price_pcts.append(float("nan"))
            ohlc_rows.append((float("nan"),) * 4)
            continue
        if base_price is None:
            base_price = last_price
        price_closes.append(last_price)
        price_pcts.append((last_price / base_price - 1.0) * 100.0 if base_price else 0.0)
        if key in ohlc_map:
            ohlc_rows.append(ohlc_map[key])
        else:
            ohlc_rows.append((last_price, last_price, last_price, last_price))

    label_ex = "агрегированно" if exchange is None else display_name(exchange)
    title = f"{coin} · {label_ex} · {timeframe}"
    subtitle = f"{format_dt(chart_start)} — {format_dt(chart_end)}"

    return {
        "title": title,
        "subtitle": subtitle,
        "times": times,
        "ohlc": ohlc_rows,
        "closes": price_closes,
        "cd_pct": cd_pcts,
        "price_pct": price_pcts,
        "cum_delta": cum_deltas,
        "coin": coin,
        "exchange": exchange,
        "timeframe": timeframe,
    }


def render_cd_chart_png(series: dict[str, Any]) -> bytes:
    """TradingView-like dark chart: price + CD% + price%."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    times_local = [to_display(t) for t in series["times"]]
    ohlc = series["ohlc"]
    cd_pct = series["cd_pct"]
    price_pct = series["price_pct"]

    bg = "#131722"
    grid = "#2a2e39"
    text = "#d1d4dc"
    up = "#26a69a"
    down = "#ef5350"
    accent = "#2962ff"
    accent2 = "#f5a623"

    fig, axes = plt.subplots(
        3,
        1,
        figsize=(12, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1.2, 1.2], "hspace": 0.05},
    )
    fig.patch.set_facecolor(bg)
    for ax in axes:
        ax.set_facecolor(bg)
        ax.tick_params(colors=text, labelsize=8)
        ax.grid(True, color=grid, linewidth=0.6, alpha=0.7)
        for spine in ax.spines.values():
            spine.set_color(grid)

    ax_price, ax_cd, ax_pp = axes

    # Candles
    width = TF_SECONDS.get(series["timeframe"], 3600) / 86400 * 0.7
    for i, t in enumerate(times_local):
        o, h, l, c = ohlc[i]
        if o != o:  # NaN
            continue
        color = up if c >= o else down
        ax_price.vlines(t, l, h, color=color, linewidth=1)
        bottom = min(o, c)
        height = abs(c - o) or (h - l) * 0.01 or 1e-8
        ax_price.add_patch(
            Rectangle(
                (mdates.date2num(t) - width / 2, bottom),
                width,
                height,
                facecolor=color,
                edgecolor=color,
                linewidth=0,
            )
        )

    ax_price.set_ylabel("Цена", color=text, fontsize=9)
    ax_price.set_title(f"{series['title']}\n{series['subtitle']}", color=text, fontsize=11, pad=8)

    ax_cd.plot(times_local, cd_pct, color=accent, linewidth=1.4)
    ax_cd.axhline(0, color=grid, linewidth=0.8)
    ax_cd.set_ylabel("CD %", color=text, fontsize=9)
    ax_cd.fill_between(
        times_local,
        cd_pct,
        0,
        where=[v >= 0 for v in cd_pct],
        color=up,
        alpha=0.25,
        interpolate=True,
    )
    ax_cd.fill_between(
        times_local,
        cd_pct,
        0,
        where=[v < 0 for v in cd_pct],
        color=down,
        alpha=0.25,
        interpolate=True,
    )

    ax_pp.plot(times_local, price_pct, color=accent2, linewidth=1.4)
    ax_pp.axhline(0, color=grid, linewidth=0.8)
    ax_pp.set_ylabel("Цена %", color=text, fontsize=9)
    ax_pp.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=DISPLAY_TZ))
    fig.autofmt_xdate(rotation=25)

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
