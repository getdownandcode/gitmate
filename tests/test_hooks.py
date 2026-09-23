"""Real-repository tests for crash-safe prepare-commit-msg integration."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitmate import hooks as hooks_mod
from gitmate.cli import app
from gitmate.config import GitmateConfig, save_config
from gitmate.hooks import HOOK_MARKER, HookError, install_hook, uninstall_hook
from gitmate.metrics import get_invocations


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git in a scratch repository."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=check,
    )


def _init_repo(repo: Path) -> None:
    """Create a repository configured for editor-free integration tests."""
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Hook Test")
    _git(repo, "config", "user.email", "hook@example.test")
    _git(repo, "commit", "--allow-empty", "-m", "initial")


def _commit(repo: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a commit with an inert editor and isolated process environment."""
    return subprocess.run(
        ["git", "commit", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


@pytest.fixture
def hook_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Isolate config, metrics, and cache for hook subprocesses."""
    env = os.environ.copy()
    env.update(
        {
            "GITMATE_CONFIG_DIR": str(tmp_path / "config"),
            "GITMATE_METRICS_DIR": str(tmp_path / "metrics"),
            "GIT_EDITOR": "true",
        }
    )
    env.pop("GEMINI_API_KEY", None)
    monkeypatch.setenv("GITMATE_CONFIG_DIR", env["GITMATE_CONFIG_DIR"])
    monkeypatch.setenv("GITMATE_METRICS_DIR", env["GITMATE_METRICS_DIR"])
    return env


def test_install_is_executable_idempotent_and_uninstallable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    path = install_hook(repo)
    original = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o111
    assert original.startswith("#!/bin/sh\n")
    assert sys.executable in original
    assert HOOK_MARKER in original

    assert install_hook(repo) == path
    assert path.read_text(encoding="utf-8") == original
    assert uninstall_hook(repo) == path
    assert not path.exists()


def test_reinstall_rewrites_gitmate_hook_to_current_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reinstall overwrites a gitmate-marked hook so upgrades fix stale interpreter paths."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = install_hook(repo)
    path.write_text(f"#!/bin/sh\n{HOOK_MARKER}\nold python path\n", encoding="utf-8")
    current_script = f"#!/bin/sh\n{HOOK_MARKER}\ncurrent python path\n"
    monkeypatch.setattr(hooks_mod, "HOOK_SCRIPT", current_script)

    install_hook(repo)

    assert path.read_text(encoding="utf-8") == current_script
    assert path.stat().st_mode & 0o111


def test_install_refuses_to_overwrite_existing_hook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = repo / ".git" / "hooks" / "prepare-commit-msg"
    path.write_text("#!/bin/sh\necho user-hook\n", encoding="utf-8")

    with pytest.raises(HookError, match="already exists"):
        install_hook(repo)
    assert path.read_text(encoding="utf-8") == "#!/bin/sh\necho user-hook\n"


def test_uninstall_refuses_to_remove_user_hook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = repo / ".git" / "hooks" / "prepare-commit-msg"
    path.write_text("#!/bin/sh\necho user-hook\n", encoding="utf-8")

    with pytest.raises(HookError, match="not a gitmate-managed"):
        uninstall_hook(repo)
    assert path.exists()


def test_cli_installs_and_uninstalls_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)
    runner = CliRunner()

    installed = runner.invoke(app, ["install-hook"])
    hook_path = repo / ".git" / "hooks" / "prepare-commit-msg"
    assert installed.exit_code == 0, installed.output
    assert hook_path.exists()

    removed = runner.invoke(app, ["uninstall-hook"])
    assert removed.exit_code == 0, removed.output
    assert not hook_path.exists()


def test_pre_commit_source_environment_is_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message_file = tmp_path / "COMMIT_EDITMSG"
    message_file.write_text("User message\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        commands.append(command)

    monkeypatch.setattr(sys, "argv", ["gitmate-hook", str(message_file)])
    monkeypatch.setenv("PRE_COMMIT_COMMIT_MSG_SOURCE", "message")
    monkeypatch.setenv("PRE_COMMIT_COMMIT_OBJECT_NAME", "deadbeef")
    monkeypatch.setattr("gitmate.hooks.subprocess.run", fake_run)

    assert hooks_mod.main() == 0
    assert commands[0][-3:] == [str(message_file), "message", "deadbeef"]


def test_real_commit_gets_fallback_message_and_hook_metrics(
    tmp_path: Path, hook_env: dict[str, str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    install_hook(repo)
    (repo / "feature.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "add", "feature.py")

    result = _commit(repo, env=hook_env)
    assert result.returncode == 0, result.stderr
    subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "feat: add feature.py"
    rows = get_invocations()
    assert len(rows) == 1
    assert rows[0].command == "commit-hook"
    assert rows[0].fallback_used


def test_budget_cap_uses_template_fallback_in_real_hook(
    tmp_path: Path, hook_env: dict[str, str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    install_hook(repo)
    save_config(GitmateConfig(budget_cap_usd=0))
    (repo / "feature.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "add", "feature.py")

    result = _commit(repo, env=hook_env)
    assert result.returncode == 0, result.stderr
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "feat: add feature.py"
    [record] = get_invocations()
    assert record.command == "commit-hook"
    assert record.fallback_used


def test_replace_message_removes_old_orphaned_temporary_files(tmp_path: Path) -> None:
    """A SIGKILLed worker leaves .COMMIT_EDITMSG.* orphans; the next write prunes them."""
    message_file = tmp_path / "COMMIT_EDITMSG"
    message_file.write_text("original\n", encoding="utf-8")
    old_temp = tmp_path / ".COMMIT_EDITMSG.abandoned"
    current_temp = tmp_path / ".COMMIT_EDITMSG.current"
    old_temp.write_text("partial\n", encoding="utf-8")
    current_temp.write_text("active\n", encoding="utf-8")
    old = time.time() - hooks_mod.HOOK_TIMEOUT_SECONDS - 2
    os.utime(old_temp, (old, old))

    hooks_mod._replace_message(message_file, "new message\n")

    assert not old_temp.exists()
    assert current_temp.exists()
    assert message_file.read_text(encoding="utf-8") == "new message\n"


def test_message_source_is_untouched(tmp_path: Path, hook_env: dict[str, str]) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    install_hook(repo)
    (repo / "feature.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "add", "feature.py")

    result = _commit(repo, "-m", "user supplied message", env=hook_env)
    assert result.returncode == 0, result.stderr
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "user supplied message"
    assert get_invocations() == []


def test_real_merge_message_is_untouched(tmp_path: Path, hook_env: dict[str, str]) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    install_hook(repo)
    _git(repo, "checkout", "-b", "side")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-m", "side change")
    _git(repo, "checkout", "main")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-m", "main change")

    result = subprocess.run(
        ["git", "merge", "--no-ff", "side"],
        cwd=repo,
        text=True,
        capture_output=True,
        env=hook_env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject.startswith("Merge branch 'side'")
    assert get_invocations() == []


def _install_fake_worker(tmp_path: Path, mode: str, env: dict[str, str]) -> None:
    """Shadow only the worker module to simulate a crash or a hung provider."""
    package = tmp_path / "shadow" / "gitmate"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "hooks.py").write_text(
        "import os, time\n"
        "mode = os.environ['GITMATE_TEST_WORKER']\n"
        "if mode == 'crash': raise RuntimeError('simulated worker crash')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = str(tmp_path / "shadow")
    env["GITMATE_TEST_WORKER"] = mode


@pytest.mark.parametrize("mode", ["crash", "slow"])
def test_worker_failure_and_timeout_do_not_block_real_commit(
    tmp_path: Path, hook_env: dict[str, str], mode: str
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    install_hook(repo)
    template = tmp_path / "commit-template.txt"
    template.write_text("Use the repository template\n", encoding="utf-8")
    _git(repo, "config", "commit.template", str(template))
    editor = tmp_path / "save-message.sh"
    editor.write_text("#!/bin/sh\nprintf '\\nReviewed by test.\\n' >> \"$1\"\n", encoding="utf-8")
    editor.chmod(0o755)
    hook_env["GIT_EDITOR"] = str(editor)
    (repo / "file.txt").write_text("change\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _install_fake_worker(tmp_path, mode, hook_env)

    started = time.monotonic()
    result = _commit(repo, env=hook_env)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "Use the repository template"
    assert elapsed < 8


def test_commit_preserves_template_and_prepends_message(
    tmp_path: Path, hook_env: dict[str, str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    install_hook(repo)
    template = tmp_path / "commit-template.txt"
    template.write_text("Ticket: JIRA-123\nReviewed-by: Lead\n", encoding="utf-8")
    _git(repo, "config", "commit.template", str(template))

    (repo / "auth.py").write_text("def auth(): pass\n", encoding="utf-8")
    _git(repo, "add", "auth.py")

    result = _commit(repo, env=hook_env)
    assert result.returncode == 0, result.stderr
    full_message = _git(repo, "log", "-1", "--format=%B").stdout
    assert full_message.startswith("feat: add auth.py")
    assert "Ticket: JIRA-123" in full_message
    assert "Reviewed-by: Lead" in full_message


def test_commit_command_has_no_hook_arguments() -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["commit", "--help"])
    assert help_result.exit_code == 0
    assert "hook-mode" not in help_result.output
    assert "commit_msg_file" not in help_result.output

    unexpected_arg = runner.invoke(app, ["commit", "some_file.py"])
    assert unexpected_arg.exit_code != 0
