"""Shared fixtures: isolated config dir, in-memory secrets, scratch git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitmate import cli
from gitmate.config import InMemorySecretStore


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point gitmate at a temp config dir; never touch ~ or the Keychain."""
    cfg_dir = tmp_path / "gitmate-config"
    monkeypatch.setenv("GITMATE_CONFIG_DIR", str(cfg_dir))
    return cfg_dir


@pytest.fixture
def mem_store(monkeypatch: pytest.MonkeyPatch) -> InMemorySecretStore:
    """Swap the CLI's secret store for an in-memory fake."""
    store = InMemorySecretStore()
    monkeypatch.setattr(cli, "secret_store", store)
    return store


def _git(repo: Path, *args: str) -> None:
    """Run git in repo, raising on failure."""
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scratch repo with real commits; rename detection forced off by default.

    `diff.renames=false` proves our explicit `-M` flag does the work, not the
    ambient git config of the machine running the tests.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "diff.renames", "false")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    monkeypatch.chdir(repo)
    return repo


def commit_file(repo: Path, name: str, content: str, message: str) -> None:
    """Write text content to name, stage, and commit it in repo."""
    full = repo / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)


def stage_file(repo: Path, name: str, data: bytes) -> None:
    """Write raw bytes to name and stage it in repo (no commit)."""
    full = repo / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    _git(repo, "add", name)
