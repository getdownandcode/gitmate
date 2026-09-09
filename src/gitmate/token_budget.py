"""Token budget manager and token counting abstractions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from gitmate.config import (
    DEFAULT_MODEL,
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    DEFAULT_TEMPLATE_OVERHEAD,
)
from gitmate.diff_extractor import FileDiff

if TYPE_CHECKING:
    from google import genai


class TokenBudgetError(Exception):
    """Base exception for token budgeting and counting errors."""


class TokenCountError(TokenBudgetError):
    """Raised when token counting fails."""


class NoApiKeyError(TokenBudgetError):
    """Raised when an API key is required but missing."""


@runtime_checkable
class TokenCounter(Protocol):
    """Token counting interface so callers and tests can substitute fakes."""

    def count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text for the specified model."""
        ...


class GeminiTokenCounter:
    """TokenCounter implementation calling the google-genai SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        client: genai.Client | None = None,
    ) -> None:
        self._client = client
        self._api_key = api_key

    def _get_client(self) -> genai.Client:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise NoApiKeyError(
                "Gemini API key is required for token counting. Run 'gitmate config set-key' first."
            )
        from google import genai

        self._client = genai.Client(api_key=self._api_key)
        return self._client

    def count_tokens(self, text: str, model: str) -> int:
        """Count tokens using google-genai client.models.count_tokens."""
        if not text:
            return 0
        client = self._get_client()
        try:
            resp = client.models.count_tokens(model=model, contents=text)
            count = getattr(resp, "total_tokens", None)
            if count is None:
                raise TokenCountError("count_tokens response did not contain 'total_tokens'.")
            return int(count)
        except Exception as exc:
            if isinstance(exc, TokenBudgetError):
                raise
            raise TokenCountError(f"failed to count tokens via Gemini SDK: {exc}") from exc


class BudgetStrategy(str, Enum):
    """Strategy decided by the token budget manager."""

    FITS = "fits"
    TRUNCATED = "truncated"
    NEEDS_CHUNKING = "needs_chunking"


@dataclass
class BudgetDecision:
    """Outcome of assessing a diff list against the token budget."""

    strategy: BudgetStrategy
    included_diffs: list[FileDiff]
    omitted_diffs: list[FileDiff]
    total_tokens: int
    budget_limit: int
    reserved_output_tokens: int
    template_overhead: int
    model: str
    summary_note: str


def format_diff_for_counting(diffs: list[FileDiff]) -> str:
    """Format FileDiff objects into text representation for token counting."""
    parts: list[str] = []
    for diff in diffs:
        header = f"File: {diff.path} ({diff.status}, +{diff.additions}/-{diff.deletions})"
        if diff.old_path:
            header += f" (renamed from {diff.old_path})"
        if diff.is_binary:
            parts.append(f"{header}\n[binary file, diff omitted]")
        elif diff.patch_text:
            parts.append(f"{header}\n{diff.patch_text}")
        else:
            parts.append(f"{header}\n[empty or omitted]")
    return "\n\n".join(parts)


class TokenBudgetManager:
    """Manages token allocation, deciding whether diffs fit, truncate, or chunk."""

    def __init__(
        self,
        counter: TokenCounter,
        model: str = DEFAULT_MODEL,
        context_window: int = 1_048_576,
        reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
        default_template_overhead: int = DEFAULT_TEMPLATE_OVERHEAD,
    ) -> None:
        self.counter = counter
        self.model = model
        self.context_window = context_window
        self.reserved_output_tokens = reserved_output_tokens
        self.default_template_overhead = default_template_overhead

    def count_diff_tokens(self, diffs: list[FileDiff]) -> int:
        """Calculate token count for a list of FileDiffs."""
        if not diffs:
            return 0
        text = format_diff_for_counting(diffs)
        return self.counter.count_tokens(text, self.model)

    def assess(
        self,
        diffs: list[FileDiff],
        template_overhead: int | None = None,
    ) -> BudgetDecision:
        """Assess diffs against budget: send as-is, truncate, or request chunking."""
        overhead = (
            template_overhead if template_overhead is not None else self.default_template_overhead
        )
        effective_budget = self.context_window - self.reserved_output_tokens - overhead

        if not diffs:
            return BudgetDecision(
                strategy=BudgetStrategy.FITS,
                included_diffs=[],
                omitted_diffs=[],
                total_tokens=0,
                budget_limit=effective_budget,
                reserved_output_tokens=self.reserved_output_tokens,
                template_overhead=overhead,
                model=self.model,
                summary_note="included 0/0 files (empty diff)",
            )

        # 1. Check if the full diff fits
        total_tokens = self.count_diff_tokens(diffs)
        if total_tokens <= effective_budget:
            return BudgetDecision(
                strategy=BudgetStrategy.FITS,
                included_diffs=list(diffs),
                omitted_diffs=[],
                total_tokens=total_tokens,
                budget_limit=effective_budget,
                reserved_output_tokens=self.reserved_output_tokens,
                template_overhead=overhead,
                model=self.model,
                summary_note=f"included {len(diffs)}/{len(diffs)} files",
            )

        # 2. Over budget: sort candidates by descending patch length to drop largest first
        candidates = [d for d in diffs if not d.is_binary and d.patch_text.strip()]
        candidates.sort(key=lambda d: len(d.patch_text), reverse=True)

        current_diffs = [
            FileDiff(
                path=d.path,
                old_path=d.old_path,
                status=d.status,
                additions=d.additions,
                deletions=d.deletions,
                patch_text=d.patch_text,
                is_binary=d.is_binary,
            )
            for d in diffs
        ]
        diff_index = {d.path: d for d in current_diffs}
        omitted: list[FileDiff] = []

        for candidate in candidates:
            note = f"[patch omitted for size: +{candidate.additions}/-{candidate.deletions} lines]"
            if len(candidate.patch_text) <= len(note):
                continue
            omitted.append(candidate)
            target = diff_index[candidate.path]
            target.patch_text = note
            current_tokens = self.count_diff_tokens(current_diffs)
            if current_tokens <= effective_budget:
                included_count = len(diffs) - len(omitted)
                return BudgetDecision(
                    strategy=BudgetStrategy.TRUNCATED,
                    included_diffs=current_diffs,
                    omitted_diffs=omitted,
                    total_tokens=current_tokens,
                    budget_limit=effective_budget,
                    reserved_output_tokens=self.reserved_output_tokens,
                    template_overhead=overhead,
                    model=self.model,
                    summary_note=(
                        f"included {included_count}/{len(diffs)} files, truncated {len(omitted)}"
                    ),
                )

        # 3. Even with all candidates truncated, remaining metadata exceeds budget
        final_tokens = self.count_diff_tokens(current_diffs)
        if omitted:
            trunc_info = f" even with all {len(omitted)} files truncated"
        else:
            trunc_info = ""
        return BudgetDecision(
            strategy=BudgetStrategy.NEEDS_CHUNKING,
            included_diffs=current_diffs,
            omitted_diffs=omitted,
            total_tokens=final_tokens,
            budget_limit=effective_budget,
            reserved_output_tokens=self.reserved_output_tokens,
            template_overhead=overhead,
            model=self.model,
            summary_note=(
                f"diff exceeded budget ({final_tokens} > {effective_budget} tokens)"
                f"{trunc_info}; requires chunking"
            ),
        )
