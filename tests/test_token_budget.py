"""Tests for TokenBudgetManager, BudgetDecision, and TokenCounter implementations."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gitmate.diff_extractor import FileDiff
from gitmate.token_budget import (
    BudgetDecision,
    BudgetStrategy,
    GeminiTokenCounter,
    InvalidBudgetError,
    NoApiKeyError,
    TokenBudgetManager,
    TokenCounter,
    TokenCountError,
    format_diff_for_counting,
)


class FakeTokenCounter:
    """In-memory TokenCounter for tests; strictly deterministic and offline."""

    def __init__(
        self,
        char_rate: int = 1,
        fixed_counts: dict[str, int] | None = None,
    ) -> None:
        self.char_rate = char_rate
        self.fixed_counts = fixed_counts or {}
        self.calls: list[tuple[str, str]] = []

    def count_tokens(self, text: str, model: str) -> int:
        self.calls.append((text, model))
        if not text:
            return 0
        if text in self.fixed_counts:
            return self.fixed_counts[text]
        return len(text) * self.char_rate


def _make_diff(
    path: str = "src/main.py",
    old_path: str | None = None,
    status: str = "modified",
    additions: int = 10,
    deletions: int = 5,
    patch_text: str = "@@ -1,5 +1,10 @@\n+line",
    is_binary: bool = False,
) -> FileDiff:
    return FileDiff(
        path=path,
        old_path=old_path,
        status=status,  # type: ignore[arg-type]
        additions=additions,
        deletions=deletions,
        patch_text=patch_text,
        is_binary=is_binary,
    )


# --- format_diff_for_counting ---


def test_format_diff_for_counting_modified() -> None:
    diff = _make_diff(
        path="file.py", status="modified", additions=2, deletions=1, patch_text="+a\n-b"
    )
    formatted = format_diff_for_counting([diff])
    assert "File: file.py (modified, +2/-1)" in formatted
    assert "+a\n-b" in formatted


def test_format_diff_for_counting_renamed() -> None:
    diff = _make_diff(
        path="new.py",
        old_path="old.py",
        status="renamed",
        additions=0,
        deletions=0,
        patch_text="",
    )
    formatted = format_diff_for_counting([diff])
    assert "File: new.py (renamed, +0/-0) (renamed from old.py)" in formatted
    assert "[empty or omitted]" in formatted


def test_format_diff_for_counting_binary() -> None:
    diff = _make_diff(path="image.png", status="added", additions=0, deletions=0, is_binary=True)
    formatted = format_diff_for_counting([diff])
    assert "File: image.png (added, +0/-0)" in formatted
    assert "[binary file, diff omitted]" in formatted


# --- TokenBudgetManager ---


def test_empty_diff_fits() -> None:
    counter = FakeTokenCounter()
    assert isinstance(counter, TokenCounter)
    mgr = TokenBudgetManager(counter=counter, context_window=10_000)
    decision = mgr.assess([])

    assert isinstance(decision, BudgetDecision)
    assert decision.strategy == BudgetStrategy.FITS
    assert decision.included_diffs == []
    assert decision.omitted_diffs == []
    assert decision.total_tokens == 0
    assert decision.budget_limit == 10_000 - 2048 - 500
    assert decision.summary_note == "included 0/0 files (empty diff)"


def test_diff_fits_under_budget() -> None:
    counter = FakeTokenCounter(char_rate=1)
    diff = _make_diff(path="a.py", patch_text="short patch")
    mgr = TokenBudgetManager(counter=counter, context_window=10_000)
    decision = mgr.assess([diff])

    assert decision.strategy == BudgetStrategy.FITS
    assert len(decision.included_diffs) == 1
    assert decision.included_diffs[0].patch_text == "short patch"
    assert decision.omitted_diffs == []
    assert decision.summary_note == "included 1/1 files"


def test_exact_boundary_fits() -> None:
    diff = _make_diff(path="a.py", patch_text="exact")
    formatted = format_diff_for_counting([diff])
    target_tokens = len(formatted)

    # Set context_window so effective_budget == target_tokens
    # effective_budget = context_window - reserved(2048) - overhead(500)
    context_window = target_tokens + 2048 + 500
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=context_window)

    decision = mgr.assess([diff])
    assert decision.strategy == BudgetStrategy.FITS
    assert decision.total_tokens == target_tokens
    assert decision.budget_limit == target_tokens


def test_boundary_plus_one_truncates() -> None:
    diff = _make_diff(path="a.py", patch_text="x" * 200)
    formatted = format_diff_for_counting([diff])
    # effective_budget is 1 token less than full formatted text
    effective_budget = len(formatted) - 1
    context_window = effective_budget + 2048 + 500

    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=context_window)

    decision = mgr.assess([diff])
    assert decision.strategy == BudgetStrategy.TRUNCATED
    assert len(decision.omitted_diffs) == 1
    assert decision.omitted_diffs[0].path == "a.py"
    assert decision.omitted_diffs[0].patch_text == "x" * 200
    assert "[patch omitted for size:" in decision.included_diffs[0].patch_text
    assert decision.summary_note == "included 0/1 files, truncated 1"


def test_truncation_drops_largest_patch_first() -> None:
    # 3 files with different patch sizes: small (50), large (300), medium (150)
    small_diff = _make_diff(path="small.py", patch_text="s" * 50)
    large_diff = _make_diff(path="large.py", patch_text="L" * 300)
    medium_diff = _make_diff(path="med.py", patch_text="m" * 150)

    diffs = [small_diff, large_diff, medium_diff]

    # Calculate token size if large is truncated:
    truncated_large_diff = _make_diff(
        path="large.py",
        patch_text=f"[patch omitted for size: +{large_diff.additions}/-{large_diff.deletions} lines]",
    )
    tokens_with_large_truncated = len(
        format_diff_for_counting([small_diff, truncated_large_diff, medium_diff])
    )

    # Set effective budget exactly to accommodate truncating only the largest
    context_window = tokens_with_large_truncated + 2048 + 500
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=context_window)

    decision = mgr.assess(diffs)

    assert decision.strategy == BudgetStrategy.TRUNCATED
    assert len(decision.omitted_diffs) == 1
    assert decision.omitted_diffs[0].path == "large.py"
    # Verify original patch preserved in omitted_diffs
    assert decision.omitted_diffs[0].patch_text == "L" * 300

    # Verify small and medium patches remain intact
    included_by_path = {d.path: d for d in decision.included_diffs}
    assert included_by_path["small.py"].patch_text == "s" * 50
    assert included_by_path["med.py"].patch_text == "m" * 150
    assert "[patch omitted for size:" in included_by_path["large.py"].patch_text
    assert decision.summary_note == "included 2/3 files, truncated 1"


def test_truncated_diff_preserves_metadata() -> None:
    diff = _make_diff(
        path="src/service.py",
        old_path="src/old_service.py",
        status="renamed",
        additions=42,
        deletions=17,
        patch_text="x" * 500,
        is_binary=False,
    )
    counter = FakeTokenCounter(char_rate=1)
    # Budget fits only the truncated note
    mgr = TokenBudgetManager(counter=counter, context_window=2048 + 500 + 150)
    decision = mgr.assess([diff])

    assert decision.strategy == BudgetStrategy.TRUNCATED
    inc = decision.included_diffs[0]
    assert inc.path == "src/service.py"
    assert inc.old_path == "src/old_service.py"
    assert inc.status == "renamed"
    assert inc.additions == 42
    assert inc.deletions == 17
    assert inc.is_binary is False
    assert inc.patch_text == "[patch omitted for size: +42/-17 lines]"


def test_binary_files_and_empty_patches_not_truncated() -> None:
    bin_diff = _make_diff(path="pic.png", is_binary=True, patch_text="")
    empty_diff = _make_diff(path="empty.txt", patch_text="")
    code_diff = _make_diff(path="code.py", patch_text="c" * 200)

    diffs = [bin_diff, empty_diff, code_diff]

    # Effective budget fits headers + truncated note for code.py
    context_window = 2048 + 500 + 200
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=context_window)

    decision = mgr.assess(diffs)
    assert decision.strategy == BudgetStrategy.TRUNCATED
    # Only code_diff was candidate for truncation
    assert len(decision.omitted_diffs) == 1
    assert decision.omitted_diffs[0].path == "code.py"
    inc_by_path = {d.path: d for d in decision.included_diffs}
    assert inc_by_path["pic.png"].is_binary is True
    assert inc_by_path["empty.txt"].patch_text == ""


def test_extreme_overflow_triggers_needs_chunking() -> None:
    diff1 = _make_diff(path="a.py", patch_text="a" * 100)
    diff2 = _make_diff(path="b.py", patch_text="b" * 100)

    # Set context window smaller than headers + replacement notes
    # So even after dropping all patches, total_tokens > effective_budget
    counter = FakeTokenCounter(char_rate=1)
    # effective_budget = 10 tokens
    mgr = TokenBudgetManager(
        counter=counter,
        context_window=2048 + 500 + 10,
        reserved_output_tokens=2048,
        default_template_overhead=500,
    )

    decision = mgr.assess([diff1, diff2])
    assert decision.strategy == BudgetStrategy.NEEDS_CHUNKING
    assert len(decision.omitted_diffs) == 2
    assert "requires chunking" in decision.summary_note
    # All patches should be replaced with notes in included_diffs
    for d in decision.included_diffs:
        assert "[patch omitted for size:" in d.patch_text


def test_template_overhead_and_reserved_output_deduction() -> None:
    diff = _make_diff(path="a.py", patch_text="hello")
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(
        counter=counter,
        context_window=10_000,
        reserved_output_tokens=3_000,
        default_template_overhead=1_000,
    )

    decision = mgr.assess([diff], template_overhead=1_500)
    assert decision.reserved_output_tokens == 3_000
    assert decision.template_overhead == 1_500
    # effective_budget = 10_000 - 3_000 - 1_500 = 5_500
    assert decision.budget_limit == 5_500


# --- GeminiTokenCounter ---


def test_gemini_token_counter_no_api_key_raises() -> None:
    counter = GeminiTokenCounter(api_key=None, client=None)
    # Empty text returns 0 without raising
    assert counter.count_tokens("", "gemini-flash") == 0

    with pytest.raises(NoApiKeyError, match="Gemini API key is required"):
        counter.count_tokens("some text", "gemini-flash")


def test_gemini_token_counter_with_mock_client() -> None:
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.total_tokens = 77
    mock_client.models.count_tokens.return_value = mock_resp

    counter = GeminiTokenCounter(client=mock_client)
    count = counter.count_tokens("hello world", "gemini-flash")

    assert count == 77
    mock_client.models.count_tokens.assert_called_once_with(
        model="gemini-flash",
        contents="hello world",
    )


def test_gemini_token_counter_missing_total_tokens_attribute() -> None:
    mock_client = MagicMock()
    mock_resp = MagicMock(spec=[])  # no total_tokens attribute
    mock_client.models.count_tokens.return_value = mock_resp

    counter = GeminiTokenCounter(client=mock_client)
    with pytest.raises(TokenCountError, match="did not contain 'total_tokens'"):
        counter.count_tokens("text", "gemini-flash")


def test_gemini_token_counter_client_exception_wrapped() -> None:
    mock_client = MagicMock()
    mock_client.models.count_tokens.side_effect = RuntimeError("API rate limit")

    counter = GeminiTokenCounter(client=mock_client)
    with pytest.raises(
        TokenCountError, match="failed to count tokens via Gemini SDK: API rate limit"
    ):
        counter.count_tokens("text", "gemini-flash")


def test_candidate_shorter_than_replacement_note_not_truncated() -> None:
    # A tiny patch of 3 characters (+x\n) shouldn't be replaced with a 38-char note
    tiny_diff = _make_diff(path="tiny.py", patch_text="+x\n")
    counter = FakeTokenCounter(char_rate=1)
    # Effective budget 10 (less than tiny_diff total formatting)
    mgr = TokenBudgetManager(
        counter=counter,
        context_window=2048 + 500 + 10,
        reserved_output_tokens=2048,
        default_template_overhead=500,
    )

    decision = mgr.assess([tiny_diff])
    # Cannot be truncated since patch is smaller than note -> triggers chunking
    assert decision.strategy == BudgetStrategy.NEEDS_CHUNKING
    assert decision.omitted_diffs == []
    assert decision.included_diffs[0].patch_text == "+x\n"


def test_gemini_token_counter_lazy_client_init(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client_cls = MagicMock()
    mock_instance = MagicMock()
    mock_resp = MagicMock()
    mock_resp.total_tokens = 15
    mock_instance.models.count_tokens.return_value = mock_resp
    mock_client_cls.return_value = mock_instance

    import google.genai

    monkeypatch.setattr(google.genai, "Client", mock_client_cls)

    counter = GeminiTokenCounter(api_key="secret-api-key")
    # Client should not be created yet
    assert counter._client is None

    result = counter.count_tokens("hello", "gemini-flash")
    assert result == 15
    mock_client_cls.assert_called_once_with(api_key="secret-api-key")


def test_non_positive_effective_budget_raises() -> None:
    counter = FakeTokenCounter()
    diff = _make_diff(path="a.py", patch_text="hello")
    # reserved alone exceeds the window
    mgr = TokenBudgetManager(counter=counter, context_window=1000, reserved_output_tokens=2048)
    with pytest.raises(InvalidBudgetError, match="not positive"):
        mgr.assess([diff])
    # per-call overhead can also zero it out
    mgr = TokenBudgetManager(counter=counter, context_window=2548)
    with pytest.raises(InvalidBudgetError, match="not positive"):
        mgr.assess([diff])
    with pytest.raises(InvalidBudgetError, match="not positive"):
        mgr.assess([])


def test_negative_budget_inputs_raise() -> None:
    counter = FakeTokenCounter()
    diff = _make_diff(path="a.py", patch_text="hello")
    with pytest.raises(InvalidBudgetError, match="must not be negative"):
        TokenBudgetManager(counter=counter, reserved_output_tokens=-5)
    with pytest.raises(InvalidBudgetError, match="must not be negative"):
        TokenBudgetManager(counter=counter, default_template_overhead=-5)
    mgr = TokenBudgetManager(counter=counter, context_window=10_000)
    with pytest.raises(InvalidBudgetError, match="must not be negative"):
        mgr.assess([diff], template_overhead=-100)


def test_largest_patch_truncated_first() -> None:
    big = _make_diff(path="src/service.py", patch_text="n" * 300)
    small = _make_diff(path="tests/__snapshots__/app.snap", patch_text="s" * 150)
    truncated_big = _make_diff(
        path="src/service.py",
        patch_text=f"[patch omitted for size: +{big.additions}/-{big.deletions} lines]",
    )
    tokens_with_big_truncated = len(format_diff_for_counting([truncated_big, small]))
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=tokens_with_big_truncated + 2048 + 500)

    decision = mgr.assess([small, big])

    assert decision.strategy == BudgetStrategy.TRUNCATED
    assert [d.path for d in decision.omitted_diffs] == ["src/service.py"]
    by_path = {d.path: d for d in decision.included_diffs}
    assert by_path["tests/__snapshots__/app.snap"].patch_text == "s" * 150


def test_duplicate_paths_each_processed() -> None:
    first = _make_diff(path="dup.py", patch_text="a" * 100)
    second = _make_diff(path="dup.py", patch_text="b" * 100)
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=2048 + 500 + 10)

    decision = mgr.assess([first, second])

    assert decision.strategy == BudgetStrategy.NEEDS_CHUNKING
    assert len(decision.omitted_diffs) == 2
    assert [d.patch_text for d in decision.included_diffs].count(
        "[patch omitted for size: +10/-5 lines]"
    ) == 2
    # Originals keep their patches; working copies are independent objects.
    assert (first.patch_text, second.patch_text) == ("a" * 100, "b" * 100)


def test_chunking_path_makes_no_extra_count_call() -> None:
    diffs = [
        _make_diff(path="a.py", patch_text="a" * 100),
        _make_diff(path="b.py", patch_text="b" * 100),
    ]
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=2048 + 500 + 10)

    decision = mgr.assess(diffs)

    assert decision.strategy == BudgetStrategy.NEEDS_CHUNKING
    # 1 initial count + 1 per truncated file; the final total reuses the last one.
    assert len(counter.calls) == 3


def test_candidates_truncated_strictly_in_descending_size_order() -> None:
    # Three candidate diffs of descending patch size: 400, 250, 100 chars.
    # When budget allows only 1 file intact, the 400 and 250 files are truncated in order.
    big = _make_diff(path="src/big.py", patch_text="b" * 400)
    med = _make_diff(path="src/med.py", patch_text="m" * 250)
    small = _make_diff(path="src/small.py", patch_text="s" * 100)
    note_big = f"[patch omitted for size: +{big.additions}/-{big.deletions} lines]"
    note_med = f"[patch omitted for size: +{med.additions}/-{med.deletions} lines]"
    big_trunc = _make_diff(path="src/big.py", patch_text=note_big)
    med_trunc = _make_diff(path="src/med.py", patch_text=note_med)
    tokens_after_two = len(format_diff_for_counting([big_trunc, med_trunc, small]))
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=tokens_after_two + 2048 + 500)

    decision = mgr.assess([small, med, big])

    assert decision.strategy == BudgetStrategy.TRUNCATED
    # Largest patches dropped first: big (400), then med (250).
    assert [d.path for d in decision.omitted_diffs] == ["src/big.py", "src/med.py"]
    by_path = {d.path: d for d in decision.included_diffs}
    assert by_path["src/small.py"].patch_text == "s" * 100


def test_many_guard_skipped_small_files_need_chunking() -> None:
    # Each patch is shorter than its replacement note, so the guard skips
    # every file — but the headers alone still exceed the budget.
    diffs = [_make_diff(path=f"f{i}.py", patch_text="+x\n") for i in range(3)]
    counter = FakeTokenCounter(char_rate=1)
    mgr = TokenBudgetManager(counter=counter, context_window=2048 + 500 + 10)

    decision = mgr.assess(diffs)

    assert decision.strategy == BudgetStrategy.NEEDS_CHUNKING
    assert decision.omitted_diffs == []
    assert [d.patch_text for d in decision.included_diffs] == ["+x\n"] * 3


def test_mixed_truncated_and_guarded_diffs_trigger_needs_chunking() -> None:
    # 1 large file is truncated, but 2 small files are guard-skipped and remaining
    # diff still exceeds the effective budget.
    big = _make_diff(path="big.py", patch_text="b" * 300)
    small1 = _make_diff(path="s1.py", patch_text="+x\n")
    small2 = _make_diff(path="s2.py", patch_text="+y\n")
    counter = FakeTokenCounter(char_rate=1)
    # Effective budget 10: even with big truncated, remaining tokens exceed 10
    mgr = TokenBudgetManager(counter=counter, context_window=2048 + 500 + 10)

    decision = mgr.assess([big, small1, small2])

    assert decision.strategy == BudgetStrategy.NEEDS_CHUNKING
    assert [d.path for d in decision.omitted_diffs] == ["big.py"]
    # Ratio format: 1 of 3 files truncated, NOT "even with all 1 files truncated"
    assert "even with 1/3 files truncated; requires chunking" in decision.summary_note
