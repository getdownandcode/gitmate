"""Tests for diff chunking and omitted diff summarization."""

from __future__ import annotations

from gitmate.chunking import chunk_patch_text, summarize_omitted_diffs
from gitmate.diff_extractor import FileDiff
from gitmate.providers.base import LLMResponse


class FakeChunkProvider:
    """Mock LLMProvider recording chunk summarization calls."""

    def __init__(self, response: str = "Added feature X") -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str, model: str) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(text=self.response, model=model, input_tokens=40, output_tokens=10)


def test_chunk_patch_text_small() -> None:
    patch = "@@ -1,3 +1,3 @@\n-old\n+new\n"
    chunks = chunk_patch_text(patch, max_chars=100)
    assert chunks == [patch]


def test_chunk_patch_text_splits_at_lines() -> None:
    lines = [f"+line {i}\n" for i in range(20)]
    patch = "".join(lines)
    chunks = chunk_patch_text(patch, max_chars=50)
    assert len(chunks) > 1
    assert "".join(chunks) == patch


def test_summarize_omitted_diffs_binary_and_empty() -> None:
    binary_diff = FileDiff(
        path="assets/logo.png",
        old_path=None,
        status="modified",
        additions=0,
        deletions=0,
        patch_text="",
        is_binary=True,
    )
    empty_diff = FileDiff(
        path="empty.txt",
        old_path=None,
        status="modified",
        additions=0,
        deletions=0,
        patch_text="   ",
        is_binary=False,
    )
    provider = FakeChunkProvider()
    merged, total_in, total_out = summarize_omitted_diffs(
        [binary_diff, empty_diff], provider, "gemini-3.5-flash-lite"
    )
    assert "- assets/logo.png: [binary or empty file]" in merged
    assert "- empty.txt: [binary or empty file]" in merged
    assert provider.prompts == []
    assert total_in == 0
    assert total_out == 0


def test_summarize_omitted_diffs_calls_provider_and_aggregates_tokens() -> None:
    diff1 = FileDiff(
        path="src/auth.py",
        old_path=None,
        status="modified",
        additions=10,
        deletions=2,
        patch_text="@@ -1,5 +1,10 @@\n+auth logic\n",
        is_binary=False,
    )
    diff2 = FileDiff(
        path="src/db.py",
        old_path=None,
        status="modified",
        additions=5,
        deletions=1,
        patch_text="@@ -1,5 +1,5 @@\n+db connection\n",
        is_binary=False,
    )
    provider = FakeChunkProvider(response="Updated service logic")
    merged, total_in, total_out = summarize_omitted_diffs(
        [diff1, diff2], provider, "gemini-3.5-flash-lite"
    )
    assert len(provider.prompts) == 2
    assert "- src/auth.py: Updated service logic" in merged
    assert "- src/db.py: Updated service logic" in merged
    assert total_in == 80
    assert total_out == 20
