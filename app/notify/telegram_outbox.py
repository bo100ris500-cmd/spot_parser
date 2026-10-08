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

_SEND_TIMEOUT_SEC = 35.0
_NETWORK_BACKOFF_BASE = 8.0
_NETWORK_BACKOFF_MAX = 45.0  # keep short — long mutes silently kill all alerts


@dataclass(order=True)
class _QueuedMessage:
    priority: int
    seq: int
    bot: Any = field(compare=False)
    chat_id: int = field(compare=False)
    text: str = field(compare=False)
    kwargs: dict[str, Any] = field(default_factory=dict, compare=False)
    attempts: int = field(default=0, compare=False)


class TelegramOutbox:
    """
    Serializes Telegram sends.

    Flood (RetryAfter) and network timeouts are tracked separately:
    - flood: may drop CD spam for long bans
    - network: short backoff only; never drop BIG/COMBO at the door
    """

    def __init__(
        self,
        *,
        min_interval_sec: float = 1.5,
        max_queue: int = 200,
        send_timeout_sec: float = _SEND_TIMEOUT_SEC,
    ) -> None:
        self.min_interval_sec = min_interval_sec
        self.max_queue = max_queue
        self.send_timeout_sec = float(send_timeout_sec)
        self._queue: asyncio.PriorityQueue[_QueuedMessage] = asyncio.PriorityQueue()
        self._seq = 0
        self._flood_until = 0.0
        self._network_until = 0.0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_flood_log = 0.0
        self._last_net_log = 0.0
        self._dropped = 0
        self._sent = 0
        self._network_backoff = _NETWORK_BACKOFF_BASE

    @property
    def blocked_until(self) -> float:
        return max(self._flood_until, self._network_until)

    def is_flood_blocked(self) -> bool:
        return time.time() < self._flood_until

    def is_network_paused(self) -> bool:
        return time.time() < self._network_until

    def is_blocked(self) -> bool:
        """True if any outbound pause is active (compat)."""
        return self.is_flood_blocked() or self.is_network_paused()

    def seconds_remaining(self) -> int:
        return max(0, int(self.blocked_until - time.time()))

    def flood_seconds_remaining(self) -> int:
        return max(0, int(self._flood_until - time.time()))

    def block_for(self, seconds: float, *, reason: str = "flood") -> None:
        until = time.time() + max(0.0, float(seconds))
        if reason == "flood":
            if until > self._flood_until:
                self._flood_until = until
                logger.error(
                    "Telegram pause (flood): %ss (~%.1fh)",
                    int(seconds),
                    float(seconds) / 3600.0,
                )
        else:
            if until > self._network_until:
                self._network_until = until
                logger.warning(
                    "Telegram pause (network): %ss",
                    int(seconds),
                )

    def note_network_error(self) -> None:
        """Short backoff after timeouts — do not silence the bot for minutes."""
        self.block_for(self._network_backoff, reason="network")
        self._network_backoff = min(self._network_backoff * 1.5, _NETWORK_BACKOFF_MAX)
        now = time.time()
        if now - self._last_net_log > 60:
            self._last_net_log = now
            logger.warning(
                "Telegram network issues — send backoff %ss (next up to %.0fs); "
                "queue=%s dropped=%s sent=%s",
                self.seconds_remaining(),
                self._network_backoff,
                self._queue.qsize(),
                self._dropped,
                self._sent,
            )

    def note_success(self) -> None:
        self._network_backoff = _NETWORK_BACKOFF_BASE
        self._network_until = 0.0
        self._sent += 1

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

    def _drop(self, reason: str, *, priority: int) -> None:
        self._dropped += 1
        if self._dropped % 25 == 1:
            logger.warning(
                "Telegram drop (%s) prio=%s queue=%s dropped=%s flood=%ss net=%ss",
                reason,
                priority,
                self._queue.qsize(),
                self._dropped,
                self.flood_seconds_remaining(),
                max(0, int(self._network_until - time.time())),
            )

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
        # Only hard-drop on long FLOOD bans (not network blips)
        if drop_if_blocked and self.is_flood_blocked() and self.flood_seconds_remaining() > 120:
            if priority >= PRIO_CD:
                self._drop("flood", priority=priority)
                return False

        if self._queue.qsize() >= self.max_queue:
            if priority >= PRIO_CD:
                self._drop("queue_full", priority=priority)
                return False
            # High-priority: still enqueue (queue may grow a bit)

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
        """Immediate send for command replies. Honours flood pause only."""
        if self.is_flood_blocked():
            logger.warning(
                "Cannot reply: Telegram flood pause %ss remaining",
                self.flood_seconds_remaining(),
            )
            return False
        try:
            await asyncio.wait_for(
                bot.send_message(chat_id, text, **kwargs),
                timeout=self.send_timeout_sec,
            )
            self.note_success()
            return True
        except TelegramRetryAfter as exc:
            self.block_for(float(exc.retry_after), reason="flood")
            return False
        except (TelegramNetworkError, asyncio.TimeoutError) as exc:
            self.note_network_error()
            logger.warning("Telegram send_now failed: %s", exc or type(exc).__name__)
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
            self.send_timeout_sec,
        )
        while not self._stop.is_set():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            now = time.time()
            wait_until = max(self._flood_until, self._network_until)
            if now < wait_until:
                remaining = wait_until - now
                # During long flood only: discard CD spam
                if self.is_flood_blocked() and self.flood_seconds_remaining() > 120:
                    if item.priority >= PRIO_CD:
                        self._drop("flood_worker", priority=item.priority)
                        continue
                await asyncio.sleep(min(remaining, 15.0))
                if time.time() < max(self._flood_until, self._network_until):
                    await self._queue.put(item)
                    continue

            try:
                await asyncio.wait_for(self._emit(item), timeout=self.send_timeout_sec)
                self.note_success()
            except TelegramRetryAfter as exc:
                self.block_for(float(exc.retry_after), reason="flood")
                now_log = time.time()
                if now_log - self._last_flood_log > 60:
                    self._last_flood_log = now_log
                    logger.error(
                        "Telegram flood control: muted for %ss (~%.1fh). "
                        "Or switch BOT_TOKEN to a new bot to unlock sooner.",
                        int(exc.retry_after),
                        float(exc.retry_after) / 3600.0,
                    )
                # Re-queue important alerts for short floods
                if float(exc.retry_after) <= 180 and item.priority <= PRIO_BIG:
                    item.attempts += 1
                    if item.attempts <= 3:
                        await self._queue.put(item)
            except (TelegramNetworkError, asyncio.TimeoutError) as exc:
                self.note_network_error()
                logger.warning(
                    "Telegram outbox send failed: %s",
                    exc or type(exc).__name__,
                )
                # Retry high-priority a few times after short network backoff
                if item.priority <= PRIO_RSI:
                    item.attempts += 1
                    if item.attempts <= 4:
                        await self._queue.put(item)
                    else:
                        self._drop("net_retries_exhausted", priority=item.priority)
                else:
                    self._drop("net_cd", priority=item.priority)
            except Exception as exc:
                logger.warning("Telegram outbox send failed: %s", exc)

            await asyncio.sleep(self.min_interval_sec)


_outbox: TelegramOutbox | None = None


def get_outbox() -> TelegramOutbox:
    global _outbox
    if _outbox is None:
        _outbox = TelegramOutbox()
    return _outbox


def init_outbox(
    *,
    min_interval_sec: float = 1.5,
    max_queue: int = 200,
    send_timeout_sec: float = _SEND_TIMEOUT_SEC,
) -> TelegramOutbox:
    global _outbox
    _outbox = TelegramOutbox(
        min_interval_sec=min_interval_sec,
        max_queue=max_queue,
        send_timeout_sec=send_timeout_sec,
    )
    return _outbox
