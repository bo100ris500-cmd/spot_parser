from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Lower number = higher priority
PRIO_CMD = -1
PRIO_COMBO = 0
PRIO_BIG = 1
PRIO_RSI = 2
PRIO_CD = 3
PRIO_OTHER = 5

# Cap how long a single Telegram HTTP call may block the outbox worker
_SEND_TIMEOUT_SEC = 20.0
_NETWORK_BACKOFF_BASE = 15.0
_NETWORK_BACKOFF_MAX = 300.0


@dataclass(order=True)
class _QueuedMessage:
    priority: int
    seq: int
    bot: Any = field(compare=False)
    chat_id: int = field(compare=False)
    text: str = field(compare=False)
    kwargs: dict[str, Any] = field(default_factory=dict, compare=False)


class TelegramOutbox:
    """
    Serializes Telegram sends with rate-limit, flood-control and network backoff.
    Prevents RetryAfter storms and long hangs when api.telegram.org is unreachable.
    """

    def __init__(
        self,
        *,
        min_interval_sec: float = 1.5,
        max_queue: int = 80,
    ) -> None:
        self.min_interval_sec = min_interval_sec
        self.max_queue = max_queue
        self._queue: asyncio.PriorityQueue[_QueuedMessage] = asyncio.PriorityQueue()
        self._seq = 0
        self._blocked_until = 0.0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_flood_log = 0.0
        self._last_net_log = 0.0
        self._dropped = 0
        self._network_backoff = _NETWORK_BACKOFF_BASE

    @property
    def blocked_until(self) -> float:
        return self._blocked_until

    def is_blocked(self) -> bool:
        return time.time() < self._blocked_until

    def seconds_remaining(self) -> int:
        return max(0, int(self._blocked_until - time.time()))

    def block_for(self, seconds: float, *, reason: str = "flood") -> None:
        until = time.time() + max(0.0, float(seconds))
        if until > self._blocked_until:
            self._blocked_until = until
            logger.error(
                "Telegram pause (%s): %ss (~%.1fh)",
                reason,
                int(seconds),
                float(seconds) / 3600.0,
            )

    def note_network_error(self) -> None:
        """Back off after timeouts so we do not pile 20s hangs forever."""
        self.block_for(self._network_backoff, reason="network")
        self._network_backoff = min(self._network_backoff * 2.0, _NETWORK_BACKOFF_MAX)
        now = time.time()
        if now - self._last_net_log > 60:
            self._last_net_log = now
            logger.warning(
                "Telegram network issues — outbound muted %ss (next backoff up to %.0fs)",
                self.seconds_remaining(),
                self._network_backoff,
            )

    def note_success(self) -> None:
        self._network_backoff = _NETWORK_BACKOFF_BASE

    def start(self) -> None:
        self._stop.clear()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="telegram-outbox")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def send(
        self,
        bot: Bot,
        chat_id: int,
        text: str,
        *,
        priority: int = PRIO_OTHER,
        drop_if_blocked: bool = True,
        **kwargs: Any,
    ) -> bool:
        """Enqueue a message. Returns False if dropped."""
        if drop_if_blocked and self.is_blocked():
            if self.seconds_remaining() > 120:
                self._dropped += 1
                if self._dropped % 50 == 1:
                    logger.warning(
                        "Telegram muted %ss left — dropping alerts (dropped=%s)",
                        self.seconds_remaining(),
                        self._dropped,
                    )
                return False

        if self._queue.qsize() >= self.max_queue:
            if priority >= PRIO_CD:
                self._dropped += 1
                return False

        self._seq += 1
        await self._queue.put(
            _QueuedMessage(
                priority=priority,
                seq=self._seq,
                bot=bot,
                chat_id=chat_id,
                text=text,
                kwargs=dict(kwargs),
            )
        )
        return True

    async def send_now_safe(self, bot: Bot, chat_id: int, text: str, **kwargs: Any) -> bool:
        """Immediate send for command replies. Honours pause; does not enqueue."""
        if self.is_blocked():
            logger.warning(
                "Cannot reply: Telegram pause %ss remaining",
                self.seconds_remaining(),
            )
            return False
        try:
            await asyncio.wait_for(
                bot.send_message(chat_id, text, **kwargs),
                timeout=_SEND_TIMEOUT_SEC,
            )
            self.note_success()
            return True
        except TelegramRetryAfter as exc:
            self.block_for(float(exc.retry_after), reason="flood")
            return False
        except (TelegramNetworkError, asyncio.TimeoutError) as exc:
            self.note_network_error()
            logger.warning("Telegram send_now failed: %s", exc)
            return False
        except Exception as exc:
            logger.exception("Telegram send failed: %s", exc)
            return False

    async def _emit(self, item: _QueuedMessage) -> None:
        await item.bot.send_message(item.chat_id, item.text, **item.kwargs)

    async def _worker(self) -> None:
        logger.info(
            "Telegram outbox started (min_interval=%.1fs, max_queue=%s, send_timeout=%.0fs)",
            self.min_interval_sec,
            self.max_queue,
            _SEND_TIMEOUT_SEC,
        )
        while not self._stop.is_set():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            now = time.time()
            if now < self._blocked_until:
                remaining = self._blocked_until - now
                if remaining > 120 and item.priority >= PRIO_CD:
                    self._dropped += 1
                    continue
                await asyncio.sleep(min(remaining, 30.0))
                if time.time() < self._blocked_until and item.priority >= PRIO_CD:
                    self._dropped += 1
                    continue
                if time.time() < self._blocked_until:
                    await self._queue.put(item)
                    continue

            try:
                await asyncio.wait_for(self._emit(item), timeout=_SEND_TIMEOUT_SEC)
                self.note_success()
            except TelegramRetryAfter as exc:
                self.block_for(float(exc.retry_after), reason="flood")
                now_log = time.time()
                if now_log - self._last_flood_log > 60:
                    self._last_flood_log = now_log
                    logger.error(
                        "Telegram flood control: muted for %ss (~%.1fh). "
                        "Alerts will be dropped until then. "
                        "Or switch BOT_TOKEN to a new bot to unlock sooner.",
                        int(exc.retry_after),
                        float(exc.retry_after) / 3600.0,
                    )
                if float(exc.retry_after) <= 120 and item.priority <= PRIO_BIG:
                    await self._queue.put(item)
            except (TelegramNetworkError, asyncio.TimeoutError) as exc:
                self.note_network_error()
                logger.warning("Telegram outbox send failed: %s", exc)
                # Do not re-queue: backoff pause covers the outage; avoids queue blow-up
            except Exception as exc:
                logger.warning("Telegram outbox send failed: %s", exc)

            await asyncio.sleep(self.min_interval_sec)


_outbox: TelegramOutbox | None = None


def get_outbox() -> TelegramOutbox:
    global _outbox
    if _outbox is None:
        _outbox = TelegramOutbox()
    return _outbox


def init_outbox(*, min_interval_sec: float = 1.5, max_queue: int = 80) -> TelegramOutbox:
    global _outbox
    _outbox = TelegramOutbox(min_interval_sec=min_interval_sec, max_queue=max_queue)
    return _outbox
