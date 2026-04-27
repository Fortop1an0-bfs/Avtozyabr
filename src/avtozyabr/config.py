"""Central configuration via pydantic-settings (reads .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "avtozyabr"
    postgres_user: str = "avtozyabr"
    postgres_password: str = "changeme"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str
    telegram_allowed_users: Annotated[list[int], Field(default_factory=list)]

    @field_validator("telegram_allowed_users", mode="before")
    @classmethod
    def parse_users(cls, v: str | list) -> list[int]:
        if isinstance(v, list):
            return [int(x) for x in v]
        return [int(x.strip()) for x in str(v).split(",") if x.strip()]

    # ── Zolotoe Yabloko ───────────────────────────────────────────────────────
    zy_base_url: str = "https://zolotoyabloko.ru"
    zy_cookies_file: Path | None = None

    # ── Polling ───────────────────────────────────────────────────────────────
    poll_interval_normal: int = 300   # seconds
    poll_interval_hot: int = 30       # seconds for high-priority items

    # ── Safety ────────────────────────────────────────────────────────────────
    daily_spend_limit: float = 0.0    # 0 = unlimited

    # ── Observability ─────────────────────────────────────────────────────────
    sentry_dsn: str = ""
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
