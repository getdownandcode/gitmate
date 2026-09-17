"""Interactive commit review loop, guardrails, editor handling, and git execution."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from gitmate import config as config_mod
from gitmate.config import GitmateConfig
from gitmate.diff_extractor import DiffExtractor, GitCommandError
from gitmate.fallback import generate_commit_message
from gitmate.metrics import get_monthly_spend, record_invocation
from gitmate.providers.base import LLMProvider
from gitmate.token_budget import TokenCounter

logger = logging.getLogger("gitmate.committer")

GIT_COMMENT_BLOCK = (
    "\n\n# Please enter the commit message for your changes. Lines starting\n"
    "# with '#' will be ignored, and an empty message aborts the commit.\n"
)


def _safe_record_telemetry(
    command: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_hit: bool,
    latency_ms: int | None,
    fallback_used: bool,
    estimated_cost_usd: float | None = None,
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
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record telemetry: %s", exc)


def _resolve_editor() -> str:
    """Resolve the preferred editor command from environment or fallback."""
    for var in ("GIT_EDITOR", "VISUAL", "EDITOR"):
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()
    if shutil.which("nano"):
        return "nano"
    return "vi"


def _render_panel(console: Console, message: str, model: str, is_fallback: bool) -> None:
    """Display the proposed commit message in a styled Rich Panel."""
    status_badge = (
        "[yellow bold]⚠ FALLBACK TEMPLATE[/yellow bold]"
        if is_fallback
        else f"[green bold]{model}[/green bold]"
    )
    panel = Panel(
        message,
        title=f"Proposed Commit Message [dim]({status_badge})[/dim]",
        border_style="yellow" if is_fallback else "green",
        padding=(1, 2),
        expand=False,
    )
    console.print(panel)


def _execute_commit(message: str, console: Console | None = None, cwd: Path | None = None) -> int:
    """Commit using a temporary file with git commit -F, ensuring cleanup in finally."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="gitmate_commit_",
        delete=False,
    ) as tf:
        tf.write(message)
        tf.flush()
        temp_path = Path(tf.name)

    repo_dir = cwd if cwd is not None else Path.cwd()
    try:
        proc = subprocess.run(
            ["git", "commit", "-F", str(temp_path)],
            cwd=repo_dir,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            if console is not None and proc.stdout.strip():
                console.print(proc.stdout.strip())
        else:
            if console is not None:
                err_msg = proc.stderr.strip() or proc.stdout.strip()
                if err_msg:
                    console.print(f"[red]{err_msg}[/red]")
        return proc.returncode
    except FileNotFoundError:
        if console is not None:
            console.print("[red]error:[/red] git binary not found on PATH.")
        return 1
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def _launch_editor(
    initial_message: str,
    console: Console,
    cwd: Path | None = None,
) -> tuple[bool, str]:
    """Launch user editor pre-populated with message and git comment lines.

    Returns (success, result_message).
    - On non-zero exit code: returns (False, initial_message).
    - On exit code 0: strips '#' comments and whitespace; if empty, returns (True, "").
    - On exit code 0 with content: returns (True, cleaned_message).
    """
    content = initial_message.rstrip() + GIT_COMMENT_BLOCK
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="gitmate_edit_",
        suffix=".txt",
        delete=False,
    ) as tf:
        tf.write(content)
        tf.flush()
        temp_path = Path(tf.name)

    editor_cmd = _resolve_editor()
    cmd_parts = shlex.split(editor_cmd)
    repo_dir = cwd if cwd is not None else Path.cwd()

    try:
        proc = subprocess.run(
            [*cmd_parts, str(temp_path)],
            cwd=repo_dir,
            check=False,
        )
        if proc.returncode != 0:
            console.print(
                f"[yellow]Edit aborted by editor (exit code {proc.returncode}). "
                "Keeping existing proposed message.[/yellow]"
            )
            return False, initial_message

        raw_text = temp_path.read_text(encoding="utf-8")
        cleaned_lines = [
            line.rstrip() for line in raw_text.splitlines() if not line.strip().startswith("#")
        ]
        cleaned_message = "\n".join(cleaned_lines).strip()
        if not cleaned_message:
            console.print("[red]Aborting commit due to empty commit message.[/red]")
            return True, ""
        return True, cleaned_message
    except FileNotFoundError:
        console.print(f"[red]error:[/red] Editor command '{cmd_parts[0]}' not found on PATH.")
        return False, initial_message
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def commit_flow(
    yes: bool = False,
    cfg: GitmateConfig | None = None,
    diff_extractor: DiffExtractor | None = None,
    console: Console | None = None,
    provider: LLMProvider | None = None,
    secret_store: config_mod.SecretStore | None = None,
    counter: TokenCounter | None = None,
) -> int:
    """Execute the commit command review loop. Returns process exit code (0 or 1)."""
    con = console if console is not None else Console()
    active_cfg = cfg if cfg is not None else config_mod.load_config()

    extractor = (
        diff_extractor
        if diff_extractor is not None
        else DiffExtractor(extra_ignores=active_cfg.ignore_globs)
    )
    repo_dir = extractor._repo

    # 1. Staged diff check
    try:
        staged_diffs = extractor.staged()
    except GitCommandError as exc:
        con.print(f"[red]error:[/red] {exc}")
        return 1

    if not staged_diffs:
        con.print("No staged changes to commit. Use 'git add' to stage files first.")
        return 0

    # 2. TTY and non-interactive guardrail checks
    if not sys.stdin.isatty() and not yes:
        con.print(
            "[red]error:[/red] Interactive review requires a TTY terminal. "
            "Use --yes for non-interactive commit (requires 'allow_noninteractive_commit' in config)."
        )
        return 1

    if yes and not active_cfg.allow_noninteractive_commit:
        con.print(
            "[red]error:[/red] Non-interactive commit (--yes) is disabled by default.\n"
            "To enable, run: gitmate config set allow_noninteractive_commit true"
        )
        return 1

    # 3. Monthly budget cap enforcement
    if active_cfg.budget_cap_usd is not None:
        monthly_spend = get_monthly_spend()
        if monthly_spend >= active_cfg.budget_cap_usd:
            con.print(
                f"[red]error:[/red] Monthly budget cap exceeded "
                f"(${monthly_spend:.2f} >= ${active_cfg.budget_cap_usd:.2f}). "
                "Aborting to prevent further LLM spend.\n"
                "To adjust or remove the cap, run: gitmate config set budget_cap_usd <new_limit_or_none>"
            )
            return 1
        if monthly_spend >= 0.8 * active_cfg.budget_cap_usd:
            con.print(
                f"[yellow]warning: Monthly spend has reached {int(monthly_spend / active_cfg.budget_cap_usd * 100)}% "
                f"of budget cap (${monthly_spend:.2f} / ${active_cfg.budget_cap_usd:.2f}).[/yellow]"
            )

    active_counter = counter
    if active_counter is None and provider is not None:

        class _SimpleCounter(TokenCounter):
            def count_tokens(self, text: str, model: str) -> int:
                return max(1, len(text) // 4)

        active_counter = _SimpleCounter()

    # 4. Initial generation & telemetry recording
    t0 = time.perf_counter()
    gen_result = generate_commit_message(
        diffs=staged_diffs,
        cfg=active_cfg,
        provider=provider,
        counter=active_counter,
        console=con,
        secret_store=secret_store,
        bypass_cache=False,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    _safe_record_telemetry(
        command="commit",
        model=gen_result.model,
        tokens_in=gen_result.input_tokens,
        tokens_out=gen_result.output_tokens,
        cache_hit=gen_result.cache_hit,
        latency_ms=latency_ms,
        fallback_used=gen_result.is_fallback,
        estimated_cost_usd=gen_result.estimated_cost_usd,
    )

    current_message = gen_result.text
    current_model = gen_result.model
    current_is_fallback = gen_result.is_fallback

    # 5. Fast path: --yes with allow_noninteractive_commit enabled
    if yes:
        code = _execute_commit(current_message, console=con, cwd=repo_dir)
        return code

    # 6. Interactive review loop
    _render_panel(con, current_message, current_model, current_is_fallback)

    while True:
        try:
            raw_input = con.input(
                "[bold cyan]?[/bold cyan] [bold][a]ccept / [e]dit / [r]egenerate / [c]ancel:[/bold] "
            )
        except (EOFError, KeyboardInterrupt):
            con.print("\nCommit cancelled.")
            return 0

        choice = raw_input.strip().lower()
        if not choice:
            con.print(
                "[yellow]Please select an option: [a]ccept, [e]dit, [r]egenerate, or [c]ancel.[/yellow]"
            )
            continue

        if choice in ("a", "accept"):
            code = _execute_commit(current_message, console=con, cwd=repo_dir)
            return code

        elif choice in ("e", "edit"):
            success, edited_msg = _launch_editor(current_message, console=con, cwd=repo_dir)
            if success:
                if not edited_msg:
                    return 1
                current_message = edited_msg
                _render_panel(con, current_message, current_model, current_is_fallback)

        elif choice in ("r", "regenerate"):
            if active_cfg.budget_cap_usd is not None:
                monthly_spend = get_monthly_spend()
                if monthly_spend >= active_cfg.budget_cap_usd:
                    con.print(
                        f"[red]error:[/red] Monthly budget cap exceeded "
                        f"(${monthly_spend:.2f} >= ${active_cfg.budget_cap_usd:.2f}). "
                        "Aborting to prevent further LLM spend.\n"
                        "To adjust or remove the cap, run: gitmate config set budget_cap_usd <new_limit_or_none>"
                    )
                    continue
                if monthly_spend >= 0.8 * active_cfg.budget_cap_usd:
                    con.print(
                        f"[yellow]warning: Monthly spend has reached {int(monthly_spend / active_cfg.budget_cap_usd * 100)}% "
                        f"of budget cap (${monthly_spend:.2f} / ${active_cfg.budget_cap_usd:.2f}).[/yellow]"
                    )

            con.print("Regenerating commit message with fresh API call...")
            t0 = time.perf_counter()
            gen_result = generate_commit_message(
                diffs=staged_diffs,
                cfg=active_cfg,
                provider=provider,
                counter=active_counter,
                console=con,
                secret_store=secret_store,
                bypass_cache=True,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)

            _safe_record_telemetry(
                command="commit",
                model=gen_result.model,
                tokens_in=gen_result.input_tokens,
                tokens_out=gen_result.output_tokens,
                cache_hit=False,
                latency_ms=latency_ms,
                fallback_used=gen_result.is_fallback,
                estimated_cost_usd=gen_result.estimated_cost_usd,
            )

            current_message = gen_result.text
            current_model = gen_result.model
            current_is_fallback = gen_result.is_fallback
            _render_panel(con, current_message, current_model, current_is_fallback)

        elif choice in ("c", "cancel"):
            con.print("Commit cancelled.")
            return 0

        else:
            con.print(
                f"[yellow]Invalid selection '{raw_input}'. "
                "Please choose [a]ccept, [e]dit, [r]egenerate, or [c]ancel.[/yellow]"
            )
