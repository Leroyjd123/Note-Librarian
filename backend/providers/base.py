"""Abstract LLM provider interface (strategy pattern)."""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """A minimal text-completion interface every provider implements.

    ``complete`` is synchronous; the engine runs it in a worker thread so many
    batches can be in flight concurrently.
    """

    name: str = "base"

    def __init__(self, api_key: str, model: str, max_tokens: int):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return the model's raw text response (expected to be JSON)."""
        raise NotImplementedError
