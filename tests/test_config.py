"""Tests for config load/save and the secret-store interface."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitmate.config import (
    ConfigError,
    GitmateConfig,
    InMemorySecretStore,
    UnknownModelError,
    config_path,
    get_context_window,
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


def test_ignore_globs_round_trip(tmp_config_dir: Path) -> None:
    save_config(GitmateConfig(ignore_globs=["*.gen.py"]))
    assert load_config() == GitmateConfig(ignore_globs=["*.gen.py"])


def test_ignore_globs_wrong_type_raises(tmp_config_dir: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text('ignore_globs = "*.gen.py"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_cache_dir_round_trip(tmp_config_dir: Path) -> None:
    save_config(GitmateConfig(cache_dir="/tmp/gitmate-cache"))
    assert load_config().cache_dir == "/tmp/gitmate-cache"


def test_cache_dir_wrong_type_raises(tmp_config_dir: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text('cache_dir = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="'cache_dir' must be a non-empty string"):
        load_config()


def test_get_context_window_known_models() -> None:
    assert get_context_window(GitmateConfig(model="gemini-3.5-flash-lite")) == 1_048_576
    assert get_context_window(GitmateConfig(model="gemini-3.5-flash")) == 1_048_576
    assert get_context_window(GitmateConfig(model="gemini-flash")) == 1_048_576
    assert get_context_window(GitmateConfig(model="claude-3-5-sonnet")) == 200_000


def test_get_context_window_user_override() -> None:
    cfg = GitmateConfig(model="gemini-flash", max_context_tokens=50_000)
    assert get_context_window(cfg) == 50_000


def test_get_context_window_unknown_model_raises() -> None:
    cfg = GitmateConfig(model="unknown-model-xyz")
    with pytest.raises(UnknownModelError, match="unknown model 'unknown-model-xyz'"):
        get_context_window(cfg)
    assert issubclass(UnknownModelError, ConfigError)


def test_get_context_window_unknown_model_with_override() -> None:
    cfg = GitmateConfig(model="unknown-model-xyz", max_context_tokens=32_000)
    assert get_context_window(cfg) == 32_000


def test_budget_config_round_trip(tmp_config_dir: Path) -> None:
    cfg = GitmateConfig(
        max_context_tokens=64_000,
        reserved_output_tokens=4096,
        template_overhead=800,
    )
    save_config(cfg)
    loaded = load_config()
    assert loaded.max_context_tokens == 64_000
    assert loaded.reserved_output_tokens == 4096
    assert loaded.template_overhead == 800


@pytest.mark.parametrize("val", ['"big"', "true", "-100", "0"])
def test_invalid_max_context_tokens_raises(tmp_config_dir: Path, val: str) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(f"max_context_tokens = {val}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="'max_context_tokens' must be a positive integer"):
        load_config()


@pytest.mark.parametrize("val", ['"big"', "false", "-50", "0"])
def test_invalid_reserved_output_tokens_raises(tmp_config_dir: Path, val: str) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(f"reserved_output_tokens = {val}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="'reserved_output_tokens' must be a positive integer"):
        load_config()


@pytest.mark.parametrize("val", ['"big"', "false", "-50", "0"])
def test_invalid_template_overhead_raises(tmp_config_dir: Path, val: str) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(f"template_overhead = {val}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="'template_overhead' must be a positive integer"):
        load_config()


def test_max_context_tokens_must_exceed_reserved_plus_overhead(tmp_config_dir: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        "max_context_tokens = 2200\nreserved_output_tokens = 2048\n",
        encoding="utf-8",
    )
    with pytest.raises(
        ConfigError,
        match="must be greater than 'reserved_output_tokens' \\+ 'template_overhead'",
    ):
        load_config()
