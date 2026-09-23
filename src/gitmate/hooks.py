"""Crash-safe prepare-commit-msg hook installation and execution."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOK_TIMEOUT_SECONDS = 4
HOOK_MARKER = "# Installed by gitmate; managed by `gitmate install-hook`."

HOOK_SCRIPT = f"""#!/bin/sh
{HOOK_MARKER}
# Crash-safe launcher for gitmate's prepare-commit-msg worker.
PYTHON="{sys.executable}"
if [ ! -x "$PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON="python3"
    else
        exit 0
    fi
fi
exec "$PYTHON" -c '
import subprocess, sys
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
sys.exit(0)
' "$@"
"""


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
        if HOOK_MARKER not in content:
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
    from gitmate.fallback import generate_commit_message, generate_fallback_message
    from gitmate.orchestrator import BudgetCapExceededError, safe_record_telemetry

    cfg = config_mod.load_config()
    diffs = DiffExtractor(extra_ignores=cfg.ignore_globs).staged()
    if not diffs:
        return
    started = time.perf_counter()
    try:
        result = generate_commit_message(
            diffs=diffs,
            cfg=cfg,
            console=None,
            command="commit-hook",
            hook_mode=True,
        )
        generated = result.text.rstrip()
    except BudgetCapExceededError:
        generated = generate_fallback_message(diffs, style=cfg.commit_style)
        safe_record_telemetry(
            command="commit-hook",
            model=cfg.model,
            tokens_in=None,
            tokens_out=None,
            cache_hit=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fallback_used=True,
            estimated_cost_usd=0.0,
            free_tier=cfg.free_tier,
        )
    existing = message_file.read_text(encoding="utf-8", errors="replace")
    if existing.strip():
        new_message = f"{generated}\n\n{existing.lstrip()}"
    else:
        new_message = f"{generated}\n"
    _replace_message(message_file, new_message)


def _replace_message(message_file: Path, message: str) -> None:
    """Atomically replace Git's message file so write failures preserve its contents."""
    cutoff = time.time() - HOOK_TIMEOUT_SECONDS
    for stale in message_file.parent.glob(f".{message_file.name}.*"):
        try:
            if stale.is_file() and stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue

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
