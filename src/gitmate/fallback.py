"""Template-based graceful degradation and commit message orchestration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gitmate import config as config_mod
from gitmate.config import API_KEY_ACCOUNT, GitmateConfig
from gitmate.diff_extractor import FileDiff
from gitmate.prompt import render_commit_prompt
from gitmate.providers.base import LLMProvider, ProviderUnavailable
from gitmate.providers.cache import CachedProvider
from gitmate.providers.gemini import GeminiProvider
from gitmate.token_budget import (
    GeminiTokenCounter,
    TokenBudgetError,
    TokenBudgetManager,
    TokenCounter,
)

if TYPE_CHECKING:
    from rich.console import Console

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


@dataclass(frozen=True)
class GenerationResult:
    """Outcome of commit message generation, tracking fallback and usage."""

    text: str
    is_fallback: bool
    model: str
    fallback_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


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


def generate_commit_message(
    diffs: list[FileDiff],
    cfg: GitmateConfig,
    provider: LLMProvider | None = None,
    counter: TokenCounter | None = None,
    console: Console | None = None,
    secret_store: config_mod.SecretStore | None = None,
) -> GenerationResult:
    """Orchestrate commit message generation with graceful fallback on provider failure."""
    if not diffs:
        return GenerationResult(
            text="No changes staged for commit.",
            is_fallback=False,
            model=cfg.model,
        )

    store = secret_store if secret_store is not None else config_mod.KeyringSecretStore()
    api_key = store.get_secret(API_KEY_ACCOUNT) or os.environ.get("GEMINI_API_KEY")

    # 1. Budget evaluation with fallback on counter/budget failure
    active_counter = counter if counter is not None else GeminiTokenCounter(api_key=api_key)
    try:
        budget_mgr = TokenBudgetManager.from_config(cfg, active_counter)
        decision = budget_mgr.assess(diffs)
        patch_chunks = [d.patch_text for d in decision.included_diffs if d.patch_text]
        diff_text = "\n".join(patch_chunks)
        summary_note = decision.summary_note
    except TokenBudgetError as exc:
        if console is not None:
            console.print("[yellow]⚠ API unavailable, using template fallback[/yellow]")
        fallback_msg = generate_fallback_message(diffs, style=cfg.commit_style)
        return GenerationResult(
            text=fallback_msg,
            is_fallback=True,
            model=cfg.model,
            fallback_reason=f"Token budgeting failed: {exc}",
        )

    # 2. Render prompt template
    prompt_text, version_key = render_commit_prompt(
        style=cfg.commit_style,
        diff_text=diff_text,
        summary_note=summary_note,
    )

    # 3. Resolve provider
    active_provider: LLMProvider
    if provider is not None:
        active_provider = provider
    else:
        base_provider = GeminiProvider(api_key=api_key)
        active_provider = CachedProvider(
            provider=base_provider,
            cache_dir=cfg.cache_dir,
            template_version=version_key,
        )

    # 4. Generate with graceful degradation
    try:
        resp = active_provider.generate(prompt=prompt_text, model=cfg.model)
        return GenerationResult(
            text=resp.text,
            is_fallback=False,
            model=resp.model,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
        )
    except ProviderUnavailable as exc:
        if console is not None:
            console.print("[yellow]⚠ API unavailable, using template fallback[/yellow]")

        fallback_msg = generate_fallback_message(diffs, style=cfg.commit_style)
        return GenerationResult(
            text=fallback_msg,
            is_fallback=True,
            model=cfg.model,
            fallback_reason=str(exc),
        )
