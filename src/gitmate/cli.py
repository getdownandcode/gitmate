"""Typer app, command definitions only — no business logic here."""

from __future__ import annotations

import dataclasses

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
secret_store: config_mod.SecretStore = config_mod.KeyringSecretStore()


def _stub(name: str, milestone: str) -> None:
    """Report a planned command that has no implementation yet."""
    console.print(f"[yellow]{name}[/yellow] is not implemented yet ({milestone}).")


@app.command()
def commit() -> None:
    """Generate a commit message for the staged diff."""
    _stub("commit", "coming in Phase 5")


@app.command()
def pr_summary() -> None:
    """Generate a PR description for the current branch."""
    _stub("pr-summary", "coming in Phase 7")


@app.command()
def changelog() -> None:
    """Generate a changelog section for a tag range."""
    _stub("changelog", "coming in Phase 7")


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
    elif field in ("model", "commit_style"):
        stripped = value.strip()
        if not stripped:
            raise typer.BadParameter(f"'{field}' must not be empty.")
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
