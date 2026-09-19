"""Pull request summary generation, clipboard integration, and GitHub CLI dispatch."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from gitmate import config as config_mod
from gitmate.config import GitmateConfig, SecretStore
from gitmate.diff_extractor import DiffExtractor
from gitmate.fallback import generate_fallback_pr_summary
from gitmate.orchestrator import GenerationResult, check_budget_cap, run_generation_pipeline
from gitmate.providers.base import LLMProvider
from gitmate.token_budget import TokenCounter

logger = logging.getLogger("gitmate.pr_summary")


class GitHubCliError(Exception):
    """Raised when gh pr create fails or is not found."""


def copy_to_clipboard(text: str, console: Console | None = None) -> bool:
    """Copy text to system clipboard, gracefully handling headless or unsupported systems.

    Returns True if successfully copied, False if clipboard is unavailable.
    """
    try:
        import pyperclip  # type: ignore[import-untyped]

        pyperclip.copy(text)
        return True
    except Exception as exc:  # noqa: BLE001
        if console is not None:
            console.print(
                f"[dim]Note: Clipboard unavailable ({exc}). PR summary displayed in terminal.[/dim]"
            )
        return False


def create_github_pr(
    base: str,
    summary: str,
    title: str | None = None,
    cwd: Path | None = None,
    console: Console | None = None,
) -> int:
    """Dispatch to gh pr create CLI if installed, passing generated summary as body."""
    gh_path = shutil.which("gh")
    if not gh_path:
        if console is not None:
            console.print(
                "[yellow]warning: GitHub CLI ('gh') not found on PATH. "
                "Install gh to create pull requests automatically.[/yellow]"
            )
        return 1

    repo_dir = cwd if cwd is not None else Path.cwd()
    cmd = ["gh", "pr", "create", "--base", base, "--body", summary]
    if title:
        cmd.extend(["--title", title])

    try:
        proc = subprocess.run(cmd, cwd=repo_dir, check=False)
        return proc.returncode
    except Exception as exc:  # noqa: BLE001
        if console is not None:
            console.print(f"[red]error running gh pr create:[/red] {exc}")
        return 1


def generate_pr_summary(
    base: str = "main",
    title: str | None = None,
    cfg: GitmateConfig | None = None,
    diff_extractor: DiffExtractor | None = None,
    provider: LLMProvider | None = None,
    counter: TokenCounter | None = None,
    console: Console | None = None,
    secret_store: SecretStore | None = None,
    bypass_cache: bool = False,
    copy_to_cb: bool = True,
    create_pr: bool = False,
    repo_dir: Path | None = None,
) -> GenerationResult:
    """Extract branch comparison diffs and generate a structured PR description."""
    con = console if console is not None else Console()
    active_cfg = cfg if cfg is not None else config_mod.load_config()

    check_budget_cap(active_cfg, console=con)

    extractor = diff_extractor if diff_extractor is not None else DiffExtractor(repo=repo_dir)
    diffs = extractor.branch_comparison(base=base)

    if not diffs:
        return GenerationResult(
            text=f"No changes between '{base}' and HEAD.",
            is_fallback=False,
            model=active_cfg.model,
        )

    gen_result = run_generation_pipeline(
        diffs=diffs,
        cfg=active_cfg,
        template_name="pr_summary",
        fallback_generator=lambda: generate_fallback_pr_summary(diffs, base=base),
        template_vars={"base_branch": base},
        command="pr_summary",
        provider=provider,
        counter=counter,
        console=con,
        secret_store=secret_store,
        bypass_cache=bypass_cache,
    )

    if copy_to_cb:
        copied = copy_to_clipboard(gen_result.text, console=con)
        if copied and con is not None:
            con.print("[dim]✔ Copied PR summary to clipboard.[/dim]")

    if create_pr:
        code = create_github_pr(
            base=base,
            summary=gen_result.text,
            title=title,
            cwd=repo_dir,
            console=con,
        )
        if code != 0:
            raise GitHubCliError(f"GitHub CLI ('gh pr create') failed with exit code {code}.")

    return gen_result
