"""Extract structured diffs from git via subprocess; no GitPython, no raw strings."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

FileStatus = Literal["added", "modified", "deleted", "renamed"]

#: Built-in noise filter; users extend (never shrink) it via `ignore_globs`.
DEFAULT_IGNORE_GLOBS: tuple[str, ...] = (
    "package-lock.json",
    "poetry.lock",
    "Cargo.lock",
    "*.min.js",
    "*.bundle.js",
    "*.map",
    "dist/*",
    "*.snap",
    "*.lock",
)


class GitCommandError(Exception):
    """Raised when a git invocation fails for any reason."""


class NotAGitRepo(GitCommandError):
    """Raised when the target directory is not inside a git repository."""


@dataclass
class FileDiff:
    """One file's diff; every downstream layer works off this, not raw text."""

    path: str
    old_path: str | None
    status: FileStatus
    additions: int
    deletions: int
    patch_text: str
    is_binary: bool


class GitRunner(Protocol):
    """Runs git so tests can substitute a fake; production shells out for real."""

    def run(self, args: list[str], cwd: Path) -> str:
        """Run `git <args>` in cwd, returning stdout or raising GitCommandError."""
        ...


class SubprocessGitRunner:
    """GitRunner that calls the real git binary with a timeout."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def run(self, args: list[str], cwd: Path) -> str:
        """Run the real git binary; map failures to typed exceptions."""
        if not cwd.exists():
            raise NotAGitRepo(f"{cwd} does not exist.")
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitCommandError("git binary not found on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(f"git {' '.join(args)} timed out.") from exc
        if proc.returncode != 0:
            err = proc.stderr.strip()
            if "not a git repository" in err:
                raise NotAGitRepo(f"{cwd} is not inside a git repository.")
            raise GitCommandError(f"git {' '.join(args)} failed: {err}")
        return proc.stdout


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Gitignore-flavored match: dir prefixes, bare basenames, else fnmatch."""
    posix = PurePosixPath(path).as_posix()
    name = PurePosixPath(path).name
    for pat in patterns:
        if pat.endswith("/*"):
            prefix = pat[:-1]
            if posix == prefix[:-1] or posix.startswith(prefix):
                return True
        elif pat.endswith("/"):
            if posix == pat[:-1] or posix.startswith(pat):
                return True
        elif "/" not in pat:
            if fnmatchcase(name, pat):
                return True
        elif fnmatchcase(posix, pat):
            return True
    return False


_STATUS_MAP: dict[str, FileStatus] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "T": "modified",
    "C": "added",
    "U": "modified",
}


def _parse_numstat(output: str) -> tuple[dict[str, tuple[int, int, str | None]], set[str]]:
    """Parse `git diff -M -z --numstat`, keyed by new path with binary set.

    With -z, records are NUL-separated and a rename carries no inline path at
    all: the `added\\tdeleted\\t` prefix is followed by an empty field, then
    the old and new paths as two further NUL-terminated fields. This avoids
    the `{old => new}` pretty-printing entirely, so no arrow-parsing is needed.
    """
    counts: dict[str, tuple[int, int, str | None]] = {}
    binary: set[str] = set()
    fields = output.split("\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        parts = field.split("\t")
        if len(parts) < 3:
            raise GitCommandError(f"cannot parse numstat record: {field!r}.")
        added_s, deleted_s, rest = parts[0], parts[1], "\t".join(parts[2:])
        old_path: str | None
        if rest == "":
            # Rename record: old and new paths follow as two extra fields.
            if i + 1 >= len(fields) or not fields[i] or not fields[i + 1]:
                raise GitCommandError(f"cannot parse numstat rename: {field!r}.")
            old, key = fields[i], fields[i + 1]
            old_path = old
            i += 2
        else:
            key, old_path = rest, None
        if added_s == "-" or deleted_s == "-":
            counts[key] = (0, 0, old_path)
            binary.add(key)
            continue
        try:
            added, deleted = int(added_s), int(deleted_s)
        except ValueError as exc:
            raise GitCommandError(f"cannot parse numstat counts: {field!r}.") from exc
        counts[key] = (added, deleted, old_path)
    return counts, binary


def _parse_name_status(output: str) -> dict[str, tuple[FileStatus, str | None]]:
    """Parse `git diff -M --name-status`; clean `R100\\told\\tnew` rename rows."""
    statuses: dict[str, tuple[FileStatus, str | None]] = {}
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        code = parts[0][0]
        if code not in _STATUS_MAP:
            raise GitCommandError(f"unknown git status code: {line!r}.")
        status = _STATUS_MAP[code]
        if code in ("R", "C"):
            if len(parts) != 3:
                raise GitCommandError(f"cannot parse rename/copy row: {line!r}.")
            _, old, new = parts
            statuses[new] = (status, old)
        else:
            if len(parts) != 2:
                raise GitCommandError(f"cannot parse status row: {line!r}.")
            statuses[parts[1]] = (status, None)
    return statuses


def _strip_git_prefix(path: str) -> str:
    """Strip git's a//b/ prefixes and optional C-style quoting."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _parse_patch(output: str) -> dict[str, str]:
    """Split `--patch` output per file, keyed by new path (old on deletion)."""
    chunks: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    minus: str | None = None
    chunk: list[str] = []

    def flush() -> None:
        nonlocal current, minus
        if current is not None or minus is not None:
            for cl in chunk:
                if cl.startswith(("rename to ", "copy to ")):
                    current = _strip_git_prefix(cl.split(" ", 2)[2].rstrip("\n"))
                elif cl.startswith(("rename from ", "copy from ")):
                    minus = _strip_git_prefix(cl.split(" ", 2)[2].rstrip("\n"))
            key = current if current is not None and current != "/dev/null" else minus
            if key is not None:
                if key not in chunks:
                    chunks[key] = []
                    order.append(key)
                chunks[key].extend(chunk)

    for line in output.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            rest = line[len("diff --git ") :].rstrip("\n")
            if len(rest) >= 5 and rest.startswith("a/"):
                mid = (len(rest) - 1) // 2
                if rest[mid : mid + 3] == " b/" and rest[2:mid] == rest[mid + 3 :]:
                    minus = _strip_git_prefix(rest[2:mid])
                    current = _strip_git_prefix(rest[mid + 3 :])
                else:
                    halves = rest.rsplit(" ", 1)
                    minus = _strip_git_prefix(halves[0].strip()) if len(halves) == 2 else None
                    current = _strip_git_prefix(halves[1].strip()) if len(halves) == 2 else None
            else:
                halves = rest.rsplit(" ", 1)
                minus = _strip_git_prefix(halves[0].strip()) if len(halves) == 2 else None
                current = _strip_git_prefix(halves[1].strip()) if len(halves) == 2 else None
            chunk = [line]
        elif current is None and minus is None:
            continue
        else:
            chunk.append(line)
    flush()
    # Re-key via the ---/+++ lines: +++ names the new file (/dev/null on delete).
    fixed: dict[str, str] = {}
    for key in order:
        text = "".join(chunks[key])
        for text_line in text.splitlines():
            if text_line.startswith("+++ "):
                plus = _strip_git_prefix(text_line[4:].strip())
                if plus == "/dev/null":
                    for prev in text.splitlines():
                        if prev.startswith("--- "):
                            key = _strip_git_prefix(prev[4:].strip())
                else:
                    key = plus
                break
        fixed[key] = text
    return fixed


class DiffExtractor:
    """Three git-diff modes sharing one parse/merge/filter pipeline."""

    def __init__(
        self,
        repo: Path | None = None,
        runner: GitRunner | None = None,
        extra_ignores: Iterable[str] = (),
    ) -> None:
        self._repo = repo if repo is not None else Path.cwd()
        self._runner = runner if runner is not None else SubprocessGitRunner()
        self._ignores = (*DEFAULT_IGNORE_GLOBS, *extra_ignores)

    def staged(self) -> list[FileDiff]:
        """Diff of staged changes (the `git commit` path)."""
        return self._extract(["diff", "--staged"])

    def branch_comparison(self, base: str) -> list[FileDiff]:
        """Three-dot `base...HEAD` diff (the PR-summary path)."""
        return self._extract(["diff", f"{base}...HEAD"])

    def rev_range(self, from_ref: str, to_ref: str) -> list[FileDiff]:
        """Two-dot `from..to` diff (the changelog path)."""
        return self._extract(["diff", f"{from_ref}..{to_ref}"])

    def _extract(self, base_args: list[str]) -> list[FileDiff]:
        """Run the three git calls and merge them keyed by path, never by order."""
        self._runner.run(["rev-parse", "--git-dir"], self._repo)
        git_flags = [
            "-c",
            "core.quotepath=false",
            "-c",
            "diff.mnemonicPrefix=false",
            *base_args,
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
        ]
        numstat = self._runner.run([*git_flags, "-M", "-z", "--numstat"], self._repo)
        patch = self._runner.run([*git_flags, "-M", "--patch"], self._repo)
        names = self._runner.run([*git_flags, "-M", "--name-status"], self._repo)
        counts, binary = _parse_numstat(numstat)
        statuses = _parse_name_status(names)
        patches = _parse_patch(patch)
        diffs: list[FileDiff] = []
        for path in list(counts) + [p for p in statuses if p not in counts]:
            if matches_any(path, self._ignores):
                continue
            added, deleted, num_old = counts.get(path, (0, 0, None))
            status, name_old = statuses.get(path, ("modified", None))
            old_path = name_old if name_old is not None else num_old
            is_binary = path in binary
            diffs.append(
                FileDiff(
                    path=path,
                    old_path=old_path,
                    status=status,
                    additions=added,
                    deletions=deleted,
                    patch_text="" if is_binary else patches.get(path, ""),
                    is_binary=is_binary,
                )
            )
        return diffs
