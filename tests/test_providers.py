"""Tests for the provider abstraction, cache wrapper, and Gemini provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from tenacity import wait_none

from gitmate.providers.base import LLMProvider, LLMResponse, ProviderUnavailable
from gitmate.providers.cache import CachedProvider, cache_key
from gitmate.providers.gemini import GeminiProvider


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eliminate tenacity exponential backoff delays during tests."""
    retry_obj = GeminiProvider._generate_with_retry.retry  # type: ignore[attr-defined]
    monkeypatch.setattr(retry_obj, "wait", wait_none())


class FakeProvider:
    """In-memory LLMProvider recording every call it actually receives."""

    def __init__(self, text: str = "generated") -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, model: str) -> LLMResponse:
        self.calls.append((prompt, model))
        return LLMResponse(text=self.text, model=model)


# --- cache ---


def test_cache_consulted_before_generate(tmp_path: Path) -> None:
    provider = FakeProvider()
    cached = CachedProvider(provider, cache_dir=tmp_path, ttl=None)

    first = cached.generate("prompt", "gemini-3.5-flash-lite")
    second = cached.generate("prompt", "gemini-3.5-flash-lite")

    assert first == second
    # The whole point of caching: one underlying call, not two.
    assert len(provider.calls) == 1
    cached.close()


def test_cache_miss_then_hit_across_prompts(tmp_path: Path) -> None:
    provider = FakeProvider()
    cached = CachedProvider(provider, cache_dir=tmp_path)
    cached.generate("a", "m")
    cached.generate("b", "m")
    assert len(provider.calls) == 2
    cached.close()


def test_cache_key_changes_with_model_and_template_version() -> None:
    base = cache_key("p", "m", "1")
    assert cache_key("p", "m", "2") != base
    assert cache_key("p", "other", "1") != base
    assert cache_key("q", "m", "1") != base
    assert cache_key("p", "m", "1") == base


def test_template_version_bump_misses_old_entry(tmp_path: Path) -> None:
    provider = FakeProvider()
    v1 = CachedProvider(provider, cache_dir=tmp_path, template_version="1")
    v1.generate("p", "m")
    assert len(provider.calls) == 1
    v1.close()

    v2 = CachedProvider(FakeProvider("v2"), cache_dir=tmp_path, template_version="2")
    resp = v2.generate("p", "m")
    assert resp.text == "v2"
    v2.close()


def test_cache_dir_override(tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    provider = FakeProvider()
    cached = CachedProvider(provider, cache_dir=custom)
    cached.generate("p", "m")
    assert (custom / "cache.db").exists()
    cached.close()


# --- Gemini provider ---


def _resp(text: str | None, finish_reason: Any = None) -> MagicMock:
    r = MagicMock()
    r.text = text
    meta = MagicMock()
    meta.prompt_token_count = 10
    meta.candidates_token_count = 5
    r.usage_metadata = meta
    if finish_reason is not None:
        cand = MagicMock()
        cand.finish_reason = finish_reason
        r.candidates = [cand]
    else:
        r.candidates = []
    return r


def test_cache_does_not_store_empty_response(tmp_path: Path) -> None:
    provider = FakeProvider(text="")
    cached = CachedProvider(provider, cache_dir=tmp_path)
    resp1 = cached.generate("p", "m")
    assert resp1.text == ""
    assert len(provider.calls) == 1

    # Empty response should not be cached; second call hits the provider again
    provider.text = "now valid"
    resp2 = cached.generate("p", "m")
    assert resp2.text == "now valid"
    assert len(provider.calls) == 2
    cached.close()


def test_gemini_provider_success() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _resp(" hello ")
    provider = GeminiProvider(client=client)
    resp = provider.generate("write a message", "gemini-3.5-flash-lite")
    assert resp.text == "hello"
    assert resp.model == "gemini-3.5-flash-lite"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5
    client.models.generate_content.assert_called_once_with(
        model="gemini-3.5-flash-lite", contents="write a message"
    )


def test_gemini_provider_empty_response_raises() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = _resp(None)
    provider = GeminiProvider(client=client)
    with pytest.raises(ProviderUnavailable, match="Gemini returned an empty response"):
        provider.generate("prompt", "gemini-3.5-flash-lite")


def test_gemini_provider_safety_declined_raises_with_finish_reason() -> None:
    client = MagicMock()
    reason = MagicMock()
    reason.name = "SAFETY"
    client.models.generate_content.return_value = _resp("", finish_reason=reason)
    provider = GeminiProvider(client=client)
    with pytest.raises(
        ProviderUnavailable, match=r"Gemini declined to respond \(finish_reason: SAFETY\)"
    ):
        provider.generate("prompt", "gemini-3.5-flash-lite")


def test_gemini_provider_no_api_key_or_client() -> None:
    with pytest.raises(ProviderUnavailable, match="API key is required"):
        GeminiProvider().generate("prompt", "gemini-3.5-flash-lite")


def test_gemini_provider_retries_transient_then_succeeds() -> None:
    from google.genai.errors import ServerError

    client = MagicMock()
    client.models.generate_content.side_effect = [
        ServerError(500, None, None),
        _resp("ok"),
    ]
    provider = GeminiProvider(client=client)
    assert provider.generate("p", "m").text == "ok"
    assert client.models.generate_content.call_count == 2


def test_gemini_provider_exhausts_retries() -> None:
    from google.genai.errors import ServerError

    client = MagicMock()
    client.models.generate_content.side_effect = ServerError(500, None, None)
    provider = GeminiProvider(client=client)
    with pytest.raises(ProviderUnavailable, match="unavailable after retries"):
        provider.generate("p", "m")
    assert client.models.generate_content.call_count == 3


def test_gemini_provider_non_transient_raises_without_retry() -> None:
    from google.genai.errors import ClientError

    client = MagicMock()
    client.models.generate_content.side_effect = ClientError(400, None, None)
    provider = GeminiProvider(client=client)
    with pytest.raises(ProviderUnavailable, match="request failed"):
        provider.generate("p", "m")
    assert client.models.generate_content.call_count == 1


def test_is_like_provider_protocol(tmp_path: Path) -> None:
    assert isinstance(GeminiProvider(), LLMProvider)
    cached = CachedProvider(FakeProvider(), cache_dir=tmp_path)
    assert isinstance(cached, LLMProvider)
    cached.close()
