import logging
from functools import lru_cache

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

import app.logging_config  # noqa: F401 - ensures logging is configured before anything logs

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(validation_alias="SUPABASE_URL")
    anthropic_api_key: str = Field(validation_alias="CLAUDE_API_KEY")
    claude_model: str = "claude-sonnet-5"
    session_secret_key: str = Field(validation_alias="SESSION_SECRET_KEY")


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing = [str(err["loc"][0]) for err in exc.errors() if err["type"] == "missing"]
        if missing:
            logger.critical(
                "Missing required environment variable(s): %s. "
                "Check .env against .env.example.",
                ", ".join(missing),
            )
        else:
            logger.critical("Invalid environment configuration: %s", exc)
        raise
