"""Template-based graceful degradation and commit message orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from gitmate import config as config_mod
from gitmate.config import GitmateConfig
from gitmate.diff_extractor import FileDiff
from gitmate.orchestrator import GenerationResult, run_generation_pipeline
from gitmate.providers.base import LLMProvider
from gitmate.token_budget import TokenCounter

__all__ = [
    "GenerationResult",
    "generate_commit_message",
    "generate_fallback_changelog",
    "generate_fallback_message",
    "generate_fallback_pr_summary",
    "run_generation_pipeline",
]

if TYPE_CHECKING:
    from rich.console import Console

    from gitmate.changelog import CommitLogEntry

DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}
BUILD_FILENAMES = {
    "pyproject.toml",
    "uv.lock",
    "setup.py",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "dockerfile",
    "docker-compose.yml",
}


def _extract_scope(path_str: str) -> str | None:
    """Infer a concise scope from a file path, omitting redundant top-level directory names."""
    path = Path(path_str)
    parts = path.parts
    if len(parts) <= 1:
        return None

    # Skip generic container directories to isolate specific subsystem
    skip_dirs = {"src", "lib", "app", "gitmate", "tests", "docs", ".github", "workflows"}
    filtered = [p for p in parts[:-1] if p.lower() not in skip_dirs]
    if filtered:
        return filtered[-1].lower()
    return None


def _find_common_scope(diffs: list[FileDiff]) -> str | None:
    """Find a shared scope if all diffs belong to the same subsystem."""
    scopes = {_extract_scope(d.path) for d in diffs}
    if len(scopes) == 1:
        return next(iter(scopes))
    return None


def _infer_type(diff: FileDiff) -> str:
    """Infer conventional commit type for a single FileDiff."""
    path_lower = diff.path.lower()
    name = Path(diff.path).name.lower()
    suffix = Path(diff.path).suffix.lower()

    if (
        path_lower.startswith(".github/")
        or "workflows" in path_lower
        or name in (".gitlab-ci.yml", "azure-pipelines.yml")
    ):
        return "ci"
    if name in BUILD_FILENAMES or suffix in (".lock", ".toml"):
        return "build"
    if path_lower.startswith("docs/") or suffix in DOC_EXTENSIONS:
        return "docs"
    if (
        path_lower.startswith("tests/")
        or "test_" in name
        or "_test." in name
        or name.endswith("_test.py")
    ):
        return "test"
    if diff.status == "added":
        return "feat"
    if diff.status == "deleted":
        return "chore"
    if diff.status == "renamed":
        return "refactor"
    return "chore"


def _infer_multi_type(diffs: list[FileDiff]) -> str:
    """Infer primary conventional type across multiple diffs."""
    types = [_infer_type(d) for d in diffs]
    # If all share the same type (e.g. all docs, all tests, all ci)
    if len(set(types)) == 1:
        return types[0]

    statuses = {d.status for d in diffs}
    if statuses == {"added"}:
        return "feat"
    if statuses == {"deleted"}:
        return "chore"
    if statuses == {"renamed"}:
        return "refactor"
    if any(t == "feat" for t in types):
        return "feat"
    return "chore"


def generate_fallback_message(diffs: list[FileDiff], style: str = "conventional") -> str:
    """Derive a clean, deterministic commit message from diff metadata without an LLM."""
    if not diffs:
        return "chore: empty commit" if style == "conventional" else "Empty commit"

    if len(diffs) == 1:
        return _single_file_fallback(diffs[0], style)

    return _multi_file_fallback(diffs, style)


def _single_file_fallback(diff: FileDiff, style: str) -> str:
    """Generate fallback message for a single file diff."""
    path = diff.path
    name = Path(path).name
    scope = _extract_scope(path)
    commit_type = _infer_type(diff)

    if style == "conventional":
        scope_prefix = f"({scope})" if scope and scope != commit_type else ""
        if diff.status == "added":
            return f"{commit_type}{scope_prefix}: add {name}"
        if diff.status == "deleted":
            return f"{commit_type}{scope_prefix}: remove {name}"
        if diff.status == "renamed":
            old_name = Path(diff.old_path).name if diff.old_path else "file"
            return f"refactor{scope_prefix}: rename {old_name} to {name}"
        return f"{commit_type}{scope_prefix}: update {name}"

    # Plain style
    if diff.status == "added":
        return f"Add {path}"
    if diff.status == "deleted":
        return f"Remove {path}"
    if diff.status == "renamed":
        return f"Rename {diff.old_path or 'file'} to {path}"
    return f"Update {path}"


def _multi_file_fallback(diffs: list[FileDiff], style: str) -> str:
    """Generate fallback summary and file list for multiple diffs."""
    added = sum(1 for d in diffs if d.status == "added")
    modified = sum(1 for d in diffs if d.status == "modified")
    deleted = sum(1 for d in diffs if d.status == "deleted")
    renamed = sum(1 for d in diffs if d.status == "renamed")

    parts: list[str] = []
    if added:
        parts.append(f"added: {added}")
    if modified:
        parts.append(f"modified: {modified}")
    if deleted:
        parts.append(f"deleted: {deleted}")
    if renamed:
        parts.append(f"renamed: {renamed}")
    breakdown = f" ({', '.join(parts)})" if parts else ""

    scope = _find_common_scope(diffs)
    scope_str = f" in {scope}" if scope else ""
    commit_type = _infer_multi_type(diffs)

    if style == "conventional":
        scope_prefix = f"({scope})" if scope and scope != commit_type else ""
        header = f"{commit_type}{scope_prefix}: update {len(diffs)} files{breakdown}"
    else:
        header = f"Update {len(diffs)} files{scope_str}{breakdown}"

    # Build detailed body bullets
    bullets: list[str] = []
    for d in diffs:
        if d.is_binary:
            bullets.append(f"- Binary file: {d.path}")
        elif d.status == "added":
            bullets.append(f"- Add {d.path} (+{d.additions})")
        elif d.status == "deleted":
            bullets.append(f"- Remove {d.path} (-{d.deletions})")
        elif d.status == "renamed":
            bullets.append(f"- Rename {d.old_path or 'file'} -> {d.path}")
        else:
            bullets.append(f"- Modify {d.path} (+{d.additions}, -{d.deletions})")

    body = "\n".join(bullets)
    return f"{header}\n\n{body}"


def generate_fallback_pr_summary(diffs: list[FileDiff], base: str = "main") -> str:
    """Deterministic offline fallback PR summary when LLM provider is unavailable."""
    if not diffs:
        return f"## Summary\n\nNo changes between `{base}` and `HEAD`.\n"

    total_added = sum(d.additions for d in diffs)
    total_deleted = sum(d.deletions for d in diffs)
    summary = (
        f"## Summary\n\n"
        f"Branch comparison `{base}...HEAD` touching {len(diffs)} file(s) "
        f"(+{total_added}, -{total_deleted}).\n\n"
        f"## Changes\n"
    )
    bullets: list[str] = []
    for d in diffs:
        if d.is_binary:
            bullets.append(f"- Binary file: `{d.path}`")
        elif d.status == "added":
            bullets.append(f"- Add `{d.path}` (+{d.additions})")
        elif d.status == "deleted":
            bullets.append(f"- Remove `{d.path}` (-{d.deletions})")
        elif d.status == "renamed":
            bullets.append(f"- Rename `{d.old_path or 'file'}` -> `{d.path}`")
        else:
            bullets.append(f"- Modify `{d.path}` (+{d.additions}, -{d.deletions})")
    summary += "\n".join(bullets) + "\n\n"
    summary += "## Verification & Testing\n\n- Deterministic template fallback generated offline.\n"
    return summary


def generate_fallback_changelog(commits: list[CommitLogEntry], from_ref: str, to_ref: str) -> str:
    """Deterministic offline fallback release notes when LLM provider is unavailable."""
    if not commits:
        return f"# Release Notes ({from_ref}..{to_ref})\n\nNo commits in revision range.\n"

    from gitmate.changelog import group_commits

    grouped = group_commits(commits)
    sections: list[str] = [f"# Release Notes ({from_ref}..{to_ref})", ""]
    for category, category_commits in grouped.items():
        if not category_commits:
            continue
        sections.append(f"## {category}")
        for c in category_commits:
            scope_prefix = f"**{c.scope}**: " if c.scope else ""
            breaking_prefix = "⚠ **BREAKING**: " if c.is_breaking else ""
            desc = c.description if c.description else c.subject
            sections.append(f"- {breaking_prefix}{scope_prefix}{desc} ({c.commit_hash[:7]})")
        sections.append("")
    return "\n".join(sections).strip() + "\n"


def generate_commit_message(
    diffs: list[FileDiff],
    cfg: GitmateConfig,
    provider: LLMProvider | None = None,
    counter: TokenCounter | None = None,
    console: Console | None = None,
    secret_store: config_mod.SecretStore | None = None,
    bypass_cache: bool = False,
) -> GenerationResult:
    """Orchestrate commit message generation with graceful fallback on provider failure."""
    return run_generation_pipeline(
        diffs=diffs,
        cfg=cfg,
        template_name=cfg.commit_style,
        fallback_generator=lambda: generate_fallback_message(diffs, style=cfg.commit_style),
        empty_diff_message="No changes staged for commit.",
        provider=provider,
        counter=counter,
        console=console,
        secret_store=secret_store,
        bypass_cache=bypass_cache,
    )
