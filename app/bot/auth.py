from __future__ import annotations

from collections.abc import Callable, Awaitable
from typing import Any

from aiogram.types import Message

from app.config import get_settings


def is_allowed(message: Message) -> bool:
    settings = get_settings()
    return bool(message.chat and message.chat.id == settings.allowed_chat_id)


def require_auth(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    async def wrapper(message: Message, *args: Any, **kwargs: Any) -> Any:
        if not is_allowed(message):
            return None
        return await handler(message, *args, **kwargs)

    return wrapper
