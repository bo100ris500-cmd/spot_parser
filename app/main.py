from __future__ import annotations

import asyncio

from aiogram import Bot

from sqlalchemy import text

from app.bot.handlers import create_dispatcher, send_alert, set_hub, setup_bot_commands
from app.config import get_app_config, get_settings
from app.db.models import Base
from app.db.session import get_engine, get_session_factory
from app.notify.consumer import AlertConsumer
from app.notify.publisher import AlertPublisher, create_redis
from app.services.runtime import RuntimeHub
from app.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Safety for existing DBs if alembic was skipped
        await conn.execute(
            text("ALTER TABLE watched_pairs ADD COLUMN IF NOT EXISTS big_started_at TIMESTAMPTZ")
        )
        await conn.execute(
            text("ALTER TABLE watched_pairs ADD COLUMN IF NOT EXISTS cd_started_at TIMESTAMPTZ")
        )
    get_session_factory()
    logger.info("Database schema ensured")


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    config = get_app_config()

    await init_db()

    redis = await create_redis()
    publisher = AlertPublisher(redis)

    hub = RuntimeHub(config, publisher)
    set_hub(hub)
    await hub.start()

    bot = Bot(token=settings.bot_token)
    await setup_bot_commands(bot)
    dp = create_dispatcher()

    async def on_alert(alert: dict) -> None:
        await send_alert(bot, settings.allowed_chat_id, alert)

    consumer = AlertConsumer(redis, on_alert)
    await consumer.start()

    logger.info("Starting Telegram polling; allowed_chat_id=%s", settings.allowed_chat_id)

    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down…")
        await consumer.stop()
        await hub.stop()
        await bot.session.close()
        await redis.aclose()
        await get_engine().dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
