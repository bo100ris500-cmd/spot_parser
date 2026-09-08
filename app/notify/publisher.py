from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

CHANNEL_ALERTS = "spot:alerts"


class AlertPublisher:
    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    async def publish(self, alert: dict[str, Any]) -> None:
        payload = json.dumps(alert, ensure_ascii=False, default=str)
        await self._redis.publish(CHANNEL_ALERTS, payload)
        logger.debug("Published alert type=%s", alert.get("type"))


async def create_redis() -> redis.Redis:
    settings = get_settings()
    return redis.from_url(settings.redis_url, decode_responses=True)
