from __future__ import annotations

from datetime import datetime
from typing import Any


def format_pair_list(pairs: list[Any]) -> str:
    if not pairs:
        return "Список пуст. Добавьте пару: /add BTC binance big cd"
    lines = ["Отслеживаемые пары:"]
    for p in pairs:
        flags = []
        if p.flag_big:
            flags.append("big" + (f":{int(p.big_threshold_usd)}" if p.big_threshold_usd else ""))
        if p.flag_cd:
            flags.append("cd")
        flag_s = " ".join(flags) if flags else "—"
        added = p.timestamp_added.strftime("%Y-%m-%d %H:%M UTC") if p.timestamp_added else "?"
        lines.append(f"• {p.exchange} — {p.coin} — {flag_s} — {added}")
    return "\n".join(lines)


def format_big_alert(alert: dict[str, Any]) -> str:
    side = "BUY" if alert.get("side") == "buy" else "SELL"
    return (
        f"🐋 Крупная сделка ({side})\n"
        f"Биржа: {alert.get('exchange')}\n"
        f"Пара: {alert.get('symbol')}\n"
        f"Объём: {alert.get('amount'):.6g} (~${alert.get('cost_usd'):,.2f})\n"
        f"Цена: {alert.get('price'):.6g}\n"
        f"Агрегация: {alert.get('trades_aggregated', 1)} fill(s)"
    )


def format_cd_alert(alert: dict[str, Any]) -> str:
    direction = "покупки" if alert.get("direction") == "buy_pressure" else "продажи"
    return (
        f"⚡ Резкая дельта (перевес {direction})\n"
        f"Биржа: {alert.get('exchange')}\n"
        f"Пара: {alert.get('symbol')}\n"
        f"Δ за {int(alert.get('window_sec', 300))}с: {alert.get('delta_window'):+.6g}\n"
        f"Накопленная Δ: {alert.get('cum_delta'):+.6g}"
    )


def format_rsi_alert(alert: dict[str, Any]) -> str:
    kind = "бычья" if alert.get("divergence") == "bullish" else "медвежья"
    return (
        f"📉 RSI-дивергенция ({kind})\n"
        f"Биржа: {alert.get('exchange')}\n"
        f"Пара: {alert.get('symbol')}\n"
        f"ТФ: {alert.get('timeframe')}\n"
        f"Цена: {alert.get('price_a'):.6g} → {alert.get('price_b'):.6g}\n"
        f"RSI: {alert.get('rsi_a'):.2f} → {alert.get('rsi_b'):.2f}"
    )


def format_report(coin: str, sections: list[dict[str, Any]]) -> str:
    if not sections:
        return f"По {coin} нет отслеживаемых пар."
    lines = [f"Отчёт по {coin.upper()}"]
    for sec in sections:
        lines.append("")
        lines.append(f"=== {sec['exchange']} / {sec['symbol']} ===")
        lines.append(f"Накопленная Δ с добавления: {sec['cum_delta']:+.6g}")
        for tf_block in sec["timeframes"]:
            lines.append(f"\n[{tf_block['tf']}]")
            lines.append(
                f"Цена: {tf_block['price_abs']:+.6g} ({tf_block['price_pct']:+.2f}%)"
            )
            lines.append(
                f"Δ: {tf_block['delta_abs']:+.6g} ({tf_block['delta_pct']:+.2f}% от объёма)"
            )
    return "\n".join(lines)


def parse_add_args(text: str) -> tuple[str, str, bool, bool, float | None]:
    """Parse '/add BTC binance big cd big:50000'."""
    parts = text.split()
    if len(parts) < 3:
        raise ValueError("Использование: /add <Coin> <Exchange> [big] [cd] [big:USD]")
    coin = parts[1]
    exchange = parts[2]
    flag_big = False
    flag_cd = False
    threshold: float | None = None
    for token in parts[3:]:
        lower = token.lower()
        if lower == "big":
            flag_big = True
        elif lower == "cd":
            flag_cd = True
        elif lower.startswith("big:"):
            flag_big = True
            threshold = float(lower.split(":", 1)[1])
        else:
            raise ValueError(f"Неизвестный флаг: {token}")
    if not flag_big and not flag_cd:
        # allow add without flags — just track for RSI/report via klines; enable nothing special
        pass
    return coin, exchange, flag_big, flag_cd, threshold
