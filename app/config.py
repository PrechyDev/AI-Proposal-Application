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
    supabase_project_url: str = Field(validation_alias="SUPABASE_PROJECT_URL")
    supabase_service_role_key: str = Field(validation_alias="SUPABASE_SERVICE_ROLE_KEY")

    # Optional, not required: unlike the vars above, the app has a real,
    # spec-sanctioned degraded mode when these are missing (email send
    # fails and is logged to delivery_logs - spec section 7's "Email API
    # is down" edge case) rather than nothing working at all. A fresh dev
    # environment shouldn't be unable to start just because no email
    # provider has been configured yet.
    brevo_api_key: str | None = Field(default=None, validation_alias="BREVO_API_KEY")
    email_from_address: str | None = Field(default=None, validation_alias="EMAIL_FROM_ADDRESS")
    email_from_name: str = Field(default="Koya Talent", validation_alias="EMAIL_FROM_NAME")
    app_base_url: str = Field(default="http://127.0.0.1:8000", validation_alias="APP_BASE_URL")


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
