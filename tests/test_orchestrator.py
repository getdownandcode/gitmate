"""Tests for commit message orchestration and graceful degradation on provider failure."""

from __future__ import annotations

from unittest.mock import MagicMock

from rich.console import Console

from gitmate.config import GitmateConfig, InMemorySecretStore
from gitmate.diff_extractor import FileDiff
from gitmate.fallback import generate_commit_message
from gitmate.providers.base import LLMResponse, ProviderUnavailable
from gitmate.token_budget import NoApiKeyError, TokenCounter


class FakeProvider:
    """In-memory LLMProvider for testing."""

    def __init__(self, response: str = "feat(cli): add commit command") -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str, model: str) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(
            text=self.response,
            model=model,
            input_tokens=150,
            output_tokens=25,
        )


class FailingProvider:
    """LLMProvider that simulates backend outage or rate limits."""

    def __init__(self, error_msg: str = "Gemini unavailable after retries: 503") -> None:
        self.error_msg = error_msg

    def generate(self, prompt: str, model: str) -> LLMResponse:
        raise ProviderUnavailable(self.error_msg)


class FakeCounter(TokenCounter):
    """Predictable in-memory token counter."""

    def __init__(self, token_rate: int = 1) -> None:
        self.token_rate = token_rate

    def count_tokens(self, text: str, model: str) -> int:
        return max(1, len(text) // self.token_rate)


class FailingCounter(TokenCounter):
    """TokenCounter simulating missing credentials or connection drop."""

    def count_tokens(self, text: str, model: str) -> int:
        raise NoApiKeyError("Missing API key for counting.")


def _sample_diff() -> list[FileDiff]:
    return [
        FileDiff(
            path="src/auth/login.py",
            old_path=None,
            status="added",
            additions=30,
            deletions=5,
            patch_text="@@ -1,5 +1,30 @@\n+class LoginService:\n+    pass",
            is_binary=False,
        )
    ]


def test_generate_commit_message_success() -> None:
    cfg = GitmateConfig(model="gemini-3.5-flash-lite", commit_style="conventional")
    provider = FakeProvider(response="feat(auth): add login service")
    counter = FakeCounter()

    result = generate_commit_message(
        diffs=_sample_diff(),
        cfg=cfg,
        provider=provider,
        counter=counter,
    )

    assert result.is_fallback is False
    assert result.text == "feat(auth): add login service"
    assert result.model == "gemini-3.5-flash-lite"
    assert result.input_tokens == 150
    assert result.output_tokens == 25
    assert result.fallback_reason is None
    assert len(provider.prompts) == 1
    assert "Diff:" in provider.prompts[0]
    assert "+class LoginService:" in provider.prompts[0]


def test_generate_commit_message_provider_failure_triggers_fallback() -> None:
    cfg = GitmateConfig(model="gemini-3.5-flash-lite", commit_style="conventional")
    provider = FailingProvider(error_msg="503 Service Unavailable")
    counter = FakeCounter()
    console = MagicMock(spec=Console)

    result = generate_commit_message(
        diffs=_sample_diff(),
        cfg=cfg,
        provider=provider,
        counter=counter,
        console=console,
    )

    assert result.is_fallback is True
    assert result.text == "feat(auth): add login.py"
    assert result.fallback_reason == "503 Service Unavailable"
    console.print.assert_called_once_with(
        "[yellow]⚠ API unavailable, using template fallback[/yellow]"
    )


def test_generate_commit_message_plain_style_fallback() -> None:
    cfg = GitmateConfig(model="gemini-3.5-flash-lite", commit_style="plain")
    provider = FailingProvider()
    counter = FakeCounter()

    result = generate_commit_message(
        diffs=_sample_diff(),
        cfg=cfg,
        provider=provider,
        counter=counter,
    )

    assert result.is_fallback is True
    assert result.text == "Add src/auth/login.py"


def test_generate_commit_message_empty_diffs() -> None:
    cfg = GitmateConfig()
    result = generate_commit_message(diffs=[], cfg=cfg)

    assert result.is_fallback is False
    assert result.text == "No changes staged for commit."


def test_generate_commit_message_counter_failure_triggers_fallback() -> None:
    cfg = GitmateConfig(commit_style="conventional")
    counter = FailingCounter()
    console = MagicMock(spec=Console)

    result = generate_commit_message(
        diffs=_sample_diff(),
        cfg=cfg,
        counter=counter,
        console=console,
    )

    assert result.is_fallback is True
    assert result.text == "feat(auth): add login.py"
    assert "Token budgeting failed" in (result.fallback_reason or "")
    console.print.assert_called_once_with(
        "[yellow]⚠ API unavailable, using template fallback[/yellow]"
    )


def test_generate_commit_message_custom_secret_store() -> None:
    cfg = GitmateConfig(commit_style="conventional")
    provider = FakeProvider(response="feat(auth): add login feature")
    counter = FakeCounter()
    store = InMemorySecretStore()
    store.set_secret("api-key", "test-key-123")

    result = generate_commit_message(
        diffs=_sample_diff(),
        cfg=cfg,
        provider=provider,
        counter=counter,
        secret_store=store,
    )

    assert result.is_fallback is False
    assert result.text == "feat(auth): add login feature"
