from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import AppConfig
from app.exchanges.base import NormalizedTrade
from app.notify.publisher import AlertPublisher
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PairDeltaRuntime:
    cum_delta: float = 0.0
    # (ts, signed_delta, abs_volume)
    events: deque[tuple[float, float, float]] = field(default_factory=deque)
    last_alert_ts: float = 0.0
    dirty: bool = False


class CumDeltaEngine:
    def __init__(self, config: AppConfig, publisher: AlertPublisher) -> None:
        self.config = config
        self.publisher = publisher
        self._state: dict[int, PairDeltaRuntime] = {}
        self._meta: dict[int, dict[str, Any]] = {}
        # in-memory minute buckets for flush: pair_id -> bucket_key -> accum
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
            self._state[pair_id] = PairDeltaRuntime(cum_delta=cum_delta)

    def get_cum_delta(self, pair_id: int) -> float:
        rt = self._state.get(pair_id)
        return rt.cum_delta if rt else 0.0

    def pop_dirty_states(self) -> dict[int, float]:
        out: dict[int, float] = {}
        for pid, rt in self._state.items():
            if rt.dirty:
                out[pid] = rt.cum_delta
                rt.dirty = False
        return out

    def pop_buckets(self) -> list[dict[str, Any]]:
        """Return and clear in-memory buckets for persistence."""
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

    async def on_trade(self, pair_id: int, trade: NormalizedTrade) -> None:
        meta = self._meta.get(pair_id)
        if not meta:
            return

        # Always accumulate for reports while pair is watched; spike alerts only if enabled (cd)
        rt = self._state.setdefault(pair_id, PairDeltaRuntime())
        signed = trade.amount if trade.side == "buy" else -trade.amount
        rt.cum_delta += signed
        rt.dirty = True

        now = time.time()
        abs_vol = trade.amount
        rt.events.append((now, signed, abs_vol))

        cutoff = now - 3600
        while rt.events and rt.events[0][0] < cutoff:
            rt.events.popleft()

        bucket_sec = int(self.config.get("bucket", "seconds", default=60))
        start = int(now // bucket_sec) * bucket_sec
        acc = self._buckets[pair_id].setdefault(
            start, {"buy_vol": 0.0, "sell_vol": 0.0, "trade_count": 0.0, "notional_sum": 0.0}
        )
        if trade.side == "buy":
            acc["buy_vol"] += trade.amount
        else:
            acc["sell_vol"] += trade.amount
        acc["trade_count"] += 1
        acc["notional_sum"] += trade.cost

        if meta.get("alerts_enabled"):
            await self._maybe_alert(pair_id, rt, now)

    async def _maybe_alert(self, pair_id: int, rt: PairDeltaRuntime, now: float) -> None:
        window = float(self.config.get("cd", "alert_window_sec", default=300))
        pct = float(self.config.get("cd", "alert_pct", default=5))
        cooldown = float(self.config.get("cd", "cooldown_sec", default=600))

        if now - rt.last_alert_ts < cooldown:
            return

        delta_win = sum(d for ts, d, _ in rt.events if ts >= now - window)
        vol_hour = sum(a for _, _, a in rt.events)
        if vol_hour <= 0:
            return

        # compare |5m delta| to average hourly volume * N%
        # use last hour total volume as baseline
        threshold = vol_hour * (pct / 100.0)
        if abs(delta_win) < threshold:
            return

        meta = self._meta[pair_id]
        direction = "buy_pressure" if delta_win > 0 else "sell_pressure"
        alert = {
            "type": "cd_spike",
            "pair_id": pair_id,
            "exchange": meta["exchange"],
            "symbol": meta["symbol"],
            "coin": meta["coin"],
            "delta_window": delta_win,
            "window_sec": window,
            "direction": direction,
            "cum_delta": rt.cum_delta,
            "ts": datetime.now(timezone.utc).isoformat(),
            "fingerprint": f"cd:{pair_id}:{int(now // cooldown)}",
        }
        rt.last_alert_ts = now
        await self.publisher.publish(alert)
        logger.info(
            "CD spike %s %s delta_5m=%.4f dir=%s",
            meta["exchange"],
            meta["symbol"],
            delta_win,
            direction,
        )
