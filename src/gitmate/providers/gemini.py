"""Gemini provider over the google-genai SDK, with retry on transient errors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gitmate.providers.base import LLMResponse, ProviderUnavailable, RetryableProviderError

if TYPE_CHECKING:
    from google import genai


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

    def __init__(self, api_key: str | None = None, client: genai.Client | None = None) -> None:
        self._client = client
        self._api_key = api_key

    def _get_client(self) -> genai.Client:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ProviderUnavailable(
                "Gemini API key is required. Run 'gitmate config set-key' first."
            )
        from google import genai

        self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate(self, prompt: str, model: str) -> LLMResponse:
        """Generate text for prompt, retrying transient failures then raising."""
        try:
            return self._generate_with_retry(self._get_client(), prompt, model)
        except RetryableProviderError as exc:
            raise ProviderUnavailable(f"Gemini unavailable after retries: {exc}") from exc

    @retry(
        retry=retry_if_exception_type(RetryableProviderError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _generate_with_retry(self, client: genai.Client, prompt: str, model: str) -> LLMResponse:
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
        except Exception as exc:
            if _is_transient(exc):
                raise RetryableProviderError(str(exc)) from exc
            raise ProviderUnavailable(f"Gemini request failed: {exc}") from exc
        text = (resp.text or "").strip()
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=getattr(resp.usage_metadata, "prompt_token_count", None),
            output_tokens=getattr(resp.usage_metadata, "candidates_token_count", None),
        )
