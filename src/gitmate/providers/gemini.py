"""Gemini provider over the google-genai SDK, with retry on transient errors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_fixed,
)

from gitmate.providers.base import LLMResponse, ProviderUnavailable, RetryableProviderError

if TYPE_CHECKING:
    from google import genai
    from google.genai import types

#: Hook-mode worst case must stay inside HOOK_TIMEOUT_SECONDS (hooks.py, 4s).
#: The worker makes one count_tokens request plus HOOK_GENERATION_ATTEMPTS
#: generation attempts, each capped at HOOK_REQUEST_TIMEOUT_MS with zero
#: backoff: 3 x 900ms = 2.7s of network time, leaving ~1.3s for interpreter
#: startup, diff extraction, and the fallback template.
HOOK_REQUEST_TIMEOUT_MS = 900
HOOK_GENERATION_ATTEMPTS = 2


def hook_http_options() -> types.HttpOptions:
    """Bound each Gemini HTTP request and disable SDK-level retries for hooks."""
    from google.genai import types

    return types.HttpOptions(
        timeout=HOOK_REQUEST_TIMEOUT_MS,
        retry_options=types.HttpRetryOptions(attempts=1),
    )


def _is_transient(exc: BaseException) -> bool:
    """True for failures worth retrying: 5xx, 429 rate limits, transport."""
    from google.genai.errors import ClientError, ServerError

    if isinstance(exc, ServerError):
        return True
    if isinstance(exc, ClientError):
        return getattr(exc, "code", None) == 429
    import httpx

    return isinstance(exc, httpx.TransportError)


class GeminiProvider:
    """LLMProvider backed by Google's Gemini models via google-genai."""

    def __init__(
        self,
        api_key: str | None = None,
        client: genai.Client | None = None,
        hook_mode: bool = False,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._hook_mode = hook_mode

    def _get_client(self) -> genai.Client:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ProviderUnavailable(
                "Gemini API key is required. Run 'gitmate config set-key' first."
            )
        from google import genai

        if self._hook_mode:
            self._client = genai.Client(api_key=self._api_key, http_options=hook_http_options())
        else:
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate(self, prompt: str, model: str) -> LLMResponse:
        """Generate text for prompt, retrying transient failures then raising."""
        generate = self._generate_with_hook_retry if self._hook_mode else self._generate_with_retry
        try:
            return generate(self._get_client(), prompt, model)
        except RetryableProviderError as exc:
            raise ProviderUnavailable(f"Gemini unavailable after retries: {exc}") from exc

    def _generate_once(self, client: genai.Client, prompt: str, model: str) -> LLMResponse:
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
        except Exception as exc:
            if _is_transient(exc):
                raise RetryableProviderError(str(exc)) from exc
            raise ProviderUnavailable(f"Gemini request failed: {exc}") from exc
        text = (resp.text or "").strip()
        if not text:
            finish_reason = None
            candidates = getattr(resp, "candidates", None)
            if candidates and len(candidates) > 0:
                finish_reason = getattr(candidates[0], "finish_reason", None)
            if finish_reason is not None:
                reason_name = getattr(finish_reason, "name", str(finish_reason))
                raise ProviderUnavailable(
                    f"Gemini declined to respond (finish_reason: {reason_name})."
                )
            raise ProviderUnavailable("Gemini returned an empty response.")
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=getattr(resp.usage_metadata, "prompt_token_count", None),
            output_tokens=getattr(resp.usage_metadata, "candidates_token_count", None),
        )

    @retry(
        retry=retry_if_exception_type(RetryableProviderError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _generate_with_retry(self, client: genai.Client, prompt: str, model: str) -> LLMResponse:
        return self._generate_once(client, prompt, model)

    @retry(
        retry=retry_if_exception_type(RetryableProviderError),
        stop=stop_after_attempt(HOOK_GENERATION_ATTEMPTS),
        wait=wait_fixed(0),
        reraise=True,
    )
    def _generate_with_hook_retry(
        self, client: genai.Client, prompt: str, model: str
    ) -> LLMResponse:
        return self._generate_once(client, prompt, model)
