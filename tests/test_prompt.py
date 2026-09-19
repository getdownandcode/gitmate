"""Tests for prompt template loading, version parsing, and rendering."""

from __future__ import annotations

import pytest

from gitmate.prompt import (
    InvalidTemplateError,
    PromptTemplate,
    TemplateNotFoundError,
    load_template,
    parse_template_content,
    render_commit_prompt,
    render_prompt,
)
from gitmate.providers.cache import cache_key


def test_load_known_templates() -> None:
    for style in ("conventional", "plain", "pr_summary"):
        template = load_template(style)
        assert isinstance(template, PromptTemplate)
        assert template.version == "1"
        assert "$diff" in template.body
        assert template.version_key == f"{template.name}:v1"

    changelog_tpl = load_template("changelog")
    assert isinstance(changelog_tpl, PromptTemplate)
    assert changelog_tpl.version == "2"
    assert changelog_tpl.version_key == "changelog:v2"
    assert "$commits" in changelog_tpl.body


def test_load_template_aliases() -> None:
    conv = load_template("conventional")
    full = load_template("commit_conventional")
    assert conv.name == full.name == "commit_conventional"
    assert conv.body == full.body


def test_load_unknown_template_raises() -> None:
    with pytest.raises(TemplateNotFoundError, match="Unknown template style 'pirate'"):
        load_template("pirate")


def test_parse_template_content_with_separator() -> None:
    raw = "version: 42\n---\nPrompt body with $diff"
    template = parse_template_content(raw, name="custom")
    assert template.name == "custom"
    assert template.version == "42"
    assert template.body == "Prompt body with $diff"
    assert template.version_key == "custom:v42"


def test_parse_template_content_without_separator() -> None:
    raw = "version: 2\nSingle header followed by $diff"
    template = parse_template_content(raw, name="custom")
    assert template.version == "2"
    assert template.body == "Single header followed by $diff"


def test_parse_template_content_default_version() -> None:
    raw = "Just a plain prompt body without version header."
    template = parse_template_content(raw, name="defaulted")
    assert template.version == "1"
    assert template.body == raw


def test_parse_template_content_empty_raises() -> None:
    with pytest.raises(InvalidTemplateError, match="is empty"):
        parse_template_content("", name="empty")


def test_parse_template_content_empty_version_raises() -> None:
    with pytest.raises(InvalidTemplateError, match="has empty version header"):
        parse_template_content("version:\n---\nbody", name="bad_ver")


def test_parse_template_content_empty_body_raises() -> None:
    with pytest.raises(InvalidTemplateError, match="has no prompt content"):
        parse_template_content("version: 1\n---\n   ", name="no_body")


def test_render_with_literal_curly_braces() -> None:
    raw = "version: 1\n---\nFormat: <type>(<scope>): {literal_brace}\nDiff: $diff"
    template = parse_template_content(raw, name="braces")
    rendered = template.render(diff="my diff")
    assert "{literal_brace}" in rendered
    assert "Diff: my diff" in rendered


def test_render_commit_prompt() -> None:
    prompt_text, version_key = render_commit_prompt(
        style="conventional",
        diff_text="+ print('hello')",
        summary_note="Note: 1 file omitted",
    )
    assert "+ print('hello')" in prompt_text
    assert "Note: 1 file omitted" in prompt_text
    assert version_key == "commit_conventional:v1"


def test_render_prompt_changelog() -> None:
    prompt_text, version_key = render_prompt(
        "changelog",
        from_ref="v1.0.0",
        to_ref="v1.1.0",
        commits="- [abc1234] feat: new feature",
        diff="diff text",
        summary_note="1 file changed",
    )
    assert "v1.0.0..v1.1.0" in prompt_text
    assert "# Release Notes (v1.1.0)" in prompt_text
    assert "- [abc1234] feat: new feature" in prompt_text
    assert version_key == "changelog:v2"


def test_template_version_bump_invalidates_cache_key() -> None:
    t1 = parse_template_content("version: 1\n---\nPrompt $diff", name="t")
    t2 = parse_template_content("version: 2\n---\nPrompt $diff", name="t")

    key1 = cache_key("Prompt foo", "gemini-3.5-flash-lite", t1.version_key)
    key2 = cache_key("Prompt foo", "gemini-3.5-flash-lite", t2.version_key)

    assert key1 != key2
