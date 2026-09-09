"""Token budget manager and token counting abstractions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import PurePosixPath
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
    """Outcome of assessing a diff list against the token budget.

    Aliasing contract: entries alias the caller's FileDiff objects, except
    truncated replacements, which are fresh copies. In particular
    ``omitted_diffs`` holds the originals (full patch preserved).
    ``included_diffs`` always carries every input file; truncated ones appear
    as one-line note placeholders, so ``len(included_diffs)`` is the file
    count while the note counts full patches versus truncations.
    """

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


_LOW_SIGNAL_SUFFIXES = (".snap", ".snapshot", ".min.js", ".bundle.js", ".map")
_TEST_DIR_NAMES = frozenset({"test", "tests"})


def _is_low_signal(path: str) -> bool:
    """True for files whose full patch is least informative if dropped.

    Test snapshots, test files, and generated bundles carry the lowest
    signal-to-noise for a commit message, so their patches go first when
    truncating. Char length stays the cost proxy inside and outside this
    group: exact per-file token counts would cost one paid API call each.
    """
    posix = PurePosixPath(path)
    name = posix.name
    if name.endswith(_LOW_SIGNAL_SUFFIXES):
        return True
    if _TEST_DIR_NAMES.intersection(posix.parts):
        return True
    if name.startswith("test_") or "_test." in name or ".test." in name:
        return True
    if ".spec." in name or ".generated." in name:
        return True
    stem = name.rsplit(".", 1)[0]
    return stem.endswith(("_generated", ".generated"))


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
        if effective_budget <= 0:
            raise ValueError(
                f"token budget is not positive ({effective_budget}); "
                "context_window must exceed reserved_output_tokens + template_overhead."
            )

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

        # 2. Over budget: drop low-signal patches first (snapshots, tests,
        # generated code), largest first inside and outside that group.
        candidates = [d for d in diffs if not d.is_binary and d.patch_text.strip()]
        candidates.sort(key=lambda d: (not _is_low_signal(d.path), -len(d.patch_text)))

        current_diffs = [replace(d) for d in diffs]
        working = {id(orig): copy for orig, copy in zip(diffs, current_diffs)}
        omitted: list[FileDiff] = []
        current_tokens = total_tokens

        for candidate in candidates:
            note = f"[patch omitted for size: +{candidate.additions}/-{candidate.deletions} lines]"
            if len(candidate.patch_text) <= len(note):
                continue
            omitted.append(candidate)
            working[id(candidate)].patch_text = note
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

        # 3. Even with all candidates truncated, remaining metadata exceeds budget.
        # current_tokens already holds the last recount (or the full count when
        # nothing was truncated), so no extra paid count call is needed here.
        if omitted:
            trunc_info = f" even with all {len(omitted)} files truncated"
        else:
            trunc_info = ""
        return BudgetDecision(
            strategy=BudgetStrategy.NEEDS_CHUNKING,
            included_diffs=current_diffs,
            omitted_diffs=omitted,
            total_tokens=current_tokens,
            budget_limit=effective_budget,
            reserved_output_tokens=self.reserved_output_tokens,
            template_overhead=overhead,
            model=self.model,
            summary_note=(
                f"diff exceeded budget ({current_tokens} > {effective_budget} tokens)"
                f"{trunc_info}; requires chunking"
            ),
        )
