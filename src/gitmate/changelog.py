"""Changelog generation from conventional commits and git diffs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from gitmate import config as config_mod
from gitmate.config import GitmateConfig, SecretStore
from gitmate.diff_extractor import DiffExtractor, GitCommandError, GitRunner, SubprocessGitRunner
from gitmate.fallback import generate_fallback_changelog
from gitmate.orchestrator import GenerationResult, check_budget_cap, run_generation_pipeline
from gitmate.providers.base import LLMProvider
from gitmate.token_budget import TokenCounter

logger = logging.getLogger("gitmate.changelog")

CONVENTIONAL_PATTERN = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<desc>.*)$"
)

CATEGORIES: list[str] = [
    "Features",
    "Bug Fixes",
    "Performance & Refactoring",
    "Documentation",
    "Maintenance & Tooling",
    "Other Changes",
]

TYPE_TO_CATEGORY: dict[str, str] = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance & Refactoring",
    "refactor": "Performance & Refactoring",
    "docs": "Documentation",
    "chore": "Maintenance & Tooling",
    "build": "Maintenance & Tooling",
    "ci": "Maintenance & Tooling",
    "test": "Maintenance & Tooling",
    "style": "Maintenance & Tooling",
}


@dataclass(frozen=True)
class CommitLogEntry:
    """Parsed commit record with conventional commit metadata."""

    commit_hash: str
    subject: str
    body: str
    author: str
    date: str
    commit_type: str | None = None
    scope: str | None = None
    description: str | None = None
    is_breaking: bool = False


def parse_commit_message(
    commit_hash: str,
    subject: str,
    body: str = "",
    author: str = "",
    date: str = "",
) -> CommitLogEntry:
    """Parse a single commit subject into conventional commit type, scope, and description."""
    match = CONVENTIONAL_PATTERN.match(subject.strip())
    if match:
        c_type = match.group("type").lower()
        scope = match.group("scope")
        desc = match.group("desc").strip()
        is_breaking = bool(match.group("breaking")) or (
            "BREAKING CHANGE:" in body or "BREAKING-CHANGE:" in body
        )
        return CommitLogEntry(
            commit_hash=commit_hash,
            subject=subject.strip(),
            body=body.strip(),
            author=author.strip(),
            date=date.strip(),
            commit_type=c_type,
            scope=scope,
            description=desc,
            is_breaking=is_breaking,
        )

    is_breaking = "BREAKING CHANGE:" in body or "BREAKING-CHANGE:" in body
    return CommitLogEntry(
        commit_hash=commit_hash,
        subject=subject.strip(),
        body=body.strip(),
        author=author.strip(),
        date=date.strip(),
        commit_type=None,
        scope=None,
        description=subject.strip(),
        is_breaking=is_breaking,
    )


def extract_commits_between(
    from_ref: str,
    to_ref: str = "HEAD",
    runner: GitRunner | None = None,
    repo: Path | None = None,
) -> list[CommitLogEntry]:
    """Retrieve and parse git commits in the revision range from_ref..to_ref."""
    git_runner = runner if runner is not None else SubprocessGitRunner()
    repo_dir = repo if repo is not None else Path.cwd()

    # Use ASCII record separator \x1e between commits and \x00 between fields
    output = git_runner.run(
        ["log", f"{from_ref}..{to_ref}", "--format=%H%x00%s%x00%b%x00%an%x00%ad%x1e"],
        repo_dir,
    )

    commits: list[CommitLogEntry] = []
    records = output.split("\x1e")
    for rec in records:
        rec = rec.strip()
        if not rec:
            continue
        parts = rec.split("\x00")
        if len(parts) >= 5:
            commit_hash, subject, body, author, date = (
                parts[0],
                parts[1],
                parts[2],
                parts[3],
                parts[4],
            )
            commits.append(
                parse_commit_message(
                    commit_hash=commit_hash,
                    subject=subject,
                    body=body,
                    author=author,
                    date=date,
                )
            )
        elif len(parts) >= 2:
            commits.append(
                parse_commit_message(
                    commit_hash=parts[0],
                    subject=parts[1],
                )
            )

    return commits


def group_commits(commits: list[CommitLogEntry]) -> dict[str, list[CommitLogEntry]]:
    """Group commits into conventional categories with an explicit 'Other Changes' bucket."""
    grouped: dict[str, list[CommitLogEntry]] = {cat: [] for cat in CATEGORIES}

    for c in commits:
        if c.commit_type and c.commit_type in TYPE_TO_CATEGORY:
            category = TYPE_TO_CATEGORY[c.commit_type]
        else:
            category = "Other Changes"
        grouped[category].append(c)

    return grouped


def format_commits_for_prompt(grouped: dict[str, list[CommitLogEntry]]) -> str:
    """Format grouped commits into markdown sections for prompt template substitution."""
    sections: list[str] = []
    for category, items in grouped.items():
        if not items:
            continue
        sections.append(f"### {category}")
        for item in items:
            scope_prefix = f"({item.scope}): " if item.scope else ": "
            t = item.commit_type or "other"
            breaking_marker = "!" if item.is_breaking else ""
            desc = item.description or item.subject
            sections.append(f"- [{item.commit_hash[:7]}] {t}{breaking_marker}{scope_prefix}{desc}")
        sections.append("")

    return "\n".join(sections).strip()


def generate_changelog(
    from_ref: str,
    to_ref: str = "HEAD",
    include_diff: bool = False,
    output_file: Path | None = None,
    cfg: GitmateConfig | None = None,
    diff_extractor: DiffExtractor | None = None,
    git_runner: GitRunner | None = None,
    provider: LLMProvider | None = None,
    counter: TokenCounter | None = None,
    console: Console | None = None,
    secret_store: SecretStore | None = None,
    bypass_cache: bool = False,
    repo_dir: Path | None = None,
) -> GenerationResult:
    """Generate structured release notes between two git references."""
    con = console if console is not None else Console()
    active_cfg = cfg if cfg is not None else config_mod.load_config()

    check_budget_cap(active_cfg, console=con)

    repo_path = repo_dir if repo_dir is not None else Path.cwd()
    runner = git_runner if git_runner is not None else SubprocessGitRunner()

    commits = extract_commits_between(from_ref, to_ref, runner=runner, repo=repo_path)
    if not commits:
        return GenerationResult(
            text=f"No commits found between '{from_ref}' and '{to_ref}'.",
            is_fallback=False,
            model=active_cfg.model,
        )

    extractor = (
        diff_extractor
        if diff_extractor is not None
        else DiffExtractor(repo=repo_path, runner=runner)
    )
    diffs = []
    if include_diff:
        try:
            diffs = extractor.rev_range(from_ref, to_ref)
        except GitCommandError as exc:
            con.print(
                f"[yellow]warning: Could not extract diffs ({exc}), proceeding with commits only.[/yellow]"
            )

    grouped = group_commits(commits)
    formatted_commits = format_commits_for_prompt(grouped)

    gen_result = run_generation_pipeline(
        diffs=diffs,
        cfg=active_cfg,
        template_name="changelog",
        fallback_generator=lambda: generate_fallback_changelog(
            commits, from_ref=from_ref, to_ref=to_ref
        ),
        template_vars={
            "commits": formatted_commits,
            "from_ref": from_ref,
            "to_ref": to_ref,
        },
        command="changelog",
        provider=provider,
        counter=counter,
        console=con,
        secret_store=secret_store,
        bypass_cache=bypass_cache,
    )

    if output_file is not None:
        try:
            output_file.write_text(gen_result.text + "\n", encoding="utf-8")
            con.print(f"[green]✔ Changelog written to {output_file}[/green]")
        except OSError as exc:
            con.print(f"[red]error writing changelog file:[/red] {exc}")

    return gen_result
