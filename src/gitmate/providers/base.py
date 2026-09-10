"""Provider abstraction: LLMProvider protocol, response types, errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ProviderUnavailable(Exception):
    """Raised when the LLM backend cannot produce a result after retries."""


class RetryableProviderError(Exception):
    """Internal marker for transient failures worth retrying before giving up."""


@dataclass
class LLMResponse:
    """Text plus optional token usage from one provider call."""

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Generates text; every backend can be swapped behind this boundary."""

    def generate(self, prompt: str, model: str) -> LLMResponse:
        """Produce a response for the prompt using the given model."""
        ...
