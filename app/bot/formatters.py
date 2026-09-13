from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.exchanges.names import display_name
from app.utils.timefmt import format_dt


def format_volume(vol: float) -> str:
    abs_v = abs(vol)
    if abs_v >= 1_000_000_000:
        return f"{vol / 1_000_000_000:.2f}B"
    if abs_v >= 1_000_000:
        return f"{vol / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"{vol / 1_000:.2f}K"
    return f"{vol:.2f}"


def format_pair_list(pairs: list[Any]) -> str:
    if not pairs:
        return "Список пуст. Добавьте пару через /add или /check"

    big_map: dict[str, list[Any]] = defaultdict(list)
    cd_map: dict[str, list[Any]] = defaultdict(list)
    for p in pairs:
        if p.flag_big:
            big_map[p.coin].append(p)
        if p.flag_cd:
            cd_map[p.coin].append(p)

    lines: list[str] = []
    lines.append("Большие транзакции:")
    if not big_map:
        lines.append("—")
    else:
        for coin in sorted(big_map.keys()):
            items = sorted(big_map[coin], key=lambda x: x.exchange)
            exchanges = " ".join(display_name(p.exchange) for p in items)
            times = [p.big_started_at or p.timestamp_added for p in items]
            when = format_dt(min(t for t in times if t is not None))
            lines.append(f"{coin} {exchanges} {when}")

    lines.append("")
    lines.append("Кумулятивная дельта:")
    if not cd_map:
        lines.append("—")
    else:
        for coin in sorted(cd_map.keys()):
            items = sorted(cd_map[coin], key=lambda x: x.exchange)
            exchanges = " ".join(display_name(p.exchange) for p in items)
            times = [p.cd_started_at or p.timestamp_added for p in items]
            when = format_dt(min(t for t in times if t is not None))
            lines.append(f"{coin} {exchanges} {when}")

    return "\n".join(lines)


def format_big_alert(alert: dict[str, Any]) -> str:
    side = "BUY" if alert.get("side") == "buy" else "SELL"
    exchange = alert.get("exchange") or ""
    symbol = alert.get("symbol") or ""
    coin = (alert.get("coin") or symbol.split("/")[0]).upper()
    cost = float(alert.get("cost_usd") or 0)
    price = float(alert.get("price") or 0)
    fills = int(alert.get("trades_aggregated", 1) or 1)
    return (
        f"🐋 {side} {exchange} {coin} ~${cost:,.2f} {price:.6g}\n"
        f"{fills} fill(s)"
    )


def format_cd_alert(alert: dict[str, Any]) -> str:
    direction = "покупки" if alert.get("direction") == "buy_pressure" else "продажи"
    return (
        f"⚡ Резкая дельта (перевес {direction})\n"
        f"Биржа: {display_name(str(alert.get('exchange') or ''))}\n"
        f"Пара: {alert.get('symbol')}\n"
        f"Δ за {int(alert.get('window_sec', 300))}с: {alert.get('delta_window'):+.6g}\n"
        f"Накопленная Δ: {alert.get('cum_delta'):+.6g}"
    )


def format_rsi_alert(alert: dict[str, Any]) -> str:
    kind = "бычья" if alert.get("divergence") == "bullish" else "медвежья"
    return (
        f"📉 RSI-дивергенция ({kind})\n"
        f"Биржа: {display_name(str(alert.get('exchange') or ''))}\n"
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
        lines.append(f"=== {display_name(sec['exchange'])} / {sec['symbol']} ===")
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


def format_check_table(coin: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"Токен {coin.upper()} не найден на отслеживаемых биржах."
    # Column widths
    name_w = max(len(display_name(r["exchange"])) for r in rows)
    name_w = max(name_w, len("Exc."))
    lines = [f"Токен {coin.upper()}", f"{'Exc.'.ljust(name_w)} | Vol"]
    for r in rows:
        name = display_name(r["exchange"]).ljust(name_w)
        vol = format_volume(float(r["volume"]))
        # visual stars by rank buckets
        lines.append(f"{name} | {vol}")
    return "\n".join(lines)


def parse_ticker_exchanges(text: str) -> tuple[str, list[str]]:
    """Parse 'BTC Binance Bybit Bitget' → coin + exchange ids."""
    from app.exchanges.names import normalize_exchange

    parts = text.strip().split()
    if len(parts) < 2:
        raise ValueError("Укажите тикер и хотя бы одну биржу, например: BTC Binance Bybit")
    coin = parts[0].upper()
    exchanges: list[str] = []
    for token in parts[1:]:
        ex = normalize_exchange(token)
        if ex is None:
            raise ValueError(f"Неизвестная биржа: {token}")
        if ex not in exchanges:
            exchanges.append(ex)
    return coin, exchanges


def parse_add_args(text: str) -> tuple[str, str, bool, bool, float | None]:
    """Legacy parser kept for tests/compat: '/add BTC binance big cd big:50000'."""
    parts = text.split()
    # allow both with and without leading /add
    if parts and parts[0].startswith("/"):
        parts = parts[1:]
    if len(parts) < 2:
        raise ValueError("Использование: /add <Coin> <Exchange> [big] [cd] [big:USD]")
    coin = parts[0]
    exchange = parts[1]
    flag_big = False
    flag_cd = False
    threshold: float | None = None
    for token in parts[2:]:
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
    return coin, exchange, flag_big, flag_cd, threshold
