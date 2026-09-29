from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


async def send_pushover(
    title: str,
    message: str,
    *,
    priority: int = 0,
) -> bool:
    """
    Send Pushover notification.
    priority=2 is emergency (requires retry/expire).
    """
    settings = get_settings()
    token = settings.pushover_api_token
    user = settings.pushover_user_key
    if not token or not user:
        logger.warning("Pushover skipped: PUSHOVER_API_TOKEN / PUSHOVER_USER_KEY not set")
        return False

    data: dict[str, Any] = {
        "token": token,
        "user": user,
        "title": title[:250],
        "message": message[:1024],
        "priority": int(priority),
    }
    if int(priority) == 2:
        data["retry"] = 60
        data["expire"] = 3600
        data["sound"] = "siren"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(PUSHOVER_URL, data=data)
            if resp.status_code >= 400:
                logger.error("Pushover error %s: %s", resp.status_code, resp.text[:300])
                return False
            return True
    except Exception as exc:
        logger.exception("Pushover send failed: %s", exc)
        return False
