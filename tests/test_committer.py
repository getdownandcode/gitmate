"""Tests for commit review flow, non-interactive guardrails, editor handling, and temp files."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from conftest import stage_file
from rich.console import Console

from gitmate.committer import _resolve_editor, commit_flow
from gitmate.config import GitmateConfig
from gitmate.diff_extractor import DiffExtractor, GitCommandError
from gitmate.metrics import get_invocations
from gitmate.providers.base import LLMProvider, LLMResponse


class FakeProvider(LLMProvider):
    """In-memory LLM provider recording invocations and returning programmed responses."""

    def __init__(
        self, responses: list[str] | str = "feat(core): initial generated message"
    ) -> None:
        if isinstance(responses, str):
            self.responses = [responses]
        else:
            self.responses = list(responses)
        self.call_count = 0
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, model: str) -> LLMResponse:
        self.calls.append((prompt, model))
        idx = min(self.call_count, len(self.responses) - 1)
        resp = self.responses[idx]
        self.call_count += 1
        return LLMResponse(
            text=resp,
            model=model,
            input_tokens=100,
            output_tokens=20,
        )


@pytest.fixture
def metrics_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate telemetry in temp directory."""
    m_dir = tmp_path / "metrics"
    m_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(m_dir))
    return m_dir


def _get_git_log_messages(repo: Path) -> list[str]:
    """Retrieve commit subjects and bodies from git log."""
    res = subprocess.run(
        ["git", "log", "--format=%B%x00"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return [msg.strip() for msg in res.stdout.split("\x00") if msg.strip()]


# --- 1. Empty Staged Diff ---


def test_empty_staged_diff_exits_zero(git_repo: Path, metrics_dir: Path) -> None:
    provider = FakeProvider()
    code = commit_flow(provider=provider)
    assert code == 0
    assert provider.call_count == 0
    assert len(get_invocations()) == 0


# --- 2. Non-TTY Guardrail ---


def test_non_tty_without_yes_exits_one(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "file.txt", b"content\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    provider = FakeProvider()
    code = commit_flow(yes=False, provider=provider)
    assert code == 1
    assert provider.call_count == 0
    assert len(get_invocations()) == 0


# --- 3. Non-interactive --yes Guardrails ---


def test_yes_with_allow_noninteractive_disabled_exits_one(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "file.txt", b"content\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    cfg = GitmateConfig(allow_noninteractive_commit=False)
    provider = FakeProvider()
    code = commit_flow(yes=True, cfg=cfg, provider=provider)
    assert code == 1
    assert provider.call_count == 0
    assert len(get_invocations()) == 0


def test_yes_with_allow_noninteractive_enabled_commits_directly(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "file.txt", b"content\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    cfg = GitmateConfig(allow_noninteractive_commit=True)
    provider = FakeProvider("feat(auto): unattended commit")
    code = commit_flow(yes=True, cfg=cfg, provider=provider)
    assert code == 0
    assert provider.call_count == 1

    messages = _get_git_log_messages(git_repo)
    assert messages[0] == "feat(auto): unattended commit"

    invocations = get_invocations()
    assert len(invocations) == 1
    assert invocations[0].command == "commit"


# --- 4. Interactive Review: [a]ccept ---


def test_interactive_accept_commits_message(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "foo.py", b"print('foo')\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": "a")

    provider = FakeProvider("feat(foo): add foo script")
    code = commit_flow(console=con, provider=provider)
    assert code == 0

    messages = _get_git_log_messages(git_repo)
    assert messages[0] == "feat(foo): add foo script"

    invocations = get_invocations()
    assert len(invocations) == 1
    assert invocations[0].command == "commit"


# --- 5. Interactive Review: [c]ancel ---


def test_interactive_cancel_exits_zero_without_commit(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "bar.py", b"print('bar')\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": "c")

    provider = FakeProvider("feat(bar): candidate message")
    code = commit_flow(console=con, provider=provider)
    assert code == 0

    # No new commit created (only initial commit from git_repo fixture)
    messages = _get_git_log_messages(git_repo)
    assert len(messages) == 1
    assert messages[0] == "init"

    # Telemetry IS recorded on generation attempt even if cancelled!
    invocations = get_invocations()
    assert len(invocations) == 1
    assert invocations[0].command == "commit"


# --- 6. Interactive Review: Empty Input & EOF ---


def test_interactive_empty_input_reprompts(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "baz.py", b"print('baz')\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    inputs = iter(["", "  ", "a"])
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    provider = FakeProvider("feat(baz): add baz")
    code = commit_flow(console=con, provider=provider)
    assert code == 0

    messages = _get_git_log_messages(git_repo)
    assert messages[0] == "feat(baz): add baz"


def test_interactive_eof_or_interrupt_cancels_cleanly(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "baz.py", b"print('baz')\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _raise_eof(prompt: str = "") -> str:
        raise EOFError()

    con = Console()
    monkeypatch.setattr(con, "input", _raise_eof)

    code = commit_flow(console=con, provider=FakeProvider())
    assert code == 0
    messages = _get_git_log_messages(git_repo)
    assert len(messages) == 1  # No commit created


# --- 7. Interactive Review: [r]egenerate ---


def test_interactive_regenerate_bypasses_cache_and_logs_telemetry(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "regen.py", b"def regen(): pass\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    inputs = iter(["r", "a"])
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    provider = FakeProvider(
        [
            "feat: first proposal",
            "feat(regen): better regenerated proposal",
        ]
    )
    code = commit_flow(console=con, provider=provider)
    assert code == 0
    assert provider.call_count == 2

    messages = _get_git_log_messages(git_repo)
    assert messages[0] == "feat(regen): better regenerated proposal"

    invocations = get_invocations()
    assert len(invocations) == 2
    assert invocations[0].cache_hit is False
    assert invocations[1].cache_hit is False


# --- 8. Interactive Review: [e]dit with Compound $EDITOR ---


def test_interactive_edit_compound_editor_and_comment_stripping(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "edit.py", b"val = 1\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    # Compound editor: python -c "<script>" --custom-flag
    editor_script = (
        "import sys, pathlib;"
        "target = pathlib.Path(sys.argv[1]);"
        "target.write_text('feat(edit): edited line\\n\\n# comment line\\n# second comment\\n')"
    )
    monkeypatch.setenv("EDITOR", f'{sys.executable} -c "{editor_script}"')

    inputs = iter(["e", "a"])
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    provider = FakeProvider("initial proposal")
    code = commit_flow(console=con, provider=provider)
    assert code == 0

    messages = _get_git_log_messages(git_repo)
    assert messages[0] == "feat(edit): edited line"


def test_interactive_edit_aborts_on_empty_commit_message(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "empty.py", b"val = 2\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    # Editor leaves only comments and whitespace
    editor_script = (
        "import sys, pathlib;"
        "target = pathlib.Path(sys.argv[1]);"
        "target.write_text('# only comments\\n# remain\\n\\n')"
    )
    monkeypatch.setenv("EDITOR", f'{sys.executable} -c "{editor_script}"')

    inputs = iter(["e"])
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    code = commit_flow(console=con, provider=FakeProvider("msg"))
    assert code == 1

    messages = _get_git_log_messages(git_repo)
    assert len(messages) == 1  # No commit created


def test_interactive_edit_nonzero_exit_preserves_message(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "preserve.py", b"val = 3\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    # Editor exits non-zero (simulating Vim :cq)
    editor_script = "import sys; sys.exit(2)"
    monkeypatch.setenv("EDITOR", f'{sys.executable} -c "{editor_script}"')

    inputs = iter(["e", "a"])
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    code = commit_flow(console=con, provider=FakeProvider("feat: preserved original"))
    assert code == 0

    messages = _get_git_log_messages(git_repo)
    assert messages[0] == "feat: preserved original"


def test_interactive_edit_missing_editor_binary(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "missing.py", b"val = 4\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    monkeypatch.setenv("EDITOR", "nonexistent_editor_command_binary_12345")

    inputs = iter(["e", "a"])
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    code = commit_flow(console=con, provider=FakeProvider("feat: original message"))
    assert code == 0
    messages = _get_git_log_messages(git_repo)
    assert messages[0] == "feat: original message"


# --- 9. Temporary File Cleanup Guarantee ---


@pytest.mark.parametrize(
    "action_inputs, expected_code",
    [
        (["a"], 0),
        (["c"], 0),
        (["e", "a"], 0),  # edit then accept
    ],
)
def test_temporary_files_are_deleted_in_all_paths(
    git_repo: Path,
    metrics_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_inputs: list[str],
    expected_code: int,
) -> None:
    stage_file(git_repo, "cleanup.py", b"cleanup = True\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    editor_script = (
        "import sys, pathlib;"
        "target = pathlib.Path(sys.argv[1]);"
        "target.write_text('feat: edited message\\n')"
    )
    monkeypatch.setenv("EDITOR", f'{sys.executable} -c "{editor_script}"')

    inputs = iter(action_inputs)
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    temp_dir = Path(tempfile.gettempdir())
    before_files = set(temp_dir.glob("gitmate_*"))

    code = commit_flow(console=con, provider=FakeProvider())
    assert code == expected_code

    after_files = set(temp_dir.glob("gitmate_*"))
    # No lingering gitmate temporary files created during this run
    leaked_files = after_files - before_files
    assert not leaked_files, f"Leaked temporary files: {leaked_files}"


# --- 10. Adversarial Edge Cases & Error Paths ---


def test_resolve_editor_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)

    # 1. Fallback when none are set
    res = _resolve_editor()
    assert res in ("nano", "vi")

    # 2. EDITOR set
    monkeypatch.setenv("EDITOR", "my-editor")
    assert _resolve_editor() == "my-editor"

    # 3. VISUAL overrides EDITOR
    monkeypatch.setenv("VISUAL", "my-visual")
    assert _resolve_editor() == "my-visual"

    # 4. GIT_EDITOR overrides both
    monkeypatch.setenv("GIT_EDITOR", "my-git-editor")
    assert _resolve_editor() == "my-git-editor"


def test_commit_flow_staged_git_command_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = DiffExtractor(repo=git_repo)
    monkeypatch.setattr(
        extractor,
        "staged",
        lambda: (_ for _ in ()).throw(GitCommandError("git diff failed")),
    )

    code = commit_flow(diff_extractor=extractor)
    assert code == 1


def test_multiple_regenerations_then_cancel_records_all_telemetry(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "multi.py", b"x = 1\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    inputs = iter(["r", "r", "c"])
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    provider = FakeProvider(["msg1", "msg2", "msg3"])
    code = commit_flow(console=con, provider=provider)
    assert code == 0
    assert provider.call_count == 3

    # All 3 generations logged
    invocations = get_invocations()
    assert len(invocations) == 3

    # No commit created
    messages = _get_git_log_messages(git_repo)
    assert len(messages) == 1


def test_interactive_edit_trailing_whitespace_stripped(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "trail.py", b"x = 1\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    editor_script = (
        "import sys, pathlib;"
        "target = pathlib.Path(sys.argv[1]);"
        "target.write_text('feat: line with trailing   \\n\\nbody with trailing   \\n')"
    )
    monkeypatch.setenv("EDITOR", f'{sys.executable} -c "{editor_script}"')

    inputs = iter(["e", "a"])
    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": next(inputs))

    code = commit_flow(console=con, provider=FakeProvider("initial"))
    assert code == 0

    messages = _get_git_log_messages(git_repo)
    assert messages[0] == "feat: line with trailing\n\nbody with trailing"


def test_commit_flow_git_commit_failure_returns_nonzero(
    git_repo: Path, metrics_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_file(git_repo, "fail.py", b"x = 1\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    con = Console()
    monkeypatch.setattr(con, "input", lambda prompt="": "a")

    orig_run = subprocess.run

    def _mock_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if cmd[0] == "git" and len(cmd) > 1 and cmd[1] == "commit":
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="pre-commit hook rejected commit",
            )
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _mock_run)

    code = commit_flow(console=con, provider=FakeProvider("msg"))
    assert code == 1
