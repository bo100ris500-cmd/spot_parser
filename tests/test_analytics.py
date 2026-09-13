from __future__ import annotations

from app.analytics.rsi_divergence import compute_rsi, detect_regular_divergence, find_swings
from app.bot.formatters import parse_add_args
from app.exchanges.ccxt_adapter import normalize_side


def test_normalize_side_ccxt():
    assert normalize_side({"side": "buy"}) == "buy"
    assert normalize_side({"side": "sell"}) == "sell"


def test_normalize_side_binance_maker():
    assert normalize_side({"info": {"m": True}}) == "sell"
    assert normalize_side({"info": {"m": False}}) == "buy"
    assert normalize_side({"info": {"isBuyerMaker": True}}) == "sell"


def test_parse_add_args():
    coin, ex, big, cd, thr = parse_add_args("/add BTC binance big cd")
    assert coin == "BTC" and ex == "binance" and big and cd and thr is None
    coin, ex, big, cd, thr = parse_add_args("/add SOL bybit big:50000")
    assert big and thr == 50000 and not cd
    coin, ex, big, cd, thr = parse_add_args("ETH okx")
    assert coin == "ETH" and ex == "okx" and not big and not cd


def test_compute_rsi_length():
    closes = [float(i) for i in range(1, 50)]
    rsi = compute_rsi(closes, 14)
    assert len(rsi) == len(closes)
    assert rsi[14] is not None
    assert all(0 <= x <= 100 for x in rsi if x is not None)


def test_find_swings():
    vals = [1, 3, 2, 5, 4, 7, 6]
    highs = find_swings(vals, kind="high", min_distance=1)
    assert 1 in highs or 3 in highs or 5 in highs


def test_bearish_divergence_smoke():
    # Construct simple series with higher highs in price and lower RSI-ish pattern
    n = 80
    closes = []
    highs = []
    lows = []
    for i in range(n):
        # two peaks: first lower price peak with higher momentum later inverted
        base = 100 + i * 0.1
        if i == 40:
            base = 120
        if i == 70:
            base = 130
        closes.append(base)
        highs.append(base + 1)
        lows.append(base - 1)
    # May or may not detect depending on swings; just ensure no exception
    detect_regular_divergence(
        highs, lows, closes, timeframe="1h", rsi_period=14, swing_lookback=8, min_swing_distance=2
    )
