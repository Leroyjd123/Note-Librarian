"""Application settings, loaded from environment / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # API keys (keep these in .env, never commit them)
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None

    # Defaults surfaced to the UI
    default_provider: str = "anthropic"
    default_anthropic_model: str = "claude-sonnet-4-6"
    default_openai_model: str = "gpt-4o"
    default_gemini_model: str = "gemini-1.5-pro"

    default_batch_size: int = 40
    default_concurrency: int = 4
    default_max_tokens: int = 16000

    data_dir: str = "data"


settings = Settings()

# provider -> (env-key attribute, default-model attribute)
PROVIDERS = {
    "anthropic": ("anthropic_api_key", "default_anthropic_model"),
    "openai": ("openai_api_key", "default_openai_model"),
    "gemini": ("gemini_api_key", "default_gemini_model"),
}


def configured_providers() -> dict[str, dict]:
    """Report which providers have an API key set and their default model."""
    out = {}
    for name, (key_attr, model_attr) in PROVIDERS.items():
        out[name] = {
            "configured": bool(getattr(settings, key_attr)),
            "default_model": getattr(settings, model_attr),
        }
    return out


def api_key_for(provider: str) -> str:
    key_attr, _ = PROVIDERS[provider]
    key = getattr(settings, key_attr)
    if not key:
        raise ValueError(
            f"No API key configured for '{provider}'. Add {key_attr.upper()} to your .env file."
        )
    return key


def resolve_api_key(provider: str, override: str | None = None) -> str:
    """Use a per-request key if supplied (bring-your-own-key), else fall back to .env."""
    if override and override.strip():
        return override.strip()
    return api_key_for(provider)
