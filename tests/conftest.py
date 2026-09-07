"""Shared fixtures: isolated config dir and in-memory secret store."""

from __future__ import annotations

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
