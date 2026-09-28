from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from app.config import AppConfig
from app.exchanges.base import NormalizedTrade
from app.notify.publisher import AlertPublisher
from app.utils.logging import get_logger

logger = get_logger(__name__)

TF_SEC = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


@dataclass
class PairDeltaRuntime:
    cum_delta_base: float = 0.0
    cum_delta_usd: float = 0.0
    # (ts, signed_usd, abs_usd)
    events: deque[tuple[float, float, float]] = field(default_factory=deque)
    last_alert_ts: dict[str, float] = field(default_factory=dict)
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
        rt.dirty = True

        now = time.time()
        rt.events.append((now, signed_usd, abs(trade.cost)))

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
        pct = float(self.config.get("cd", "alert_pct", default=20))
        cooldown = float(self.config.get("cd", "cooldown_sec", default=600))
        tfs = list(self.config.get("cd", "alert_timeframes", default=["5m", "15m", "1h", "4h", "1d"]))

        meta = self._meta[pair_id]
        for tf in tfs:
            window = float(TF_SEC.get(tf, 0))
            if window <= 0:
                continue
            last = rt.last_alert_ts.get(tf, 0.0)
            if now - last < cooldown:
                continue

            delta_usd = sum(d for ts, d, _ in rt.events if ts >= now - window)
            vol_usd = sum(a for ts, _, a in rt.events if ts >= now - window)
            if vol_usd <= 0:
                continue

            imbalance_pct = abs(delta_usd) / vol_usd * 100.0
            if imbalance_pct < pct:
                continue

            direction = "buy_pressure" if delta_usd > 0 else "sell_pressure"
            alert = {
                "type": "cd_spike",
                "pair_id": pair_id,
                "exchange": meta["exchange"],
                "symbol": meta["symbol"],
                "coin": meta["coin"],
                "delta_window": delta_usd,
                "window_sec": int(window),
                "timeframe": tf,
                "imbalance_pct": imbalance_pct,
                "direction": direction,
                "cum_delta": rt.cum_delta_usd,
                "ts": datetime.now(timezone.utc).isoformat(),
                "fingerprint": f"cd:{pair_id}:{tf}:{int(now // cooldown)}",
            }
            rt.last_alert_ts[tf] = now
            await self.publisher.publish(alert)
            logger.info(
                "CD spike %s %s tf=%s delta_usd=%.2f pct=%.2f",
                meta["exchange"],
                meta["symbol"],
                tf,
                delta_usd,
                imbalance_pct,
            )
