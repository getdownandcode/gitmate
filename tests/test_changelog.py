"""Unit and integration tests for changelog generation, commit parsing, grouping, and output."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import commit_file
from rich.console import Console

from gitmate.changelog import (
    CommitLogEntry,
    extract_commits_between,
    format_commits_for_prompt,
    generate_changelog,
    group_commits,
    parse_commit_message,
)
from gitmate.config import GitmateConfig
from gitmate.metrics import get_invocations, record_invocation
from gitmate.providers.base import LLMProvider, LLMResponse, ProviderUnavailable


class FakeProvider(LLMProvider):
    """In-memory provider recording calls and returning static responses."""

    def __init__(self, response: str = "# Release Notes\n\n## Features\n- New feature") -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, model: str) -> LLMResponse:
        self.calls.append((prompt, model))
        return LLMResponse(
            text=self.response,
            model=model,
            input_tokens=200,
            output_tokens=50,
        )


class FailingProvider(LLMProvider):
    """Provider simulating API outage."""

    def generate(self, prompt: str, model: str) -> LLMResponse:
        raise ProviderUnavailable("API connection refused")


@pytest.fixture
def metrics_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate metrics database in temp directory."""
    m_dir = tmp_path / "metrics"
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(m_dir))
    return m_dir


def test_parse_commit_message_conventional_with_scope() -> None:
    entry = parse_commit_message(
        commit_hash="abc1234567890",
        subject="feat(auth): implement oauth2 flow",
        body="Body details",
        author="Alice",
        date="2026-09-18",
    )
    assert entry.commit_type == "feat"
    assert entry.scope == "auth"
    assert entry.description == "implement oauth2 flow"
    assert entry.author == "Alice"


def test_parse_commit_message_conventional_breaking() -> None:
    entry = parse_commit_message(
        commit_hash="def456",
        subject="fix(db)!: drop legacy table",
    )
    assert entry.commit_type == "fix"
    assert entry.scope == "db"
    assert entry.description == "drop legacy table"


def test_parse_commit_message_non_conventional() -> None:
    entry = parse_commit_message(
        commit_hash="ghi789",
        subject="random cleanup commit without prefix",
    )
    assert entry.commit_type is None
    assert entry.scope is None
    assert entry.description == "random cleanup commit without prefix"


def test_group_commits_categorization_and_other_changes() -> None:
    commits = [
        CommitLogEntry("1", "feat(ui): new button", "", "Dev", "", "feat", "ui", "new button"),
        CommitLogEntry("2", "fix: fix off by one", "", "Dev", "", "fix", None, "fix off by one"),
        CommitLogEntry("3", "docs: update readme", "", "Dev", "", "docs", None, "update readme"),
        CommitLogEntry(
            "4", "refactor: clean up loop", "", "Dev", "", "refactor", None, "clean up loop"
        ),
        CommitLogEntry("5", "chore: bump deps", "", "Dev", "", "chore", None, "bump deps"),
        CommitLogEntry(
            "6", "unformatted message", "", "Dev", "", None, None, "unformatted message"
        ),
    ]

    grouped = group_commits(commits)
    assert len(grouped["Features"]) == 1
    assert len(grouped["Bug Fixes"]) == 1
    assert len(grouped["Documentation"]) == 1
    assert len(grouped["Performance & Refactoring"]) == 1
    assert len(grouped["Maintenance & Tooling"]) == 1
    assert len(grouped["Other Changes"]) == 1

    # Verify zero dropped commits
    total_grouped = sum(len(items) for items in grouped.values())
    assert total_grouped == len(commits)


def test_format_commits_for_prompt() -> None:
    commits = [
        CommitLogEntry(
            "123456789", "feat(cli): add stats", "", "Dev", "", "feat", "cli", "add stats"
        ),
    ]
    grouped = group_commits(commits)
    formatted = format_commits_for_prompt(grouped)
    assert "### Features" in formatted
    assert "[1234567] feat(cli): add stats" in formatted


def test_extract_commits_between_real_git(git_repo: Path) -> None:
    subprocess.run(["git", "tag", "v1.0.0"], cwd=git_repo, check=True)
    commit_file(git_repo, "a.py", "a=1", "feat(core): add module a")
    commit_file(git_repo, "b.py", "b=2", "fix(core): fix bug in b")
    subprocess.run(["git", "tag", "v1.1.0"], cwd=git_repo, check=True)

    commits = extract_commits_between("v1.0.0", "v1.1.0", repo=git_repo)
    assert len(commits) == 2
    subjects = [c.subject for c in commits]
    assert "fix(core): fix bug in b" in subjects
    assert "feat(core): add module a" in subjects


def test_generate_changelog_empty_range(git_repo: Path) -> None:
    res = generate_changelog(from_ref="HEAD", to_ref="HEAD", repo_dir=git_repo)
    assert res.is_fallback is False
    assert "No commits found" in res.text


def test_generate_changelog_with_mock_provider(git_repo: Path, metrics_dir: Path) -> None:
    subprocess.run(["git", "tag", "v1.0.0"], cwd=git_repo, check=True)
    commit_file(git_repo, "feat.py", "feat = True", "feat(api): expose new endpoint")
    subprocess.run(["git", "tag", "v1.1.0"], cwd=git_repo, check=True)

    provider = FakeProvider(response="# Release Notes v1.1.0\n\n## Features\n- New API endpoint")
    cfg = GitmateConfig(model="gemini-3-flash-preview")

    res = generate_changelog(
        from_ref="v1.0.0",
        to_ref="v1.1.0",
        cfg=cfg,
        provider=provider,
        repo_dir=git_repo,
    )

    assert res.is_fallback is False
    assert "Release Notes v1.1.0" in res.text
    assert len(provider.calls) == 1
    assert "expose new endpoint" in provider.calls[0][0]

    # Verify telemetry was recorded
    invocations = get_invocations()
    assert len(invocations) == 1
    assert invocations[0].command == "changelog"
    assert invocations[0].model == "gemini-3-flash-preview"
    assert invocations[0].tokens_in == 200
    assert invocations[0].tokens_out == 50


def test_generate_changelog_fallback_on_provider_error(git_repo: Path, metrics_dir: Path) -> None:
    subprocess.run(["git", "tag", "v1.0.0"], cwd=git_repo, check=True)
    commit_file(git_repo, "fix.py", "fix = True", "fix: correct calculation")
    subprocess.run(["git", "tag", "v1.1.0"], cwd=git_repo, check=True)

    provider = FailingProvider()
    cfg = GitmateConfig(model="gemini-3-flash-preview")

    res = generate_changelog(
        from_ref="v1.0.0",
        to_ref="v1.1.0",
        cfg=cfg,
        provider=provider,
        repo_dir=git_repo,
    )

    assert res.is_fallback is True
    assert "# Release Notes (v1.0.0..v1.1.0)" in res.text
    assert "## Bug Fixes" in res.text
    assert "correct calculation" in res.text

    invocations = get_invocations()
    assert len(invocations) == 1
    assert invocations[0].command == "changelog"
    assert invocations[0].fallback_used == 1


def test_generate_changelog_writes_to_file(git_repo: Path, tmp_path: Path) -> None:
    subprocess.run(["git", "tag", "v1.0.0"], cwd=git_repo, check=True)
    commit_file(git_repo, "c.py", "c = 3", "chore: setup config")
    subprocess.run(["git", "tag", "v1.1.0"], cwd=git_repo, check=True)

    out_file = tmp_path / "CHANGELOG.md"
    provider = FakeProvider(response="# Release Notes\n- Done")

    res = generate_changelog(
        from_ref="v1.0.0",
        to_ref="v1.1.0",
        output_file=out_file,
        provider=provider,
        repo_dir=git_repo,
    )

    assert out_file.exists()
    assert out_file.read_text(encoding="utf-8").strip() == res.text.strip()


def test_generate_changelog_budget_cap_exceeded(git_repo: Path, metrics_dir: Path) -> None:
    record_invocation(
        command="changelog",
        model="gemini-3-flash-preview",
        tokens_in=1_000_000,
        tokens_out=200_000,
        cache_hit=False,
        latency_ms=500,
        fallback_used=False,
        estimated_cost_usd=15.0,
    )

    cfg = GitmateConfig(budget_cap_usd=10.0)
    con = Console(record=True)
    res = generate_changelog(
        from_ref="HEAD~1", to_ref="HEAD", cfg=cfg, console=con, repo_dir=git_repo
    )

    assert res.is_fallback is True
    assert "Monthly budget cap exceeded" in res.text
    assert "Monthly budget cap exceeded" in con.export_text()
