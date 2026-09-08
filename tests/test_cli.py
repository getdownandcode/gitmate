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
    for name in ("commit", "pr-summary", "changelog", "doc", "config", "debug-diff"):
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


def test_debug_diff_shows_table_and_patch(tmp_config_dir: Path, git_repo: Path) -> None:
    from conftest import commit_file, stage_file

    commit_file(git_repo, "a.py", "l1\n", "add a")
    stage_file(git_repo, "a.py", b"l1\nl2\n")
    result = runner.invoke(cli.app, ["debug-diff"])
    assert result.exit_code == 0
    assert "a.py" in result.output
    assert "@@" in result.output


def test_debug_diff_summary_omits_patch(tmp_config_dir: Path, git_repo: Path) -> None:
    from conftest import commit_file, stage_file

    commit_file(git_repo, "a.py", "l1\n", "add a")
    stage_file(git_repo, "a.py", b"l1\nl2\n")
    result = runner.invoke(cli.app, ["debug-diff", "--summary"])
    assert result.exit_code == 0
    assert "a.py" in result.output
    assert "@@" not in result.output


def test_debug_diff_branch_mode(tmp_config_dir: Path, git_repo: Path) -> None:
    import subprocess

    from conftest import commit_file

    commit_file(git_repo, "b.py", "v1\n", "base")
    subprocess.run(["git", "checkout", "-b", "f"], cwd=git_repo, check=True)
    commit_file(git_repo, "b.py", "v1\nv2\n", "bump")
    result = runner.invoke(cli.app, ["debug-diff", "--base", "main"])
    assert result.exit_code == 0
    assert "b.py" in result.output


def test_debug_diff_conflicting_modes_fail(tmp_config_dir: Path, git_repo: Path) -> None:
    result = runner.invoke(cli.app, ["debug-diff", "--base", "main", "--from-ref", "v1"])
    assert result.exit_code != 0
    result = runner.invoke(cli.app, ["debug-diff", "--from-ref", "v1"])
    assert result.exit_code != 0
