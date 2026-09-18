"""Pull request summary generation, clipboard integration, and GitHub CLI dispatch."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from rich.console import Console

from gitmate import config as config_mod
from gitmate.config import GitmateConfig, SecretStore
from gitmate.diff_extractor import DiffExtractor, GitCommandError
from gitmate.fallback import generate_fallback_pr_summary
from gitmate.metrics import get_monthly_spend, record_invocation
from gitmate.orchestrator import GenerationResult, run_generation_pipeline
from gitmate.providers.base import LLMProvider
from gitmate.token_budget import TokenCounter

logger = logging.getLogger("gitmate.pr_summary")


def _safe_record_telemetry(
    command: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_hit: bool,
    latency_ms: int | None,
    fallback_used: bool,
    estimated_cost_usd: float | None = None,
    free_tier: bool = False,
) -> None:
    """Record invocation to metrics database without propagating errors."""
    try:
        record_invocation(
            command=command,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_hit=cache_hit,
            latency_ms=latency_ms,
            fallback_used=fallback_used,
            estimated_cost_usd=estimated_cost_usd,
            free_tier=free_tier,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record telemetry: %s", exc)


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
    head: str = "HEAD",
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

    # Budget cap check
    if active_cfg.budget_cap_usd is not None:
        monthly_spend = get_monthly_spend()
        if monthly_spend >= active_cfg.budget_cap_usd:
            con.print(
                f"[red]error:[/red] Monthly budget cap exceeded "
                f"(${monthly_spend:.2f} >= ${active_cfg.budget_cap_usd:.2f}). "
                "Aborting to prevent further LLM spend."
            )
            return GenerationResult(
                text="Monthly budget cap exceeded. Generation aborted.",
                is_fallback=True,
                model=active_cfg.model,
                fallback_reason="Monthly budget cap exceeded",
            )
        if monthly_spend >= 0.8 * active_cfg.budget_cap_usd:
            con.print(
                f"[yellow]warning: Monthly spend has reached {int(monthly_spend / active_cfg.budget_cap_usd * 100)}% "
                f"of budget cap (${monthly_spend:.2f} / ${active_cfg.budget_cap_usd:.2f}).[/yellow]"
            )

    extractor = diff_extractor if diff_extractor is not None else DiffExtractor(repo=repo_dir)

    try:
        diffs = extractor.branch_comparison(base=base)
    except GitCommandError as exc:
        con.print(f"[red]git error:[/red] {exc}")
        return GenerationResult(
            text=f"Git error: {exc}",
            is_fallback=True,
            model=active_cfg.model,
            fallback_reason=str(exc),
        )

    if not diffs:
        empty_msg = f"No changes between '{base}' and {head}."
        con.print(f"[dim]{empty_msg}[/dim]")
        return GenerationResult(
            text=empty_msg,
            is_fallback=False,
            model=active_cfg.model,
        )

    active_counter = counter
    if active_counter is None and provider is not None:

        class _SimpleCounter(TokenCounter):
            def count_tokens(self, text: str, model: str) -> int:
                return max(1, len(text) // 4)

        active_counter = _SimpleCounter()

    t0 = time.perf_counter()
    gen_result = run_generation_pipeline(
        diffs=diffs,
        cfg=active_cfg,
        template_name="pr_summary",
        fallback_generator=lambda: generate_fallback_pr_summary(diffs, base=base, head=head),
        template_vars={"base_branch": base, "head_branch": head},
        empty_diff_message=f"No changes between '{base}' and {head}.",
        provider=provider,
        counter=active_counter,
        console=con,
        secret_store=secret_store,
        bypass_cache=bypass_cache,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    _safe_record_telemetry(
        command="pr_summary",
        model=gen_result.model,
        tokens_in=gen_result.input_tokens,
        tokens_out=gen_result.output_tokens,
        cache_hit=gen_result.cache_hit,
        latency_ms=latency_ms,
        fallback_used=gen_result.is_fallback,
        estimated_cost_usd=gen_result.estimated_cost_usd,
        free_tier=active_cfg.free_tier,
    )

    if copy_to_cb:
        copied = copy_to_clipboard(gen_result.text, console=con)
        if copied:
            con.print("[dim]✔ Copied PR summary to clipboard.[/dim]")

    if create_pr:
        create_github_pr(base=base, summary=gen_result.text, cwd=repo_dir, console=con)

    return gen_result
