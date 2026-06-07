"""Google Gemini provider."""
from __future__ import annotations

from .base import LLMProvider


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, max_tokens: int):
        super().__init__(api_key, model, max_tokens)
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        self._genai = genai

    def complete(self, system: str, user: str) -> str:
        model = self._genai.GenerativeModel(self.model, system_instruction=system)
        resp = model.generate_content(
            user,
            generation_config={
                "response_mime_type": "application/json",
                "max_output_tokens": self.max_tokens,
            },
        )
        return resp.text or ""
