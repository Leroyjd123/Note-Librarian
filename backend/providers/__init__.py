"""Provider factory."""
from __future__ import annotations

from .base import LLMProvider


def get_provider(provider: str, model: str, max_tokens: int, api_key: str) -> LLMProvider:
    if provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key, model, max_tokens)
    if provider == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(api_key, model, max_tokens)
    if provider == "gemini":
        from .gemini_provider import GeminiProvider

        return GeminiProvider(api_key, model, max_tokens)
    raise ValueError(f"Unknown provider: {provider}")
