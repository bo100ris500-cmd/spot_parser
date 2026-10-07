from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_app_config, get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        try:
            cfg = get_app_config()
            pool_size = int(cfg.get("database", "pool_size", default=20))
            max_overflow = int(cfg.get("database", "max_overflow", default=30))
            pool_timeout = float(cfg.get("database", "pool_timeout", default=30))
            pool_recycle = int(cfg.get("database", "pool_recycle", default=1800))
        except Exception:
            pool_size, max_overflow, pool_timeout, pool_recycle = 20, 30, 30.0, 1800

        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            echo=False,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        logger.info(
            "DB pool ready pool_size=%s max_overflow=%s timeout=%s recycle=%s",
            pool_size,
            max_overflow,
            pool_timeout,
            pool_recycle,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
