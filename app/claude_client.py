from functools import lru_cache

from anthropic import Anthropic

from app.config import get_settings


@lru_cache
def get_client() -> Anthropic:
    settings = get_settings()
    return Anthropic(api_key=settings.anthropic_api_key)
