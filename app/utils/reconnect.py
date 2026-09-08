from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.utils.logging import get_logger

logger = get_logger(__name__)
T = TypeVar("T")


async def with_exponential_backoff(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    name: str = "task",
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    max_attempts: int | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            if max_attempts is not None and attempt >= max_attempts:
                logger.error("%s failed after %s attempts: %s", name, attempt, exc)
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= 0.5 + random.random()
            logger.warning("%s error (attempt %s): %s; retry in %.1fs", name, attempt, exc, delay)
            await asyncio.sleep(delay)
