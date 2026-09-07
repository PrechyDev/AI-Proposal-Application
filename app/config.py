from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(validation_alias="SUPABASE_URL")
    anthropic_api_key: str = Field(validation_alias="CLAUDE_API_KEY")
    claude_model: str = "claude-sonnet-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
