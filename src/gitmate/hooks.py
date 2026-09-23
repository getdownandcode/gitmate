"""Crash-safe prepare-commit-msg hook installation and execution."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_TIMEOUT_SECONDS = 4
HOOK_MARKER = "# Installed by gitmate; managed by `gitmate install-hook`."

HOOK_SCRIPT = f'''#!{sys.executable}
# Installed by gitmate; managed by `gitmate install-hook`.
"""Crash-safe launcher for gitmate's prepare-commit-msg worker."""
import subprocess
import sys

try:
    subprocess.run(
        [sys.executable, "-m", "gitmate.hooks", "--worker", *sys.argv[1:]],
        timeout={HOOK_TIMEOUT_SECONDS},
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
except BaseException:
    pass
raise SystemExit(0)
'''


class HookError(Exception):
    """Raised when a hook cannot be safely installed or removed."""


def _hook_path(cwd: Path | None = None) -> Path:
    """Resolve the repository's active prepare-commit-msg hook path."""
    repo = cwd if cwd is not None else Path.cwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks/prepare-commit-msg"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HookError(f"cannot locate the prepare-commit-msg hook: {exc}") from exc
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else (repo / path).resolve()


def install_hook(cwd: Path | None = None) -> Path:
    """Install the crash-safe hook, refusing to replace user-owned hooks."""
    path = _hook_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = path.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARKER in content:
            path.chmod(path.stat().st_mode | 0o111)
            return path
        raise HookError(f"{path} already exists; move or remove it before installing gitmate.")
    path.write_text(HOOK_SCRIPT, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def uninstall_hook(cwd: Path | None = None) -> Path:
    """Remove the gitmate hook while preserving hooks owned by other tools."""
    path = _hook_path(cwd)
    if not path.exists():
        return path
    content = path.read_text(encoding="utf-8", errors="replace")
    if HOOK_MARKER not in content:
        raise HookError(f"{path} is not a gitmate-managed hook; it was left untouched.")
    path.unlink()
    return path


def main() -> int:
    """Run the hook worker with a hard timeout and unconditional success status."""
    args = sys.argv[1:]
    if args and args[0] == "--worker":
        try:
            _run_worker(args[1:])
        except BaseException:  # noqa: BLE001, S110
            pass
        return 0

    if args:
        source = os.environ.get("PRE_COMMIT_COMMIT_MSG_SOURCE")
        commit_hash = os.environ.get("PRE_COMMIT_COMMIT_OBJECT_NAME")
        if len(args) == 1 and source:
            args.append(source)
        if len(args) == 2 and commit_hash:
            args.append(commit_hash)

    try:
        subprocess.run(
            [sys.executable, "-m", "gitmate.hooks", "--worker", *args],
            timeout=HOOK_TIMEOUT_SECONDS,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except BaseException:  # noqa: BLE001, S110
        pass
    return 0


def _run_worker(args: list[str]) -> None:
    """Generate a message for supported Git hook sources without committing."""
    if not args or len(args) > 3:
        return
    message_file = Path(args[0])
    source = args[1] if len(args) > 1 else ""
    if source not in ("", "template") or not message_file.is_file():
        return

    from gitmate import config as config_mod
    from gitmate.diff_extractor import DiffExtractor
    from gitmate.fallback import generate_commit_message

    cfg = config_mod.load_config()
    diffs = DiffExtractor(extra_ignores=cfg.ignore_globs).staged()
    if not diffs:
        return
    result = generate_commit_message(
        diffs=diffs,
        cfg=cfg,
        console=None,
        command="commit-hook",
    )
    _replace_message(message_file, result.text.rstrip() + "\n")


def _replace_message(message_file: Path, message: str) -> None:
    """Atomically replace Git's message file so write failures preserve its contents."""
    mode = stat.S_IMODE(message_file.stat().st_mode)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=message_file.parent,
            prefix=f".{message_file.name}.",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(message)
        temp_path.chmod(mode)
        os.replace(temp_path, message_file)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
