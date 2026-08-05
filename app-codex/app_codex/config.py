"""Environment-backed configuration for the Codex microservice."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CODEX_",
        env_file=".env",
        extra="ignore",
    )

    project_root: str = "."
    api_key: str = ""
    timeout_s: int = 600
    redis_url: str = "redis://localhost:6379/0"
    max_concurrency: int = 2
    token_secret: str = "dev-insecure-token-secret-change-me"


settings = Settings()

