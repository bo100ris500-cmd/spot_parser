from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Lower number = higher priority
PRIO_COMBO = 0
PRIO_BIG = 1
PRIO_RSI = 2
PRIO_CD = 3
PRIO_OTHER = 5


@dataclass(order=True)
class _QueuedMessage:
    priority: int
    seq: int
    bot: Any = field(compare=False)
    chat_id: int = field(compare=False)
    text: str = field(compare=False)


class TelegramOutbox:
    """
    Serializes Telegram sends with rate-limit and flood-control pause.
    Prevents RetryAfter log storms and further bans.
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
        self._dropped = 0

    @property
    def blocked_until(self) -> float:
        return self._blocked_until

    def is_blocked(self) -> bool:
        return time.time() < self._blocked_until

    def seconds_remaining(self) -> int:
        return max(0, int(self._blocked_until - time.time()))

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
    ) -> bool:
        """Enqueue a message. Returns False if dropped."""
        if drop_if_blocked and self.is_blocked():
            # Long flood bans: don't pile up thousands of messages
            if self.seconds_remaining() > 120:
                self._dropped += 1
                if self._dropped % 50 == 1:
                    logger.warning(
                        "Telegram muted %ss left — dropping alerts (dropped=%s)",
                        self.seconds_remaining(),
                        self._dropped,
                    )
                return False

        # Bound queue: drop lowest-priority (highest number) by not accepting CD spam
        if self._queue.qsize() >= self.max_queue:
            if priority >= PRIO_CD:
                self._dropped += 1
                return False
            # Still enqueue high-priority; worker will drain

        self._seq += 1
        await self._queue.put(
            _QueuedMessage(priority=priority, seq=self._seq, bot=bot, chat_id=chat_id, text=text)
        )
        return True

    async def send_now_safe(self, bot: Bot, chat_id: int, text: str) -> bool:
        """
        Immediate send for command replies.
        Honours flood pause; does not enqueue.
        """
        if self.is_blocked():
            logger.warning(
                "Cannot reply: Telegram flood pause %ss remaining",
                self.seconds_remaining(),
            )
            return False
        try:
            await bot.send_message(chat_id, text)
            return True
        except TelegramRetryAfter as exc:
            self._blocked_until = time.time() + float(exc.retry_after)
            logger.error(
                "Telegram flood control: pause sending for %ss (~%.1fh)",
                int(exc.retry_after),
                float(exc.retry_after) / 3600.0,
            )
            return False
        except Exception as exc:
            logger.exception("Telegram send failed: %s", exc)
            return False

    async def _worker(self) -> None:
        logger.info(
            "Telegram outbox started (min_interval=%.1fs, max_queue=%s)",
            self.min_interval_sec,
            self.max_queue,
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
                # Drop low-priority while banned for a long time
                if remaining > 120 and item.priority >= PRIO_CD:
                    self._dropped += 1
                    continue
                # Re-queue high priority after short wait chunks
                await asyncio.sleep(min(remaining, 30.0))
                if time.time() < self._blocked_until and item.priority >= PRIO_CD:
                    self._dropped += 1
                    continue
                if time.time() < self._blocked_until:
                    await self._queue.put(item)
                    continue

            try:
                await item.bot.send_message(item.chat_id, item.text)
            except TelegramRetryAfter as exc:
                self._blocked_until = time.time() + float(exc.retry_after)
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
                # Do not re-queue during multi-hour bans
                if float(exc.retry_after) <= 120 and item.priority <= PRIO_BIG:
                    await self._queue.put(item)
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
