"""Typer app, command definitions only — no business logic here."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from gitmate import config as config_mod
from gitmate.config import API_KEY_ACCOUNT

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
    cfg = config_mod.load_config()
    key_set = secret_store.get_secret(API_KEY_ACCOUNT) is not None
    table = Table(title="gitmate config", show_header=False)
    table.add_column("field")
    table.add_column("value")
    table.add_row("config file", str(config_mod.config_path()))
    table.add_row("model", cfg.model)
    table.add_row("commit_style", cfg.commit_style)
    table.add_row("budget_cap_usd", str(cfg.budget_cap_usd) if cfg.budget_cap_usd else "none")
    table.add_row("api_key", "set" if key_set else "not set")
    console.print(table)


@config_app.command("set")
def config_set(field: str, value: str) -> None:
    """Set one field: model, commit_style, or budget_cap_usd (empty clears)."""
    cfg = config_mod.load_config()
    match field:
        case "model":
            cfg.model = value
        case "commit_style":
            cfg.commit_style = value
        case "budget_cap_usd":
            cfg.budget_cap_usd = None if value == "" else _parse_budget(value)
        case _:
            raise typer.BadParameter(
                f"unknown field {field!r}; expected model, commit_style, or budget_cap_usd."
            )
    config_mod.save_config(cfg)
    console.print(f"[green]Set {field}.[/green]")


def _parse_budget(value: str) -> float:
    """Parse a budget string, rejecting non-numeric input."""
    try:
        return float(value)
    except ValueError:
        raise typer.BadParameter(f"{value!r} is not a number.") from None
