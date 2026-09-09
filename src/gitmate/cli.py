"""Typer app, command definitions only — no business logic here."""

from __future__ import annotations

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
    table.add_row("model", cfg.model)
    table.add_row("commit_style", cfg.commit_style)
    table.add_row(
        "budget_cap_usd", str(cfg.budget_cap_usd) if cfg.budget_cap_usd is not None else "none"
    )
    table.add_row("ignore_globs", ", ".join(cfg.ignore_globs) if cfg.ignore_globs else "none")
    table.add_row("api_key", "set" if key_set else "not set")
    console.print(table)


@config_app.command("set")
def config_set(field: str, value: str) -> None:
    """Set one field: model, commit_style, budget_cap_usd, ignore_globs."""
    try:
        cfg = config_mod.load_config()
    except config_mod.ConfigError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None
    match field:
        case "model":
            cfg.model = value
        case "commit_style":
            cfg.commit_style = value
        case "budget_cap_usd":
            cfg.budget_cap_usd = None if value == "" else _parse_budget(value)
        case "ignore_globs":
            cfg.ignore_globs = [g.strip() for g in value.split(",") if g.strip()]
        case _:
            raise typer.BadParameter(
                f"unknown field {field!r}; expected model, commit_style, "
                "budget_cap_usd, or ignore_globs."
            )
    config_mod.save_config(cfg)
    console.print(f"[green]Set {field}.[/green]")


def _parse_budget(value: str) -> float:
    """Parse a budget string, rejecting non-numeric input."""
    try:
        return float(value)
    except ValueError:
        raise typer.BadParameter(f"{value!r} is not a number.") from None
