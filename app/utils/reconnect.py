from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.exchanges.capabilities import is_invalid_symbol_error
from app.utils.logging import get_logger

logger = get_logger(__name__)
T = TypeVar("T")


class PermanentStreamError(RuntimeError):
    """Raised when reconnecting is pointless (invalid symbol, missing dependency, …)."""


async def with_exponential_backoff(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    name: str = "task",
    base_delay: float = 2.0,
    max_delay: float = 300.0,
    max_attempts: int | None = None,
    stop_on_invalid_symbol: bool = True,
) -> T:
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except asyncio.CancelledError:
            raise
        except PermanentStreamError:
            raise
        except Exception as exc:
            attempt += 1
            if stop_on_invalid_symbol and is_invalid_symbol_error(exc):
                logger.error("%s permanent error (invalid symbol): %s", name, exc)
                raise PermanentStreamError(str(exc)) from exc
            # Missing protobuf etc. — no point hammering every second
            msg = str(exc).lower()
            if "protobuf" in msg or "no module named 'google'" in msg:
                logger.error("%s dependency error: %s", name, exc)
                raise PermanentStreamError(str(exc)) from exc

            if max_attempts is not None and attempt >= max_attempts:
                logger.error("%s failed after %s attempts: %s", name, attempt, exc)
                raise PermanentStreamError(f"max attempts ({max_attempts}): {exc}") from exc

            delay = min(max_delay, base_delay * (2 ** min(attempt - 1, 8)))
            delay *= 0.5 + random.random()
            # Throttle log spam: every attempt until 5, then every 10th
            if attempt <= 5 or attempt % 10 == 0:
                logger.warning(
                    "%s error (attempt %s): %s; retry in %.1fs", name, attempt, exc, delay
                )
            await asyncio.sleep(delay)
