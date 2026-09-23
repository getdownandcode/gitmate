"""Typer app, command definitions only — no business logic here."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from gitmate import config as config_mod
from gitmate.config import API_KEY_ACCOUNT
from gitmate.diff_extractor import DiffExtractor, GitCommandError

app = typer.Typer(
    help="AI-assisted git commit messages, PR summaries, and changelogs.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="View and change gitmate settings.")
app.add_typer(config_app, name="config")

console = Console()
err_console = Console(stderr=True)
secret_store: config_mod.SecretStore = config_mod.KeyringSecretStore()

#: The installed distribution name; the CLI command stays `gitmate`.
DIST_NAME = "gitmate-cli"


def _version_callback(value: bool) -> None:
    """Print the installed distribution version for --version."""
    if value:
        from importlib.metadata import PackageNotFoundError, version

        try:
            console.print(version(DIST_NAME))
        except PackageNotFoundError:
            console.print("unknown (gitmate-cli is not installed as a distribution)")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the installed gitmate version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """AI-assisted git commit messages, PR summaries, and changelogs."""


def _stub(name: str, milestone: str) -> None:
    """Report a planned command that has no implementation yet."""
    console.print(f"[yellow]{name}[/yellow] is not implemented yet ({milestone}).")


@app.command()
def commit(
    yes: bool = typer.Option(False, "--yes", "-y", help="Commit without interactive confirmation."),
) -> None:
    """Generate a commit message for the staged diff and review before committing."""
    from gitmate.committer import commit_flow

    code = commit_flow(yes=yes, console=console, secret_store=secret_store)
    if code != 0:
        raise typer.Exit(code)


@app.command("install-hook")
def install_hook() -> None:
    """Install gitmate's local prepare-commit-msg hook in this repository."""
    from gitmate.hooks import HookError
    from gitmate.hooks import install_hook as install

    try:
        path = install()
    except HookError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None
    console.print(f"[green]Installed prepare-commit-msg hook:[/green] {path}")


@app.command("uninstall-hook")
def uninstall_hook() -> None:
    """Remove gitmate's local prepare-commit-msg hook."""
    from gitmate.hooks import HookError
    from gitmate.hooks import uninstall_hook as uninstall

    try:
        path = uninstall()
    except HookError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None
    console.print(f"[green]Removed gitmate prepare-commit-msg hook:[/green] {path}")


@app.command()
def stats(
    days: int | None = typer.Option(None, "--days", "-d", help="Limit stats to the last N days."),
    month: bool = typer.Option(
        False, "--month", "-m", help="Show stats for current calendar month."
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Output raw JSON for scripting or README tables."
    ),
) -> None:
    """Display telemetry metrics, cache hit rate, token usage, and estimated spend."""
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime, timedelta

    from gitmate.metrics import get_aggregate_stats

    since: datetime | None = None
    now = datetime.now(UTC)
    if month:
        since = datetime(now.year, now.month, 1, tzinfo=UTC)
    elif days is not None:
        if days <= 0:
            raise typer.BadParameter("--days must be a positive integer.")
        since = now - timedelta(days=days)

    stats_data = get_aggregate_stats(since=since)

    if raw:
        typer.echo(json.dumps(asdict(stats_data), indent=2))
        return

    if stats_data.total_invocations == 0:
        console.print(
            "[yellow]No invocations recorded yet. Run 'gitmate commit' to start collecting metrics.[/yellow]"
        )
        return

    time_label = "Current Month" if month else (f"Last {days} Days" if days else "All Time")
    table = Table(title=f"gitmate stats ({time_label})", show_header=True)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total Invocations", str(stats_data.total_invocations))
    table.add_row("Cache Hits", f"{stats_data.cache_hits} ({stats_data.cache_hit_rate:.1f}%)")
    table.add_row(
        "Tokens (In / Out / Total)",
        f"{stats_data.total_tokens_in:,} / {stats_data.total_tokens_out:,} / {stats_data.total_tokens:,}",
    )
    table.add_row("Estimated Spend", f"${stats_data.total_cost_usd:.4f}")
    table.add_row("Avg Latency (Overall)", f"{stats_data.avg_latency_ms:.0f} ms")
    table.add_row("Avg Latency (Cache Hit)", f"{stats_data.avg_latency_hit_ms:.0f} ms")
    table.add_row("Avg Latency (Cache Miss)", f"{stats_data.avg_latency_miss_ms:.0f} ms")
    table.add_row("Fallback Templates Used", str(stats_data.fallback_count))

    console.print(table)

    if stats_data.by_command:
        cmd_table = Table(title="By Command", show_header=True)
        cmd_table.add_column("Command")
        cmd_table.add_column("Runs", justify="right")
        cmd_table.add_column("Hit Rate", justify="right")
        cmd_table.add_column("Tokens", justify="right")
        cmd_table.add_column("Spend", justify="right")
        cmd_table.add_column("Avg Latency", justify="right")
        for cmd, b in sorted(stats_data.by_command.items()):
            cmd_table.add_row(
                cmd,
                str(b.invocations),
                f"{b.cache_hit_rate:.1f}%",
                f"{b.total_tokens:,}",
                f"${b.cost_usd:.4f}",
                f"{b.avg_latency_ms:.0f} ms",
            )
        console.print(cmd_table)


@app.command("pr-summary")
def pr_summary(
    base: str = typer.Option("main", "--base", "-b", help="Base branch to compare against."),
    title: str | None = typer.Option(None, "--title", "-t", help="PR title for gh pr create."),
    copy: bool = typer.Option(True, "--copy/--no-copy", help="Copy summary to clipboard."),
    create_pr: bool = typer.Option(
        False, "--create-pr", help="Open GitHub PR create flow with gh CLI."
    ),
    bypass_cache: bool = typer.Option(False, "--bypass-cache", help="Bypass cached generations."),
) -> None:
    """Generate a PR description by comparing HEAD against a base branch."""
    from rich.markdown import Markdown
    from rich.panel import Panel

    from gitmate.diff_extractor import GitCommandError
    from gitmate.orchestrator import BudgetCapExceededError
    from gitmate.pr_summary import GitHubCliError, generate_pr_summary

    try:
        res = generate_pr_summary(
            base=base,
            title=title,
            console=err_console,
            secret_store=secret_store,
            bypass_cache=bypass_cache,
            copy_to_cb=copy,
            create_pr=create_pr,
        )
    except (GitCommandError, BudgetCapExceededError, GitHubCliError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None

    if res.text.startswith("No changes between"):
        err_console.print(f"[dim]{res.text}[/dim]")
        return

    status_badge = (
        "[yellow bold]⚠ FALLBACK TEMPLATE[/yellow bold]"
        if res.is_fallback
        else f"[green bold]{res.model}[/green bold]"
    )
    panel = Panel(
        Markdown(res.text),
        title=f"PR Summary [dim]({base}...HEAD) ({status_badge})[/dim]",
        border_style="yellow" if res.is_fallback else "green",
        padding=(1, 2),
    )
    console.print(panel)


@app.command("changelog")
def changelog(
    from_ref: str = typer.Option(
        ..., "--from", "-f", help="Starting git revision or tag (exclusive)."
    ),
    to_ref: str = typer.Option(
        "HEAD", "--to", "-t", help="Ending git revision or tag (inclusive)."
    ),
    output: Path | None = typer.Option(  # noqa: B008
        None, "--output", "-o", help="Output file path (prints to stdout if omitted)."
    ),
    include_diff: bool = typer.Option(
        False, "--include-diff", help="Include diff summaries in addition to commits."
    ),
    bypass_cache: bool = typer.Option(False, "--bypass-cache", help="Bypass cached generations."),
) -> None:
    """Generate a changelog section between two git tags or revisions."""
    from gitmate.changelog import generate_changelog
    from gitmate.diff_extractor import GitCommandError
    from gitmate.orchestrator import BudgetCapExceededError

    try:
        res = generate_changelog(
            from_ref=from_ref,
            to_ref=to_ref,
            include_diff=include_diff,
            output_file=output,
            console=err_console,
            secret_store=secret_store,
            bypass_cache=bypass_cache,
        )
    except (GitCommandError, BudgetCapExceededError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None

    if res.text.startswith("No commits found"):
        err_console.print(f"[dim]{res.text}[/dim]")
        return

    if output is None:
        typer.echo(res.text)


@app.command()
def doc() -> None:
    """Planned docs helper (no implementing phase assigned yet)."""
    _stub("doc", "not scheduled yet")


@app.command(name="debug-diff")
def debug_diff(
    base: str | None = typer.Option(
        None, "--base", help="Compare branch base against HEAD (e.g. --base main)."
    ),
    from_ref: str | None = typer.Option(None, "--from-ref", help="Range start ref."),
    to_ref: str | None = typer.Option(None, "--to-ref", help="Range end ref."),
    summary: bool = typer.Option(
        False, "--summary", help="Show per-file stats only, no patch text."
    ),
) -> None:
    """Print the cleaned, filtered diff (defaults to staged changes)."""
    if base is not None and (from_ref is not None or to_ref is not None):
        raise typer.BadParameter("--base cannot be combined with --from-ref/--to-ref.")
    if (from_ref is None) != (to_ref is None):
        raise typer.BadParameter("--from-ref and --to-ref must be given together.")
    try:
        cfg = config_mod.load_config()
        extractor = DiffExtractor(extra_ignores=cfg.ignore_globs)
        if base is not None:
            diffs = extractor.branch_comparison(base)
        elif from_ref is not None and to_ref is not None:
            diffs = extractor.rev_range(from_ref, to_ref)
        else:
            diffs = extractor.staged()
    except (GitCommandError, config_mod.ConfigError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None
    if not diffs:
        console.print("No changes.")
        return
    table = Table(title="diff", show_header=True)
    table.add_column("file")
    table.add_column("status")
    table.add_column("+", justify="right")
    table.add_column("-", justify="right")
    table.add_column("note")
    for diff in diffs:
        name = diff.path if diff.old_path is None else f"{diff.old_path} -> {diff.path}"
        note = "binary, patch skipped" if diff.is_binary else ""
        table.add_row(name, diff.status, str(diff.additions), str(diff.deletions), note)
    console.print(table)
    if summary:
        return
    for diff in diffs:
        if diff.patch_text:
            console.print(f"[bold]{diff.path}[/bold]")
            console.print(diff.patch_text, markup=False, highlight=False, crop=False)


@config_app.command("set-key")
def config_set_key() -> None:
    """Store the LLM API key in the OS credential store."""
    secret = typer.prompt("API key", hide_input=True, confirmation_prompt=True)
    if not secret.strip():
        raise typer.BadParameter("API key must not be empty.")
    secret_store.set_secret(API_KEY_ACCOUNT, secret)
    console.print("[green]API key saved to the OS credential store.[/green]")


@config_app.command("show")
def config_show() -> None:
    """Print current settings (never prints the secret itself)."""
    try:
        cfg = config_mod.load_config()
    except config_mod.ConfigError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None
    key_set = secret_store.get_secret(API_KEY_ACCOUNT) is not None
    table = Table(title="gitmate config", show_header=False)
    table.add_column("field")
    table.add_column("value")
    table.add_row("config file", str(config_mod.config_path()))
    for f in dataclasses.fields(cfg):
        val = getattr(cfg, f.name)
        if val is None:
            display_val = "none"
        elif isinstance(val, list):
            display_val = ", ".join(val) if val else "none"
        else:
            display_val = str(val)
        table.add_row(f.name, display_val)
    table.add_row("api_key", "set" if key_set else "not set")
    console.print(table)


@config_app.command("set")
def config_set(field: str, value: str) -> None:
    """Set any config field: model, commit_style, budget_cap_usd, ignore_globs, cache_dir, etc."""
    try:
        cfg = config_mod.load_config()
    except config_mod.ConfigError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None

    field_map = {f.name: f for f in dataclasses.fields(config_mod.GitmateConfig)}
    if field not in field_map:
        valid_fields = ", ".join(field_map.keys())
        raise typer.BadParameter(f"unknown field {field!r}; expected one of: {valid_fields}.")

    if field == "ignore_globs":
        cfg.ignore_globs = [g.strip() for g in value.split(",") if g.strip()]
    elif field == "budget_cap_usd":
        cfg.budget_cap_usd = None if value in ("", "none") else _parse_budget(value)
    elif field in ("max_context_tokens", "reserved_output_tokens", "template_overhead"):
        if field == "max_context_tokens" and value in ("", "none"):
            cfg.max_context_tokens = None
        else:
            try:
                int_val = int(value)
                if int_val <= 0:
                    raise ValueError
            except ValueError:
                raise typer.BadParameter(f"'{field}' must be a positive integer.") from None
            setattr(cfg, field, int_val)
    elif field == "cache_dir":
        cfg.cache_dir = None if value in ("", "none") else value.strip() or None
    elif field in ("allow_noninteractive_commit", "free_tier"):
        val_clean = value.strip().lower()
        if val_clean == "true":
            setattr(cfg, field, True)
        elif val_clean == "false":
            setattr(cfg, field, False)
        else:
            raise typer.BadParameter(f"'{field}' must be 'true' or 'false'.")
    elif field in ("model", "commit_style"):
        stripped = value.strip()
        if not stripped:
            raise typer.BadParameter(f"'{field}' must not be empty.")
        if field == "commit_style" and stripped not in config_mod.COMMIT_STYLES:
            valid_styles = ", ".join(config_mod.COMMIT_STYLES)
            raise typer.BadParameter(f"'commit_style' must be one of: {valid_styles}.")
        setattr(cfg, field, stripped)
    else:
        setattr(cfg, field, value)

    # Validate overall config integrity
    max_ctx = cfg.max_context_tokens
    reserved = cfg.reserved_output_tokens
    overhead = cfg.template_overhead
    if max_ctx is not None and max_ctx <= (reserved + overhead):
        raise typer.BadParameter(
            f"'max_context_tokens' ({max_ctx}) must be greater than "
            f"'reserved_output_tokens' + 'template_overhead' ({reserved + overhead})."
        )

    config_mod.save_config(cfg)
    console.print(f"[green]Set {field}.[/green]")


def _parse_budget(value: str) -> float:
    """Parse a budget string, rejecting non-numeric input."""
    try:
        return float(value)
    except ValueError:
        raise typer.BadParameter(f"{value!r} is not a number.") from None
