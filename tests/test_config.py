"""Tests for config load/save and the secret-store interface."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitmate.config import (
    ConfigError,
    GitmateConfig,
    InMemorySecretStore,
    config_path,
    load_config,
    save_config,
)


def test_missing_file_returns_defaults(tmp_config_dir: Path) -> None:
    assert load_config() == GitmateConfig()


def test_save_then_load_round_trip(tmp_config_dir: Path) -> None:
    cfg = GitmateConfig(model="test-model", commit_style="plain", budget_cap_usd=12.5)
    save_config(cfg)
    assert config_path().exists()
    assert load_config() == cfg


def test_missing_fields_fall_back_to_defaults(tmp_config_dir: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text('model = "other-model"\n', encoding="utf-8")
    assert load_config() == GitmateConfig(model="other-model")


def test_unknown_keys_are_ignored(tmp_config_dir: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text('model = "m"\nfuture_key = 1\n', encoding="utf-8")
    assert load_config().model == "m"


def test_malformed_toml_raises(tmp_config_dir: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("model = [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_wrong_types_raise(tmp_config_dir: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text('budget_cap_usd = "lots"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_memory_store_round_trip() -> None:
    store = InMemorySecretStore()
    assert store.get_secret("api-key") is None
    store.set_secret("api-key", "s3cr3t")
    assert store.get_secret("api-key") == "s3cr3t"
