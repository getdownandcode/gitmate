"""Tests for the Typer command surface (definitions only, no logic)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from gitmate import cli
from gitmate.config import API_KEY_ACCOUNT, InMemorySecretStore, load_config

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for name in ("commit", "pr-summary", "changelog", "doc", "config"):
        assert name in result.output


def test_stubs_report_not_implemented() -> None:
    for name in ("commit", "pr-summary", "changelog", "doc"):
        result = runner.invoke(cli.app, [name])
        assert result.exit_code == 0
        assert "not implemented" in result.output


def test_config_show_reports_key_status(
    tmp_config_dir: Path, mem_store: InMemorySecretStore
) -> None:
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0
    assert "not set" in result.output
    mem_store.set_secret(API_KEY_ACCOUNT, "x")
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0
    assert "api_key" in result.output


def test_config_set_key_stores_secret(tmp_config_dir: Path, mem_store: InMemorySecretStore) -> None:
    result = runner.invoke(cli.app, ["config", "set-key"], input="s3cr3t\ns3cr3t\n")
    assert result.exit_code == 0
    assert mem_store.get_secret(API_KEY_ACCOUNT) == "s3cr3t"


def test_config_set_field_persists(tmp_config_dir: Path) -> None:
    result = runner.invoke(cli.app, ["config", "set", "model", "my-model"])
    assert result.exit_code == 0
    assert load_config().model == "my-model"


def test_config_set_unknown_field_fails(tmp_config_dir: Path) -> None:
    result = runner.invoke(cli.app, ["config", "set", "nope", "x"])
    assert result.exit_code != 0
