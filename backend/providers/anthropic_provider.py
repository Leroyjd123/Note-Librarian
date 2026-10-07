"""Anthropic Claude provider."""
from __future__ import annotations

from .base import LLMProvider, ModelDeclined

# Models that accept server-side refusal fallbacks: if a safety classifier declines
# a batch, the API re-runs it on a fallback model instead of stopping.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int):
        super().__init__(api_key, model, max_tokens)
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key)

    def complete(self, system: str, user: str) -> str:
        # Streaming avoids HTTP timeouts (and the SDK's refusal of long
        # non-streaming requests) when max_tokens is large.
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if self.model in _FALLBACK_MODELS:
            stream = self.client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            stream = self.client.messages.stream(**kwargs)
        with stream as s:
            resp = s.get_final_message()
        if resp.stop_reason == "refusal":
            raise ModelDeclined("the model declined this batch")
        return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
