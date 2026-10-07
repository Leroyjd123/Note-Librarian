"""Google Gemini provider (google-genai SDK)."""
from __future__ import annotations

from .base import LLMProvider


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, max_tokens: int):
        super().__init__(api_key, model, max_tokens)
        from google import genai
        from google.genai import types

        self.client = genai.Client(api_key=api_key)
        self._types = types

    def complete(self, system: str, user: str) -> str:
        resp = self.client.models.generate_content(
            model=self.model,
            contents=user,
            config=self._types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                max_output_tokens=self.max_tokens,
            ),
        )
        return resp.text or ""
