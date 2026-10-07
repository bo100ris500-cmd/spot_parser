from __future__ import annotations

import logging
import sys
import time
from typing import Any


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level.upper())

    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def format_exc_detail(exc: BaseException) -> str:
    """Richer exception text — ccxt often puts only the request URL in str(exc)."""
    parts: list[str] = [type(exc).__name__]
    msg = str(exc).strip()
    if msg:
        parts.append(msg)
    for attr in ("status", "http_status", "code"):
        val = getattr(exc, attr, None)
        if val is not None and str(val) not in msg:
            parts.append(f"{attr}={val}")
    nested = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if nested is not None and nested is not exc:
        nested_msg = str(nested).strip()
        if nested_msg and nested_msg not in msg:
            parts.append(f"cause={type(nested).__name__}:{nested_msg[:200]}")
    return " | ".join(parts)


_throttle_until: dict[str, float] = {}


def warn_throttled(
    logger: logging.Logger,
    key: str,
    msg: str,
    *args: Any,
    interval_sec: float = 60.0,
) -> None:
    """Log WARNING at most once per key within interval_sec."""
    now = time.monotonic()
    until = _throttle_until.get(key, 0.0)
    if now < until:
        return
    _throttle_until[key] = now + interval_sec
    logger.warning(msg, *args)
