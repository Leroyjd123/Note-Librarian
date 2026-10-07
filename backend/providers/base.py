"""Abstract LLM provider interface (strategy pattern)."""
from __future__ import annotations

from abc import ABC, abstractmethod

# HTTP statuses worth retrying: timeouts, conflicts, rate limits, server errors/overload.
_TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
_TRANSIENT_NAMES = ("Timeout", "Connection", "RateLimit", "Overloaded", "ServiceUnavailable", "ServerError")


class ModelDeclined(Exception):
    """The model answered but declined the request (e.g. a safety refusal).

    Not an outage, so it is never retried as-is; the engine splits the batch instead.
    """


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


def is_transient(exc: BaseException) -> bool:
    """True if a failed call is worth retrying (rate limit, overload, network blip)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUS:
        return True
    return any(n in type(exc).__name__ for n in _TRANSIENT_NAMES)
