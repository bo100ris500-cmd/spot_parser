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


@dataclass
class _AggPending:
    side: str
    amount: float = 0.0
    cost: float = 0.0
    price_sum: float = 0.0
    count: int = 0
    last_ts: float = 0.0
    trade_ids: set[str] = field(default_factory=set)


class BigTradeDetector:
    """
    Big-trade detector for all watched exchanges.

    Threshold is taken from the most liquid watched exchange for the coin
    (adaptive avg×K or absolute big:USD on that venue), then applied to
    trades on every exchange — so alerts fire on all venues, but with one
    shared liquidity-based bar.
    """

    def __init__(
        self,
        config: AppConfig,
        publisher: AlertPublisher,
        *,
        most_liquid_exchange: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self.config = config
        self.publisher = publisher
        self._most_liquid_exchange = most_liquid_exchange
        # pair_id -> deque of (ts, usd_volume)
        self._samples: dict[int, deque[tuple[float, float]]] = defaultdict(deque)
        self._seen_ids: dict[int, set[str]] = defaultdict(set)
        self._pending: dict[tuple[int, str], _AggPending] = {}
        self._meta: dict[int, dict[str, Any]] = {}

    def set_pair_meta(
        self,
        pair_id: int,
        *,
        exchange: str,
        symbol: str,
        coin: str,
        enabled: bool,
        threshold_usd: float | None,
    ) -> None:
        self._meta[pair_id] = {
            "exchange": exchange,
            "symbol": symbol,
            "coin": coin,
            "enabled": enabled,
            "threshold_usd": threshold_usd,
        }
        if not enabled:
            self._pending.pop((pair_id, "buy"), None)
            self._pending.pop((pair_id, "sell"), None)

    def clear_pair(self, pair_id: int) -> None:
        self._meta.pop(pair_id, None)
        self._samples.pop(pair_id, None)
        self._seen_ids.pop(pair_id, None)
        self._pending.pop((pair_id, "buy"), None)
        self._pending.pop((pair_id, "sell"), None)

    def _ref_pair_id(self, coin: str, preferred_exchange: str | None) -> int | None:
        candidates = [
            pid
            for pid, m in self._meta.items()
            if m.get("enabled") and m.get("coin") == coin
        ]
        if not candidates:
            return None
        if preferred_exchange:
            for pid in candidates:
                if self._meta[pid]["exchange"] == preferred_exchange:
                    return pid
        return candidates[0]

    async def _shared_threshold(self, coin: str, fallback_cost: float) -> float:
        """Threshold from most liquid big-watched exchange for this coin."""
        window_min = float(self.config.get("big", "window_minutes", default=60))
        k = float(self.config.get("big", "k_multiplier", default=15))

        preferred: str | None = None
        use_liq = bool(self.config.get("big", "threshold_from_most_liquid", default=True))
        if use_liq and self._most_liquid_exchange is not None:
            try:
                preferred = await self._most_liquid_exchange(coin)
            except Exception as exc:
                logger.warning("most-liquid lookup failed for %s: %s", coin, exc)

        ref_pid = self._ref_pair_id(coin, preferred)
        if ref_pid is None:
            return max(fallback_cost * k, 1000.0)

        meta = self._meta[ref_pid]
        absolute = meta.get("threshold_usd")
        if absolute is not None:
            return float(absolute)

        now = time.time()
        samples = self._samples[ref_pid]
        cutoff = now - window_min * 60
        while samples and samples[0][0] < cutoff:
            samples.popleft()

        if len(samples) < 10:
            # Warm-up on reference venue: keep a sane floor
            if samples:
                avg = sum(v for _, v in samples) / len(samples)
            else:
                avg = fallback_cost
            return max(avg * k, 1000.0)

        avg = sum(v for _, v in samples) / len(samples)
        return avg * k

    async def on_trade(self, pair_id: int, trade: NormalizedTrade) -> None:
        meta = self._meta.get(pair_id)
        if not meta or not meta.get("enabled"):
            return

        seen = self._seen_ids[pair_id]
        if trade.trade_id in seen:
            return
        seen.add(trade.trade_id)
        if len(seen) > 5000:
            self._seen_ids[pair_id] = set(list(seen)[-2500:])

        now = time.time()
        agg_ms = float(self.config.get("big", "aggregate_ms", default=1500))
        window_min = float(self.config.get("big", "window_minutes", default=60))

        # Always record samples on this venue (reference venue needs its own history)
        samples = self._samples[pair_id]
        samples.append((now, trade.cost))
        cutoff = now - window_min * 60
        while samples and samples[0][0] < cutoff:
            samples.popleft()

        threshold = await self._shared_threshold(meta["coin"], trade.cost)

        if trade.cost < threshold:
            await self._flush_expired(pair_id, now, agg_ms)
            return

        key = (pair_id, trade.side)
        pending = self._pending.get(key)
        if pending is None:
            self._pending[key] = _AggPending(
                side=trade.side,
                amount=trade.amount,
                cost=trade.cost,
                price_sum=trade.price * trade.amount,
                count=1,
                last_ts=now,
                trade_ids={trade.trade_id},
            )
        else:
            if now - pending.last_ts <= agg_ms / 1000.0:
                pending.amount += trade.amount
                pending.cost += trade.cost
                pending.price_sum += trade.price * trade.amount
                pending.count += 1
                pending.last_ts = now
                pending.trade_ids.add(trade.trade_id)
            else:
                await self._emit(pair_id, pending)
                self._pending[key] = _AggPending(
                    side=trade.side,
                    amount=trade.amount,
                    cost=trade.cost,
                    price_sum=trade.price * trade.amount,
                    count=1,
                    last_ts=now,
                    trade_ids={trade.trade_id},
                )

        await self._flush_expired(pair_id, now, agg_ms)

    async def _flush_expired(self, pair_id: int, now: float, agg_ms: float) -> None:
        for side in ("buy", "sell"):
            key = (pair_id, side)
            pending = self._pending.get(key)
            if pending and now - pending.last_ts > agg_ms / 1000.0:
                await self._emit(pair_id, pending)
                del self._pending[key]

    async def _emit(self, pair_id: int, pending: _AggPending) -> None:
        meta = self._meta[pair_id]
        avg_price = pending.price_sum / pending.amount if pending.amount else 0.0
        alert = {
            "type": "big_trade",
            "pair_id": pair_id,
            "exchange": meta["exchange"],
            "symbol": meta["symbol"],
            "coin": meta["coin"],
            "side": pending.side,
            "amount": pending.amount,
            "cost_usd": pending.cost,
            "price": avg_price,
            "trades_aggregated": pending.count,
            "ts": datetime.now(timezone.utc).isoformat(),
            "fingerprint": f"big:{pair_id}:{pending.side}:{min(pending.trade_ids)}",
        }
        await self.publisher.publish(alert)
        logger.info(
            "BIG %s %s %s amount=%.4f usd=%.2f",
            meta["exchange"],
            meta["symbol"],
            pending.side,
            pending.amount,
            pending.cost,
        )
