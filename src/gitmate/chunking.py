"""Chunking abstractions: split oversized diffs and summarize via LLMProvider."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gitmate.diff_extractor import FileDiff
    from gitmate.providers.base import LLMProvider


def chunk_patch_text(patch: str, max_chars: int = 8_000) -> list[str]:
    """Split large patch text into chunks if needed to avoid blowing per-call limits."""
    if len(patch) <= max_chars:
        return [patch]
    lines = patch.splitlines(keepends=True)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) > max_chars and current:
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


# Compatibility alias for internal callers
_chunk_patch_text = chunk_patch_text


def summarize_omitted_diffs(
    omitted_diffs: list[FileDiff],
    provider: LLMProvider,
    model: str,
) -> tuple[str, int, int]:
    """Summarize omitted diffs per file/chunk and return merged notes and token usage.

    Consumes BudgetDecision.omitted_diffs (which preserves untruncated patches),
    prompts the provider for concise per-file summaries, and aggregates token usage.
    """
    summaries: list[str] = []
    total_in = 0
    total_out = 0

    for diff in omitted_diffs:
        if diff.is_binary or not diff.patch_text.strip():
            summaries.append(f"- {diff.path}: [binary or empty file]")
            continue

        chunks = chunk_patch_text(diff.patch_text)
        file_part_summaries: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            part_suffix = f" (part {i}/{len(chunks)})" if len(chunks) > 1 else ""
            prompt = (
                f"Summarize the following git diff for {diff.path}{part_suffix} in 1-2 concise bullet points "
                f"(focus on WHAT changed and WHY):\n\n"
                f"{chunk}"
            )
            resp = provider.generate(prompt=prompt, model=model)
            if resp.input_tokens is not None:
                total_in += resp.input_tokens
            if resp.output_tokens is not None:
                total_out += resp.output_tokens
            text = resp.text.strip()
            if text:
                file_part_summaries.append(text)

        joined_file_summary = "; ".join(file_part_summaries) if file_part_summaries else "Updated"
        summaries.append(f"- {diff.path}: {joined_file_summary}")

    return "\n".join(summaries), total_in, total_out
