"""Tests for the Typer command surface (definitions only, no logic)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitmate import cli
from gitmate.config import (
    API_KEY_ACCOUNT,
    GitmateConfig,
    InMemorySecretStore,
    config_path,
    load_config,
    save_config,
)

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for name in ("commit", "pr-summary", "changelog", "doc", "config", "debug-diff"):
        assert name in result.output


def test_stubs_report_not_implemented() -> None:
    for name in ("pr-summary", "changelog", "doc"):
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


def test_config_show_displays_zero_budget(
    tmp_config_dir: Path, mem_store: InMemorySecretStore
) -> None:
    result = runner.invoke(cli.app, ["config", "set", "budget_cap_usd", "0"])
    assert result.exit_code == 0
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0
    assert "0.0" in result.output


def test_config_set_unknown_field_fails(tmp_config_dir: Path) -> None:
    result = runner.invoke(cli.app, ["config", "set", "nope", "x"])
    assert result.exit_code != 0


def test_config_set_ignore_globs_persists(tmp_config_dir: Path) -> None:
    result = runner.invoke(cli.app, ["config", "set", "ignore_globs", "*.gen.py, *.tmp"])
    assert result.exit_code == 0
    assert load_config().ignore_globs == ["*.gen.py", "*.tmp"]
    result = runner.invoke(cli.app, ["config", "set", "ignore_globs", ""])
    assert result.exit_code == 0
    assert load_config().ignore_globs == []


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


def test_config_show_displays_ignore_globs(
    tmp_config_dir: Path, mem_store: InMemorySecretStore
) -> None:
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0
    assert "ignore_globs" in result.output
    assert "none" in result.output

    save_config(GitmateConfig(ignore_globs=["*.lock", "dist/*"]))
    result2 = runner.invoke(cli.app, ["config", "show"])
    assert result2.exit_code == 0
    assert "*.lock, dist/*" in result2.output


def test_debug_diff_malformed_config_clean_error(tmp_config_dir: Path, git_repo: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("model = [invalid toml\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["debug-diff"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_debug_diff_not_a_git_repo_clean_error(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    non_repo = tmp_path / "not_a_repo"
    non_repo.mkdir()
    monkeypatch.chdir(non_repo)
    result = runner.invoke(cli.app, ["debug-diff"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "is not inside a git repository" in result.output
    assert "Traceback" not in result.output


def test_config_show_malformed_config_clean_error(tmp_config_dir: Path) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("model = [invalid toml\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_debug_diff_git_command_error_clean_error(tmp_config_dir: Path, git_repo: Path) -> None:
    result = runner.invoke(cli.app, ["debug-diff", "--base", "nonexistent-branch"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_config_set_and_show_all_dataclass_fields(
    tmp_config_dir: Path, mem_store: InMemorySecretStore
) -> None:
    runner.invoke(cli.app, ["config", "set", "max_context_tokens", "16000"])
    runner.invoke(cli.app, ["config", "set", "reserved_output_tokens", "1000"])
    runner.invoke(cli.app, ["config", "set", "template_overhead", "400"])
    runner.invoke(cli.app, ["config", "set", "cache_dir", "/custom/cache"])

    cfg = load_config()
    assert cfg.max_context_tokens == 16000
    assert cfg.reserved_output_tokens == 1000
    assert cfg.template_overhead == 400
    assert cfg.cache_dir == "/custom/cache"

    res = runner.invoke(cli.app, ["config", "show"])
    assert res.exit_code == 0
    assert "max_context_tokens" in res.output
    assert "16000" in res.output
    assert "cache_dir" in res.output
    assert "/custom/cache" in res.output


def test_config_set_budget_fields_validation(tmp_config_dir: Path) -> None:
    res = runner.invoke(cli.app, ["config", "set", "max_context_tokens", "0"])
    assert res.exit_code != 0
    assert "positive integer" in res.output

    res = runner.invoke(cli.app, ["config", "set", "max_context_tokens", "--", "-5"])
    assert res.exit_code != 0
    assert "positive integer" in res.output

    res = runner.invoke(cli.app, ["config", "set", "reserved_output_tokens", "not-a-number"])
    assert res.exit_code != 0
    assert "positive integer" in res.output

    # max_context_tokens <= reserved + overhead (default 2048 + 500 = 2548)
    res = runner.invoke(cli.app, ["config", "set", "max_context_tokens", "2000"])
    assert res.exit_code != 0
    assert "must be greater than" in res.output


def test_commit_cli_no_staged_diff_exits_zero(git_repo: Path, tmp_config_dir: Path) -> None:
    result = runner.invoke(cli.app, ["commit"])
    assert result.exit_code == 0
    assert "No staged changes" in result.output


def test_commit_cli_non_tty_without_yes_exits_one(git_repo: Path, tmp_config_dir: Path) -> None:
    from conftest import stage_file

    stage_file(git_repo, "a.py", b"x = 1\n")
    result = runner.invoke(cli.app, ["commit"])
    assert result.exit_code == 1
    assert "Interactive review requires a TTY terminal" in result.output


def test_commit_cli_yes_disabled_by_default(git_repo: Path, tmp_config_dir: Path) -> None:
    from conftest import stage_file

    stage_file(git_repo, "a.py", b"x = 1\n")
    result = runner.invoke(cli.app, ["commit", "--yes"])
    assert result.exit_code == 1
    assert "Non-interactive commit (--yes) is disabled by default" in result.output


def test_commit_cli_yes_enabled_in_config(
    git_repo: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import stage_file

    import gitmate.committer as committer_mod
    from gitmate.fallback import GenerationResult

    stage_file(git_repo, "a.py", b"x = 1\n")
    save_config(GitmateConfig(allow_noninteractive_commit=True))

    monkeypatch.setattr(
        committer_mod,
        "generate_commit_message",
        lambda *args, **kwargs: GenerationResult(
            text="feat: non-interactive cli commit",
            is_fallback=False,
            model="gemini-3.5-flash-lite",
        ),
    )

    result = runner.invoke(cli.app, ["commit", "--yes"])
    assert result.exit_code == 0


def test_config_set_allow_noninteractive_commit(tmp_config_dir: Path) -> None:
    # Set True
    res = runner.invoke(cli.app, ["config", "set", "allow_noninteractive_commit", "true"])
    assert res.exit_code == 0
    assert load_config().allow_noninteractive_commit is True

    # Set False
    res = runner.invoke(cli.app, ["config", "set", "allow_noninteractive_commit", "false"])
    assert res.exit_code == 0
    assert load_config().allow_noninteractive_commit is False

    # Case insensitive
    res = runner.invoke(cli.app, ["config", "set", "allow_noninteractive_commit", "TRUE"])
    assert res.exit_code == 0
    assert load_config().allow_noninteractive_commit is True

    # Invalid value
    res = runner.invoke(cli.app, ["config", "set", "allow_noninteractive_commit", "invalid"])
    assert res.exit_code != 0
    assert "must be 'true' or 'false'" in res.output


def test_config_set_free_tier(tmp_config_dir: Path) -> None:
    # Set True
    res = runner.invoke(cli.app, ["config", "set", "free_tier", "true"])
    assert res.exit_code == 0
    assert load_config().free_tier is True

    # Set False
    res = runner.invoke(cli.app, ["config", "set", "free_tier", "false"])
    assert res.exit_code == 0
    assert load_config().free_tier is False


def test_config_show_displays_allow_noninteractive_commit(tmp_config_dir: Path) -> None:
    res = runner.invoke(cli.app, ["config", "show"])
    assert res.exit_code == 0
    assert "allow_noninteractive_commit" in res.output
    assert "False" in res.output
    assert "free_tier" in res.output


def test_stats_cli_empty_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(tmp_path / "empty_metrics"))
    res = runner.invoke(cli.app, ["stats"])
    assert res.exit_code == 0
    assert "No invocations recorded yet" in res.output


def test_stats_cli_populated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gitmate.metrics import record_invocation

    m_dir = tmp_path / "cli_metrics"
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(m_dir))

    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=5000,
        tokens_out=500,
        cache_hit=False,
        latency_ms=450,
        fallback_used=False,
    )
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=5000,
        tokens_out=500,
        cache_hit=True,
        latency_ms=12,
        fallback_used=False,
    )

    # 1. Standard table
    res = runner.invoke(cli.app, ["stats"])
    assert res.exit_code == 0
    assert "gitmate stats (All Time)" in res.output
    assert "Total Invocations" in res.output
    assert "Cache Hits" in res.output
    assert "50.0%" in res.output
    assert "Estimated Spend" in res.output
    assert "By Command" in res.output

    # 2. Time-scoped
    res_month = runner.invoke(cli.app, ["stats", "--month"])
    assert res_month.exit_code == 0
    assert "gitmate stats (Current Month)" in res_month.output

    res_days = runner.invoke(cli.app, ["stats", "--days", "7"])
    assert res_days.exit_code == 0
    assert "gitmate stats (Last 7 Days)" in res_days.output


def test_stats_cli_raw_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from gitmate.metrics import record_invocation

    m_dir = tmp_path / "json_metrics"
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(m_dir))

    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=1000,
        tokens_out=100,
        cache_hit=True,
        latency_ms=15,
        fallback_used=False,
    )

    res = runner.invoke(cli.app, ["stats", "--raw"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["total_invocations"] == 1
    assert data["cache_hits"] == 1
    assert data["cache_hit_rate"] == 100.0
    assert "commit" in data["by_command"]


def test_stats_cli_raw_json_long_model_name_no_rewrapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure long model names (100+ chars) do not get line-wrapped and break JSON parsing."""
    import json

    from gitmate.metrics import record_invocation

    m_dir = tmp_path / "long_model_metrics"
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(m_dir))

    long_model = "custom-organization-deployment-fine-tuned-gemini-3-flash-preview-endpoint-accelerated-v2-long-identifier-string"
    assert len(long_model) > 100

    record_invocation(
        command="commit",
        model=long_model,
        tokens_in=5000,
        tokens_out=250,
        cache_hit=False,
        latency_ms=120,
        fallback_used=False,
    )

    res = runner.invoke(cli.app, ["stats", "--raw"])
    assert res.exit_code == 0
    # Must parse cleanly without JSONDecodeError caused by line-wrapping
    data = json.loads(res.output)
    assert long_model in data["by_model"]
    assert data["by_model"][long_model]["invocations"] == 1


def test_stats_cli_invalid_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(tmp_path / "metrics"))
    res = runner.invoke(cli.app, ["stats", "--days", "0"])
    assert res.exit_code != 0
    assert "positive integer" in res.output
