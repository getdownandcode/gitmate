"""Tests for the offline, deterministic template-based fallback generator."""

from __future__ import annotations

from pathlib import Path

from gitmate.config import GitmateConfig
from gitmate.diff_extractor import FileDiff
from gitmate.fallback import generate_commit_message, generate_fallback_message
from gitmate.providers.base import LLMProvider, LLMResponse
from gitmate.providers.cache import CachedProvider
from gitmate.token_budget import TokenCounter


def test_empty_diffs() -> None:
    assert generate_fallback_message([], style="conventional") == "chore: empty commit"
    assert generate_fallback_message([], style="plain") == "Empty commit"


def test_single_file_added_conventional() -> None:
    diff = FileDiff(
        path="src/auth/login.py",
        old_path=None,
        status="added",
        additions=45,
        deletions=0,
        patch_text="+ class Login...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="conventional")
    assert msg == "feat(auth): add login.py"


def test_single_file_added_plain() -> None:
    diff = FileDiff(
        path="src/auth/login.py",
        old_path=None,
        status="added",
        additions=45,
        deletions=0,
        patch_text="+ class Login...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="plain")
    assert msg == "Add src/auth/login.py"


def test_single_file_modified_conventional() -> None:
    diff = FileDiff(
        path="src/gitmate/cli.py",
        old_path=None,
        status="modified",
        additions=10,
        deletions=2,
        patch_text="@@ ...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="conventional")
    assert msg == "chore: update cli.py"


def test_single_file_deleted_conventional() -> None:
    diff = FileDiff(
        path="src/auth/old_auth.py",
        old_path=None,
        status="deleted",
        additions=0,
        deletions=50,
        patch_text="- def old...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="conventional")
    assert msg == "chore(auth): remove old_auth.py"


def test_single_file_renamed_conventional() -> None:
    diff = FileDiff(
        path="src/auth/auth_v2.py",
        old_path="src/auth/auth_v1.py",
        status="renamed",
        additions=5,
        deletions=2,
        patch_text="@@ ...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="conventional")
    assert msg == "refactor(auth): rename auth_v1.py to auth_v2.py"


def test_single_file_docs_conventional() -> None:
    diff = FileDiff(
        path="README.md",
        old_path=None,
        status="modified",
        additions=5,
        deletions=1,
        patch_text="@@ ...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="conventional")
    assert msg == "docs: update README.md"


def test_single_file_test_conventional() -> None:
    diff = FileDiff(
        path="tests/test_cli.py",
        old_path=None,
        status="modified",
        additions=20,
        deletions=0,
        patch_text="@@ ...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="conventional")
    assert msg == "test: update test_cli.py"


def test_single_file_build_conventional() -> None:
    diff = FileDiff(
        path="pyproject.toml",
        old_path=None,
        status="modified",
        additions=1,
        deletions=0,
        patch_text="@@ ...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="conventional")
    assert msg == "build: update pyproject.toml"


def test_single_file_ci_conventional() -> None:
    diff = FileDiff(
        path=".github/workflows/ci.yml",
        old_path=None,
        status="modified",
        additions=3,
        deletions=1,
        patch_text="@@ ...",
        is_binary=False,
    )
    msg = generate_fallback_message([diff], style="conventional")
    assert msg == "ci: update ci.yml"


def test_multi_file_same_scope_conventional() -> None:
    diffs = [
        FileDiff(
            path="src/providers/gemini.py",
            old_path=None,
            status="modified",
            additions=50,
            deletions=5,
            patch_text="...",
            is_binary=False,
        ),
        FileDiff(
            path="src/providers/anthropic.py",
            old_path=None,
            status="added",
            additions=80,
            deletions=0,
            patch_text="...",
            is_binary=False,
        ),
    ]
    msg = generate_fallback_message(diffs, style="conventional")
    lines = msg.splitlines()
    assert lines[0] == "feat(providers): update 2 files (added: 1, modified: 1)"
    assert "- Add src/providers/anthropic.py (+80)" in msg
    assert "- Modify src/providers/gemini.py (+50, -5)" in msg


def test_multi_file_plain_style() -> None:
    diffs = [
        FileDiff(
            path="src/auth/login.py",
            old_path=None,
            status="added",
            additions=10,
            deletions=0,
            patch_text="...",
            is_binary=False,
        ),
        FileDiff(
            path="src/auth/logout.py",
            old_path=None,
            status="modified",
            additions=5,
            deletions=2,
            patch_text="...",
            is_binary=False,
        ),
    ]
    msg = generate_fallback_message(diffs, style="plain")
    lines = msg.splitlines()
    assert lines[0] == "Update 2 files in auth (added: 1, modified: 1)"
    assert "- Add src/auth/login.py (+10)" in msg
    assert "- Modify src/auth/logout.py (+5, -2)" in msg


def test_multi_file_cross_subsystem_no_shared_scope() -> None:
    diffs = [
        FileDiff(
            path="src/auth/login.py",
            old_path=None,
            status="added",
            additions=10,
            deletions=0,
            patch_text="...",
            is_binary=False,
        ),
        FileDiff(
            path="docs/guide.md",
            old_path=None,
            status="added",
            additions=5,
            deletions=0,
            patch_text="...",
            is_binary=False,
        ),
    ]
    msg = generate_fallback_message(diffs, style="conventional")
    lines = msg.splitlines()
    assert lines[0] == "feat: update 2 files (added: 2)"


def test_multi_file_with_binary_and_renames() -> None:
    diffs = [
        FileDiff(
            path="assets/logo.png",
            old_path=None,
            status="added",
            additions=0,
            deletions=0,
            patch_text="",
            is_binary=True,
        ),
        FileDiff(
            path="src/old.py",
            old_path="src/legacy.py",
            status="renamed",
            additions=0,
            deletions=0,
            patch_text="",
            is_binary=False,
        ),
    ]
    msg = generate_fallback_message(diffs, style="conventional")
    assert "- Binary file: assets/logo.png" in msg
    assert "- Rename src/legacy.py -> src/old.py" in msg


class _MockProvider(LLMProvider):
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0

    def generate(self, prompt: str, model: str) -> LLMResponse:
        resp = self.responses[min(self.call_count, len(self.responses) - 1)]
        self.call_count += 1
        return LLMResponse(text=resp, model=model, input_tokens=50, output_tokens=10)


class _SimpleCounter(TokenCounter):
    def count_tokens(self, text: str, model: str) -> int:
        return 10


def test_generate_commit_message_bypass_cache(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cfg = GitmateConfig(cache_dir=str(cache_dir))
    diffs = [
        FileDiff(
            path="src/main.py",
            old_path=None,
            status="modified",
            additions=5,
            deletions=1,
            patch_text="@@ ...",
            is_binary=False,
        )
    ]
    raw_provider = _MockProvider(["feat: initial proposal", "feat: fresh regenerated"])
    cached = CachedProvider(provider=raw_provider, cache_dir=str(cache_dir))
    counter = _SimpleCounter()

    # 1. First generation with cache enabled -> cache miss
    res1 = generate_commit_message(
        diffs, cfg=cfg, provider=cached, counter=counter, bypass_cache=False
    )
    assert res1.text == "feat: initial proposal"
    assert res1.cache_hit is False
    assert raw_provider.call_count == 1

    # 2. Second generation with cache enabled -> cache hit, no new provider call
    res2 = generate_commit_message(
        diffs, cfg=cfg, provider=cached, counter=counter, bypass_cache=False
    )
    assert res2.text == "feat: initial proposal"
    assert res2.cache_hit is True
    assert raw_provider.call_count == 1

    # 3. Third generation with bypass_cache=True -> bypasses cache, calls provider for fresh message
    res3 = generate_commit_message(
        diffs, cfg=cfg, provider=cached, counter=counter, bypass_cache=True
    )
    assert res3.text == "feat: fresh regenerated"
    assert res3.cache_hit is False
    assert raw_provider.call_count == 2

    # 4. Subsequent generation with bypass_cache=False still returns the cached original entry
    res4 = generate_commit_message(
        diffs, cfg=cfg, provider=cached, counter=counter, bypass_cache=False
    )
    assert res4.text == "feat: initial proposal"
    assert res4.cache_hit is True
    assert raw_provider.call_count == 2
