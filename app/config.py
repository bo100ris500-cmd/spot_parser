from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="BOT_TOKEN")
    allowed_chat_id: int = Field(alias="ALLOWED_CHAT_ID")
    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    config_path: str = Field(default="config.yaml", alias="CONFIG_PATH")


def load_yaml_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


class AppConfig:
    """Runtime YAML config that can be reloaded without process rebuild."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data = load_yaml_config(self.path)

    def reload(self) -> None:
        self._data = load_yaml_config(self.path)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self._data
        for key in keys:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur


_app_config: AppConfig | None = None


def get_app_config() -> AppConfig:
    global _app_config
    if _app_config is None:
        settings = get_settings()
        _app_config = AppConfig(settings.config_path)
    return _app_config
