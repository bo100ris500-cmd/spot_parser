from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.utils.logging import get_logger

logger = get_logger(__name__)

DivergenceType = Literal["bullish", "bearish"]


@dataclass
class DivergenceSignal:
    divergence_type: DivergenceType
    timeframe: str
    price_a: float
    price_b: float
    rsi_a: float
    rsi_b: float
    index_a: int
    index_b: int


def compute_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    if len(closes) < period + 1:
        return [None] * len(closes)

    rsi: list[float | None] = [None] * len(closes)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    rsi[period] = _rsi(avg_gain, avg_loss)

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i + 1] = _rsi(avg_gain, avg_loss)

    return rsi


def find_swings(
    values: list[float],
    *,
    kind: Literal["high", "low"],
    min_distance: int,
) -> list[int]:
    """Local swing highs/lows with minimum distance between pivots."""
    indices: list[int] = []
    n = len(values)
    if n < 3:
        return indices
    for i in range(1, n - 1):
        if kind == "high":
            if values[i] > values[i - 1] and values[i] >= values[i + 1]:
                if not indices or i - indices[-1] >= min_distance:
                    indices.append(i)
                elif values[i] > values[indices[-1]]:
                    indices[-1] = i
        else:
            if values[i] < values[i - 1] and values[i] <= values[i + 1]:
                if not indices or i - indices[-1] >= min_distance:
                    indices.append(i)
                elif values[i] < values[indices[-1]]:
                    indices[-1] = i
    return indices


def detect_regular_divergence(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    timeframe: str,
    rsi_period: int = 14,
    swing_lookback: int = 8,
    min_swing_distance: int = 3,
) -> DivergenceSignal | None:
    """
    Regular (classic) divergence on the last confirmed closed bar.
    Bearish: price higher high, RSI lower high.
    Bullish: price lower low, RSI higher low.
    """
    rsi = compute_rsi(closes, rsi_period)
    if any(x is None for x in rsi[-5:]):
        return None

    rsi_vals = [float(x) if x is not None else float("nan") for x in rsi]

    swing_highs = find_swings(highs, kind="high", min_distance=min_swing_distance)
    swing_lows = find_swings(lows, kind="low", min_distance=min_swing_distance)

    # require the latest swing to be near the end (confirmed on recent close)
    last_idx = len(closes) - 1

    # Bearish
    recent_highs = [i for i in swing_highs if i <= last_idx][-swing_lookback:]
    if len(recent_highs) >= 2:
        a, b = recent_highs[-2], recent_highs[-1]
        if b >= last_idx - 2:  # divergence confirmed on/near last closed candle
            if highs[b] > highs[a] and rsi_vals[b] < rsi_vals[a]:
                return DivergenceSignal(
                    divergence_type="bearish",
                    timeframe=timeframe,
                    price_a=highs[a],
                    price_b=highs[b],
                    rsi_a=rsi_vals[a],
                    rsi_b=rsi_vals[b],
                    index_a=a,
                    index_b=b,
                )

    # Bullish
    recent_lows = [i for i in swing_lows if i <= last_idx][-swing_lookback:]
    if len(recent_lows) >= 2:
        a, b = recent_lows[-2], recent_lows[-1]
        if b >= last_idx - 2:
            if lows[b] < lows[a] and rsi_vals[b] > rsi_vals[a]:
                return DivergenceSignal(
                    divergence_type="bullish",
                    timeframe=timeframe,
                    price_a=lows[a],
                    price_b=lows[b],
                    rsi_a=rsi_vals[a],
                    rsi_b=rsi_vals[b],
                    index_a=a,
                    index_b=b,
                )

    return None
