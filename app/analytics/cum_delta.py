from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.config import AppConfig
from app.exchanges.base import NormalizedTrade
from app.notify.publisher import AlertPublisher
from app.utils.logging import get_logger

logger = get_logger(__name__)

TF_SEC = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "24h": 24 * 60 * 60,
    # aliases
    "1d": 24 * 60 * 60,
}


@dataclass
class PairDeltaRuntime:
    cum_delta_base: float = 0.0
    cum_delta_usd: float = 0.0
    # (ts, signed_usd, abs_usd, price)
    events: deque[tuple[float, float, float, float]] = field(default_factory=deque)
    last_alert_ts: dict[str, float] = field(default_factory=dict)
    last_price: float = 0.0
    dirty: bool = False


class CumDeltaEngine:
    def __init__(
        self,
        config: AppConfig,
        publisher: AlertPublisher,
        *,
        liquidity_ranker: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.config = config
        self.publisher = publisher
        self._liquidity_ranker = liquidity_ranker
        self._state: dict[int, PairDeltaRuntime] = {}
        self._meta: dict[int, dict[str, Any]] = {}
        self._buckets: dict[int, dict[int, dict[str, float]]] = defaultdict(dict)

    def set_pair_meta(
        self,
        pair_id: int,
        *,
        exchange: str,
        symbol: str,
        coin: str,
        alerts_enabled: bool,
        cum_delta: float = 0.0,
    ) -> None:
        self._meta[pair_id] = {
            "exchange": exchange,
            "symbol": symbol,
            "coin": coin,
            "alerts_enabled": alerts_enabled,
        }
        if pair_id not in self._state:
            self._state[pair_id] = PairDeltaRuntime(cum_delta_base=cum_delta, cum_delta_usd=0.0)

    def reset_pair(self, pair_id: int, cum_delta: float = 0.0) -> None:
        self._state[pair_id] = PairDeltaRuntime(cum_delta_base=cum_delta, cum_delta_usd=0.0)
        self._buckets.pop(pair_id, None)

    def clear_pair(self, pair_id: int) -> None:
        self._meta.pop(pair_id, None)
        self._state.pop(pair_id, None)
        self._buckets.pop(pair_id, None)

    def get_cum_delta(self, pair_id: int) -> float:
        rt = self._state.get(pair_id)
        return rt.cum_delta_base if rt else 0.0

    def get_cum_delta_usd(self, pair_id: int) -> float:
        rt = self._state.get(pair_id)
        return rt.cum_delta_usd if rt else 0.0

    def pop_dirty_states(self) -> dict[int, float]:
        out: dict[int, float] = {}
        for pid, rt in self._state.items():
            if rt.dirty:
                out[pid] = rt.cum_delta_base
                rt.dirty = False
        return out

    def pop_buckets(self) -> list[dict[str, Any]]:
        bucket_sec = int(self.config.get("bucket", "seconds", default=60))
        rows: list[dict[str, Any]] = []
        for pair_id, buckets in list(self._buckets.items()):
            for start_ts, acc in buckets.items():
                rows.append(
                    {
                        "pair_id": pair_id,
                        "bucket_start": datetime.fromtimestamp(start_ts, tz=timezone.utc),
                        "bucket_seconds": bucket_sec,
                        "buy_vol": acc.get("buy_vol", 0.0),
                        "sell_vol": acc.get("sell_vol", 0.0),
                        "trade_count": int(acc.get("trade_count", 0)),
                        "notional_sum": acc.get("notional_sum", 0.0),
                    }
                )
            self._buckets[pair_id] = {}
        return rows

    @staticmethod
    def _window_stats(
        rt: PairDeltaRuntime, now: float, window: float
    ) -> tuple[float, float, float, float] | None:
        """Return (delta_usd, vol_usd, price_pct, last_price) or None."""
        events = [e for e in rt.events if e[0] >= now - window]
        if not events:
            return None
        delta_usd = sum(e[1] for e in events)
        vol_usd = sum(e[2] for e in events)
        if vol_usd <= 0:
            return None
        first_price = events[0][3]
        last_price = events[-1][3]
        if first_price <= 0:
            price_pct = 0.0
        else:
            price_pct = (last_price / first_price - 1.0) * 100.0
        return delta_usd, vol_usd, price_pct, last_price

    async def on_trade(self, pair_id: int, trade: NormalizedTrade) -> None:
        meta = self._meta.get(pair_id)
        if not meta or not meta.get("alerts_enabled"):
            return

        rt = self._state.setdefault(pair_id, PairDeltaRuntime())
        trade_ts = trade.timestamp.timestamp() if trade.timestamp else time.time()
        signed_base = trade.amount if trade.side == "buy" else -trade.amount
        signed_usd = trade.cost if trade.side == "buy" else -trade.cost
        rt.cum_delta_base += signed_base
        rt.cum_delta_usd += signed_usd
        rt.last_price = trade.price
        rt.dirty = True

        now = time.time()
        rt.events.append((now, signed_usd, abs(trade.cost), float(trade.price)))

        hist = float(self.config.get("cd", "event_history_sec", default=90000))
        cutoff = now - hist
        while rt.events and rt.events[0][0] < cutoff:
            rt.events.popleft()

        bucket_sec = int(self.config.get("bucket", "seconds", default=60))
        start = int(trade_ts // bucket_sec) * bucket_sec
        acc = self._buckets[pair_id].setdefault(
            start, {"buy_vol": 0.0, "sell_vol": 0.0, "trade_count": 0.0, "notional_sum": 0.0}
        )
        if trade.side == "buy":
            acc["buy_vol"] += trade.amount
        else:
            acc["sell_vol"] += trade.amount
        acc["trade_count"] += 1
        acc["notional_sum"] += trade.cost

        await self._maybe_alert(pair_id, rt, now)

    async def _maybe_alert(self, pair_id: int, rt: PairDeltaRuntime, now: float) -> None:
        pct = float(self.config.get("cd", "alert_pct", default=40))
        cooldown = float(self.config.get("cd", "cooldown_sec", default=600))
        combo_cooldown = float(self.config.get("cd", "combo_cooldown_sec", default=cooldown))
        price_max = float(self.config.get("cd", "combo_max_price_pct", default=5))
        tfs = list(
            self.config.get(
                "cd",
                "alert_timeframes",
                default=["15m", "30m", "1h", "4h", "12h", "24h"],
            )
        )

        meta = self._meta[pair_id]
        hits: dict[str, dict[str, Any]] = {}

        for tf in tfs:
            window = float(TF_SEC.get(tf, 0))
            if window <= 0:
                continue
            last = rt.last_alert_ts.get(tf, 0.0)
            if now - last < cooldown:
                continue
            stats = self._window_stats(rt, now, window)
            if stats is None:
                continue
            delta_usd, vol_usd, price_pct, last_price = stats
            imbalance_pct = abs(delta_usd) / vol_usd * 100.0
            if imbalance_pct < pct:
                continue
            hits[tf] = {
                "delta_usd": delta_usd,
                "vol_usd": vol_usd,
                "imbalance_pct": imbalance_pct,
                "price_pct": price_pct,
                "price": last_price,
                "window_sec": int(window),
                "direction": "buy_pressure" if delta_usd > 0 else "sell_pressure",
            }

        if not hits:
            return

        # COMBO: 15m + 1h both hit, |Δprice| over 1h < threshold
        combo_ready = (
            "15m" in hits
            and "1h" in hits
            and abs(hits["1h"]["price_pct"]) < price_max
            and now - rt.last_alert_ts.get("combo", 0.0) >= combo_cooldown
        )

        if combo_ready:
            h15, h1h = hits["15m"], hits["1h"]
            # Prefer stronger absolute delta for headline numbers
            primary = h1h if abs(h1h["delta_usd"]) >= abs(h15["delta_usd"]) else h15
            alert = {
                "type": "cd_combo",
                "pair_id": pair_id,
                "exchange": meta["exchange"],
                "symbol": meta["symbol"],
                "coin": meta["coin"],
                "delta_window": primary["delta_usd"],
                "delta_15m": h15["delta_usd"],
                "delta_1h": h1h["delta_usd"],
                "imbalance_pct": primary["imbalance_pct"],
                "imbalance_15m": h15["imbalance_pct"],
                "imbalance_1h": h1h["imbalance_pct"],
                "price_pct": h1h["price_pct"],
                "price": h1h["price"],
                "direction": primary["direction"],
                "cum_delta": rt.cum_delta_usd,
                "timeframe": "15m+1h",
                "window_sec": h1h["window_sec"],
                "pushover": True,
                "pushover_priority": 2,
                "ts": datetime.now(timezone.utc).isoformat(),
                "fingerprint": f"cdcombo:{pair_id}:{int(now // combo_cooldown)}",
            }
            rt.last_alert_ts["combo"] = now
            rt.last_alert_ts["15m"] = now
            rt.last_alert_ts["1h"] = now
            await self.publisher.publish(alert)
            logger.info(
                "CD COMBO %s %s imb15=%.1f imb1h=%.1f price%%=%.2f",
                meta["exchange"],
                meta["symbol"],
                h15["imbalance_pct"],
                h1h["imbalance_pct"],
                h1h["price_pct"],
            )
            # Still emit other TFs (30m, 4h, …) except 15m/1h
            hits.pop("15m", None)
            hits.pop("1h", None)

        for tf, h in hits.items():
            alert = {
                "type": "cd_spike",
                "pair_id": pair_id,
                "exchange": meta["exchange"],
                "symbol": meta["symbol"],
                "coin": meta["coin"],
                "delta_window": h["delta_usd"],
                "window_sec": h["window_sec"],
                "timeframe": tf,
                "imbalance_pct": h["imbalance_pct"],
                "price_pct": h["price_pct"],
                "price": h["price"],
                "direction": h["direction"],
                "cum_delta": rt.cum_delta_usd,
                "ts": datetime.now(timezone.utc).isoformat(),
                "fingerprint": f"cd:{pair_id}:{tf}:{int(now // cooldown)}",
            }
            rt.last_alert_ts[tf] = now
            await self.publisher.publish(alert)
            logger.info(
                "CD spike %s %s tf=%s delta$=%.2f imb=%.1f price%%=%.2f",
                meta["exchange"],
                meta["symbol"],
                tf,
                h["delta_usd"],
                h["imbalance_pct"],
                h["price_pct"],
            )
