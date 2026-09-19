"""Unit and integration tests for PR summary generation, clipboard, and gh CLI dispatch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import commit_file
from rich.console import Console

from gitmate.config import GitmateConfig
from gitmate.diff_extractor import GitCommandError
from gitmate.metrics import get_invocations, record_invocation
from gitmate.orchestrator import BudgetCapExceededError
from gitmate.pr_summary import (
    GitHubCliError,
    copy_to_clipboard,
    create_github_pr,
    generate_pr_summary,
)
from gitmate.providers.base import LLMProvider, LLMResponse, ProviderUnavailable


class FakeProvider(LLMProvider):
    """In-memory provider recording calls and returning static responses."""

    def __init__(self, response: str = "## Summary\nImplemented PR summary.") -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, model: str) -> LLMResponse:
        self.calls.append((prompt, model))
        return LLMResponse(
            text=self.response,
            model=model,
            input_tokens=120,
            output_tokens=40,
        )


class FailingProvider(LLMProvider):
    """Provider that simulates an API outage."""

    def generate(self, prompt: str, model: str) -> LLMResponse:
        raise ProviderUnavailable("API is down")


@pytest.fixture
def metrics_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate metrics database in temp directory."""
    m_dir = tmp_path / "metrics"
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(m_dir))
    return m_dir


def test_copy_to_clipboard_success() -> None:
    with patch("pyperclip.copy") as mock_copy:
        success = copy_to_clipboard("test text")
        assert success is True
        mock_copy.assert_called_once_with("test text")


def test_copy_to_clipboard_failure_graceful() -> None:
    with patch("pyperclip.copy", side_effect=Exception("No display")):
        con = Console(record=True)
        success = copy_to_clipboard("test text", console=con)
        assert success is False
        assert "Clipboard unavailable" in con.export_text()


def test_create_github_pr_missing_gh() -> None:
    with patch("shutil.which", return_value=None):
        con = Console(record=True)
        code = create_github_pr(base="main", summary="PR body", console=con)
        assert code == 1
        assert "GitHub CLI ('gh') not found" in con.export_text()


def test_create_github_pr_success() -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        code = create_github_pr(base="main", summary="PR body", title="feat: test")
        assert code == 0
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--body",
            "PR body",
            "--title",
            "feat: test",
        ]


def test_pr_summary_no_diffs(git_repo: Path) -> None:
    res = generate_pr_summary(base="main", repo_dir=git_repo)
    assert res.is_fallback is False
    assert "No changes" in res.text


def test_pr_summary_with_diffs_and_telemetry(git_repo: Path, metrics_dir: Path) -> None:
    # Create a feature branch with a commit
    subprocess.run(["git", "checkout", "-b", "feature-x"], cwd=git_repo, check=True)
    commit_file(git_repo, "hello.py", "print('hello')", "feat: add hello")

    provider = FakeProvider(response="## Summary\nAdded greeting script.")
    cfg = GitmateConfig(model="gemini-3-flash-preview")

    res = generate_pr_summary(
        base="main",
        cfg=cfg,
        provider=provider,
        repo_dir=git_repo,
        copy_to_cb=False,
    )

    assert res.is_fallback is False
    assert res.text == "## Summary\nAdded greeting script."
    assert len(provider.calls) == 1
    assert "hello.py" in provider.calls[0][0]

    # Verify telemetry was recorded
    invocations = get_invocations()
    assert len(invocations) == 1
    row = invocations[0]
    assert row.command == "pr_summary"
    assert row.model == "gemini-3-flash-preview"
    assert row.tokens_in == 120
    assert row.tokens_out == 40
    assert row.fallback_used == 0


def test_pr_summary_fallback_on_provider_error(git_repo: Path, metrics_dir: Path) -> None:
    subprocess.run(["git", "checkout", "-b", "feature-err"], cwd=git_repo, check=True)
    commit_file(git_repo, "feat.py", "x = 1\n", "feat: new feature")

    provider = FailingProvider()
    cfg = GitmateConfig(model="gemini-3-flash-preview")

    res = generate_pr_summary(
        base="main",
        cfg=cfg,
        provider=provider,
        repo_dir=git_repo,
        copy_to_cb=False,
    )

    assert res.is_fallback is True
    assert "## Summary" in res.text
    assert "## Changes" in res.text
    assert "feat.py" in res.text

    invocations = get_invocations()
    assert len(invocations) == 1
    assert invocations[0].command == "pr_summary"
    assert invocations[0].fallback_used == 1


def test_pr_summary_budget_cap_exceeded(git_repo: Path, metrics_dir: Path) -> None:
    # Seed high spend to exceed cap
    record_invocation(
        command="commit",
        model="gemini-3-flash-preview",
        tokens_in=1_000_000,
        tokens_out=200_000,
        cache_hit=False,
        latency_ms=500,
        fallback_used=False,
        estimated_cost_usd=10.0,
    )

    cfg = GitmateConfig(budget_cap_usd=5.0)
    con = Console(record=True)
    with pytest.raises(BudgetCapExceededError, match="budget cap exceeded"):
        generate_pr_summary(base="main", cfg=cfg, console=con, repo_dir=git_repo)
    assert "budget cap exceeded" in con.export_text().lower()


def test_pr_summary_git_error(git_repo: Path) -> None:
    con = Console(record=True)
    with pytest.raises(GitCommandError):
        generate_pr_summary(base="non-existent-branch-xyz", console=con, repo_dir=git_repo)


def test_pr_summary_gh_cli_error(git_repo: Path) -> None:
    subprocess.run(["git", "checkout", "-b", "feature-gh"], cwd=git_repo, check=True)
    commit_file(git_repo, "feat_gh.py", "x = 42\n", "feat: test gh error")

    provider = FakeProvider()
    with (
        patch("gitmate.pr_summary.create_github_pr", return_value=1),
        pytest.raises(GitHubCliError, match="exit code 1"),
    ):
        generate_pr_summary(
            base="main",
            provider=provider,
            repo_dir=git_repo,
            copy_to_cb=False,
            create_pr=True,
        )
