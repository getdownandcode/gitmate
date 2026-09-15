"""Prompt template loading, version extraction, and rendering."""

from __future__ import annotations

import importlib.resources
import string
from dataclasses import dataclass

TEMPLATE_ALIASES: dict[str, str] = {
    "conventional": "commit_conventional",
    "plain": "commit_plain",
    "pr_summary": "pr_summary",
    "commit_conventional": "commit_conventional",
    "commit_plain": "commit_plain",
}


class TemplateError(Exception):
    """Base exception for prompt template failures."""


class TemplateNotFoundError(TemplateError):
    """Raised when a requested prompt template cannot be found."""


class InvalidTemplateError(TemplateError):
    """Raised when a template file contains an invalid version or format."""


@dataclass(frozen=True)
class PromptTemplate:
    """A versioned prompt template whose version_key drives cache invalidation."""

    name: str
    version: str
    body: str

    @property
    def version_key(self) -> str:
        """Stable key combining template name and version for diskcache hashing."""
        return f"{self.name}:v{self.version}"

    def render(self, **kwargs: str) -> str:
        """Substitute variables using safe string template interpolation."""
        template = string.Template(self.body)
        return template.safe_substitute(**kwargs).strip()


def parse_template_content(content: str, name: str = "template") -> PromptTemplate:
    """Parse a template string containing an optional 'version: <v>' header."""
    lines = content.strip().splitlines()
    if not lines:
        raise InvalidTemplateError(f"Template {name!r} is empty.")

    version = "1"
    body_lines: list[str] = []

    if lines[0].strip().startswith("version:"):
        ver_part = lines[0].strip().split("version:", 1)[1].strip()
        if not ver_part:
            raise InvalidTemplateError(f"Template {name!r} has empty version header.")
        version = ver_part

        # Skip separator '---' if present
        start_idx = 1
        if len(lines) > 1 and lines[1].strip() == "---":
            start_idx = 2
        body_lines = lines[start_idx:]
    else:
        body_lines = lines

    body = "\n".join(body_lines).strip()
    if not body:
        raise InvalidTemplateError(f"Template {name!r} has no prompt content.")

    return PromptTemplate(name=name, version=version, body=body)


def load_template(style: str) -> PromptTemplate:
    """Load a versioned prompt template by style name from package resources."""
    normalized = style.strip().lower()
    template_name = TEMPLATE_ALIASES.get(normalized)
    if template_name is None:
        valid_styles = ", ".join(sorted(set(TEMPLATE_ALIASES.keys())))
        raise TemplateNotFoundError(
            f"Unknown template style {style!r}. Expected one of: {valid_styles}."
        )

    filename = f"{template_name}.txt"
    try:
        ref = importlib.resources.files("gitmate.templates").joinpath(filename)
        content = ref.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise TemplateNotFoundError(f"Template file {filename!r} not found: {exc}") from exc

    return parse_template_content(content, name=template_name)


def render_commit_prompt(style: str, diff_text: str, summary_note: str = "") -> tuple[str, str]:
    """Render a commit prompt for the given style and return (prompt, version_key)."""
    template = load_template(style)
    rendered = template.render(diff=diff_text, summary_note=summary_note)
    return rendered, template.version_key
