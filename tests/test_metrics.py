"""Tests for SQLite telemetry recording, schema creation, and error suppression."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from gitmate.metrics import (
    clear_metrics,
    get_invocations,
    get_metrics_db_path,
    init_db,
    record_invocation,
)


@pytest.fixture
def metrics_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate metrics database in a temporary directory."""
    m_dir = tmp_path / "metrics"
    m_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GITMATE_METRICS_DIR", str(m_dir))
    return m_dir


def test_metrics_db_path_resolution(metrics_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. GITMATE_METRICS_DIR override
    assert get_metrics_db_path() == metrics_dir / "metrics.db"

    # 2. Explicit argument directory
    custom_dir = metrics_dir / "sub"
    custom_dir.mkdir()
    assert get_metrics_db_path(custom_dir) == custom_dir / "metrics.db"

    # 3. Explicit argument file
    custom_file = metrics_dir / "direct.sqlite"
    assert get_metrics_db_path(custom_file) == custom_file

    # 4. XDG_DATA_HOME fallback
    monkeypatch.delenv("GITMATE_METRICS_DIR")
    xdg_dir = metrics_dir / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_dir))
    assert get_metrics_db_path() == xdg_dir / "gitmate" / "metrics.db"


def test_init_db_creates_schema_and_indices(metrics_dir: Path) -> None:
    db_file = init_db()
    assert db_file.exists()

    conn = sqlite3.connect(db_file)
    try:
        # Verify table exists
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='invocations'")
        assert cursor.fetchone() is not None

        # Verify columns
        cursor.execute("PRAGMA table_info(invocations)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        assert "id" in columns
        assert "timestamp" in columns
        assert "command" in columns
        assert "model" in columns
        assert "tokens_in" in columns
        assert "tokens_out" in columns
        assert "cache_hit" in columns
        assert "latency_ms" in columns
        assert "fallback_used" in columns

        # Verify indices
        cursor.execute("PRAGMA index_list(invocations)")
        indices = {row[1] for row in cursor.fetchall()}
        assert "idx_invocations_timestamp" in indices
        assert "idx_invocations_command" in indices
    finally:
        conn.close()


def test_record_invocation_and_retrieve_all_fields(metrics_dir: Path) -> None:
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=150,
        tokens_out=30,
        cache_hit=False,
        latency_ms=450,
        fallback_used=False,
    )

    records = get_invocations()
    assert len(records) == 1
    rec = records[0]
    assert rec.command == "commit"
    assert rec.model == "gemini-3.5-flash-lite"
    assert rec.tokens_in == 150
    assert rec.tokens_out == 30
    assert rec.cache_hit is False
    assert rec.latency_ms == 450
    assert rec.fallback_used is False
    assert rec.free_tier is False
    assert "T" in rec.timestamp  # ISO 8601


def test_free_tier_is_persisted_and_not_repriced(metrics_dir: Path) -> None:
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=1_000_000,
        tokens_out=1_000_000,
        cache_hit=False,
        latency_ms=200,
        fallback_used=False,
        free_tier=True,
    )

    [record] = get_invocations()
    assert record.free_tier is True
    assert record.estimated_cost_usd == 0.0


def test_record_invocation_boolean_mappings(metrics_dir: Path) -> None:
    # Cache hit
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=0,
        tokens_out=0,
        cache_hit=True,
        latency_ms=10,
        fallback_used=False,
    )
    # Fallback used
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=None,
        tokens_out=None,
        cache_hit=False,
        latency_ms=5,
        fallback_used=True,
    )

    records = get_invocations()
    assert len(records) == 2
    assert records[0].cache_hit is True
    assert records[0].fallback_used is False
    assert records[1].cache_hit is False
    assert records[1].fallback_used is True


def test_record_invocation_nullable_vs_zero(metrics_dir: Path) -> None:
    # Null values
    record_invocation(
        command="commit",
        model="test-model",
        tokens_in=None,
        tokens_out=None,
        cache_hit=False,
        latency_ms=None,
        fallback_used=False,
    )
    # Zero values
    record_invocation(
        command="commit",
        model="test-model",
        tokens_in=0,
        tokens_out=0,
        cache_hit=False,
        latency_ms=0,
        fallback_used=False,
    )

    records = get_invocations()
    assert len(records) == 2
    assert records[0].tokens_in is None
    assert records[0].tokens_out is None
    assert records[0].latency_ms is None

    assert records[1].tokens_in == 0
    assert records[1].tokens_out == 0
    assert records[1].latency_ms == 0


def test_get_invocations_limit_and_clear(metrics_dir: Path) -> None:
    for i in range(5):
        record_invocation(
            command=f"cmd-{i}",
            model="m",
            tokens_in=i,
            tokens_out=i,
            cache_hit=False,
            latency_ms=100,
            fallback_used=False,
        )

    assert len(get_invocations()) == 5
    limited = get_invocations(limit=2)
    assert len(limited) == 2
    assert limited[0].command == "cmd-0"
    assert limited[1].command == "cmd-1"

    clear_metrics()
    assert len(get_invocations()) == 0


def test_record_invocation_suppresses_sqlite_and_io_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 1. Corrupt SQLite DB file (invalid header)
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"NOT_A_SQLITE_DATABASE_HEADER")

    with caplog.at_level(logging.WARNING, logger="gitmate.metrics"):
        # Must not raise
        record_invocation(
            command="commit",
            model="m",
            tokens_in=10,
            tokens_out=10,
            cache_hit=False,
            latency_ms=100,
            fallback_used=False,
            db_path=corrupt_db,
        )
    assert any("Failed to record invocation metrics" in msg for msg in caplog.messages)

    # 2. Non-existent path get_invocations returns empty list without error
    non_existent = tmp_path / "does_not_exist" / "metrics.db"
    assert get_invocations(db_path=non_existent) == []


def test_calculate_cost() -> None:
    from gitmate.metrics import calculate_cost

    # Gemini Flash-Lite: $0.30 / 1M in, $2.50 / 1M out
    # 100_000 in ($0.03) + 10_000 out ($0.025) = $0.055
    cost = calculate_cost(
        model="gemini-3.5-flash-lite",
        tokens_in=100_000,
        tokens_out=10_000,
        cache_hit=False,
        fallback_used=False,
    )
    assert cost == 0.055

    # Gemini 3 Flash / Preview: $0.50 / 1M in, $3.00 / 1M out
    # 100_000 in ($0.05) + 10_000 out ($0.03) = $0.08
    cost_flash = calculate_cost(
        model="gemini-3-flash",
        tokens_in=100_000,
        tokens_out=10_000,
        cache_hit=False,
        fallback_used=False,
    )
    assert cost_flash == 0.08

    # Unknown model warns and returns $0.0
    cost_unknown = calculate_cost(
        model="unknown-custom-model",
        tokens_in=100_000,
        tokens_out=10_000,
    )
    assert cost_unknown == 0.0

    # Cache hit is always $0.0
    assert (
        calculate_cost(
            model="gemini-3.5-flash-lite",
            tokens_in=100_000,
            tokens_out=10_000,
            cache_hit=True,
        )
        == 0.0
    )

    # Fallback template is always $0.0
    assert (
        calculate_cost(
            model="gemini-3.5-flash-lite",
            tokens_in=100_000,
            tokens_out=10_000,
            fallback_used=True,
        )
        == 0.0
    )

    # Zero tokens is $0.0
    assert calculate_cost("gemini-3.5-flash-lite", 0, 0) == 0.0
    assert calculate_cost("gemini-3.5-flash-lite", None, None) == 0.0

    # Claude 3.5 Sonnet: $3.00 / 1M in, $15.00 / 1M out
    # 10_000 in ($0.03) + 1_000 out ($0.015) = $0.045
    cost_claude = calculate_cost("claude-3-5-sonnet", 10_000, 1_000)
    assert cost_claude == 0.045

    # Free tier: Gemini models calculate as $0.0
    cost_free_gemini = calculate_cost("gemini-3.5-flash-lite", 100_000, 10_000, free_tier=True)
    assert cost_free_gemini == 0.0
    # Free tier: Non-Gemini models remain billed
    cost_free_claude = calculate_cost("claude-3-5-sonnet", 10_000, 1_000, free_tier=True)
    assert cost_free_claude == 0.045


def test_schema_migration_adds_estimated_cost_column(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy_metrics.db"
    # Create Phase 5 legacy schema without estimated_cost_usd
    legacy_sql = """
    CREATE TABLE invocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        command TEXT NOT NULL,
        model TEXT NOT NULL,
        tokens_in INTEGER,
        tokens_out INTEGER,
        cache_hit INTEGER NOT NULL,
        latency_ms INTEGER,
        fallback_used INTEGER NOT NULL
    );
    INSERT INTO invocations (
        timestamp, command, model, tokens_in, tokens_out, cache_hit, latency_ms, fallback_used
    ) VALUES ('2026-09-01T12:00:00Z', 'commit', 'gemini-3.5-flash-lite', 100, 20, 0, 200, 0);
    """
    conn = sqlite3.connect(db_file)
    conn.executescript(legacy_sql)
    conn.close()

    # Now call init_db and get_invocations on the legacy file
    init_db(db_file)
    records = get_invocations(db_file)
    assert len(records) == 1
    # Read-time re-pricing recalculates legacy row: 100 in ($0.00003) + 20 out ($0.00005) = 0.00008
    assert records[0].estimated_cost_usd == 0.00008

    # Record a new invocation on the migrated DB
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=1_000_000,
        tokens_out=1_000_000,
        cache_hit=False,
        latency_ms=300,
        fallback_used=False,
        db_path=db_file,
    )
    records_after = get_invocations(db_file)
    assert len(records_after) == 2
    # Second record has calculated cost: 0.30 + 2.50 = 2.80
    assert records_after[1].estimated_cost_usd == 2.80


def test_migration_does_not_guess_free_tier_for_existing_zero_cost_rows(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "existing_metrics.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE invocations (
            id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, command TEXT NOT NULL,
            model TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER,
            cache_hit INTEGER NOT NULL, latency_ms INTEGER,
            fallback_used INTEGER NOT NULL, estimated_cost_usd REAL
        );
        INSERT INTO invocations VALUES (
            1, '2026-09-01T12:00:00Z', 'commit', 'gemini-3.5-flash-lite',
            1000000, 1000000, 0, 200, 0, 0.0
        );
        """
    )
    conn.close()

    [record] = get_invocations(db_file)
    assert record.free_tier is None
    assert record.estimated_cost_usd == 0.0


def test_get_monthly_spend(metrics_dir: Path) -> None:
    from gitmate.metrics import get_monthly_spend

    # September 2026
    record_invocation(
        command="commit",
        model="m",
        tokens_in=100,
        tokens_out=100,
        cache_hit=False,
        latency_ms=100,
        fallback_used=False,
        estimated_cost_usd=0.05,
        timestamp="2026-09-10T10:00:00Z",
    )
    record_invocation(
        command="commit",
        model="m",
        tokens_in=100,
        tokens_out=100,
        cache_hit=False,
        latency_ms=100,
        fallback_used=False,
        estimated_cost_usd=0.03,
        timestamp="2026-09-15T12:00:00Z",
    )
    # August 2026
    record_invocation(
        command="commit",
        model="m",
        tokens_in=100,
        tokens_out=100,
        cache_hit=False,
        latency_ms=100,
        fallback_used=False,
        estimated_cost_usd=0.20,
        timestamp="2026-08-20T12:00:00Z",
    )

    sep_spend = get_monthly_spend(year=2026, month=9)
    assert sep_spend == 0.08

    aug_spend = get_monthly_spend(year=2026, month=8)
    assert aug_spend == 0.20

    oct_spend = get_monthly_spend(year=2026, month=10)
    assert oct_spend == 0.0


def test_get_aggregate_stats(metrics_dir: Path) -> None:
    from datetime import UTC, datetime

    from gitmate.metrics import get_aggregate_stats

    # Empty DB stats
    empty_stats = get_aggregate_stats()
    assert empty_stats.total_invocations == 0
    assert empty_stats.cache_hit_rate == 0.0
    assert empty_stats.total_cost_usd == 0.0

    # Populate with diverse entries
    # 1. Commit cache miss
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=1000,
        tokens_out=200,
        cache_hit=False,
        latency_ms=500,
        fallback_used=False,
        estimated_cost_usd=0.001,
        timestamp="2026-09-10T10:00:00Z",
    )
    # 2. Commit cache hit
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=1000,
        tokens_out=200,
        cache_hit=True,
        latency_ms=10,
        fallback_used=False,
        estimated_cost_usd=0.0,
        timestamp="2026-09-11T10:00:00Z",
    )
    # 3. Fallback commit
    record_invocation(
        command="commit",
        model="gemini-3.5-flash-lite",
        tokens_in=0,
        tokens_out=0,
        cache_hit=False,
        latency_ms=5,
        fallback_used=True,
        estimated_cost_usd=0.0,
        timestamp="2026-09-12T10:00:00Z",
    )
    # 4. PR summary command
    record_invocation(
        command="pr_summary",
        model="gemini-3.5-flash",
        tokens_in=2000,
        tokens_out=500,
        cache_hit=False,
        latency_ms=800,
        fallback_used=False,
        estimated_cost_usd=0.005,
        timestamp="2026-09-15T10:00:00Z",
    )

    stats = get_aggregate_stats()
    assert stats.total_invocations == 4
    assert stats.cache_hits == 1
    assert stats.cache_hit_rate == 25.0
    assert stats.total_tokens_in == 4000
    assert stats.total_tokens_out == 900
    assert stats.total_tokens == 4900
    assert stats.total_cost_usd == 0.006
    assert stats.fallback_count == 1
    assert stats.avg_latency_hit_ms == 10.0
    assert stats.avg_latency_miss_ms == 435.0  # (500 + 5 + 800) / 3 = 435.0

    # Check command breakdown
    assert "commit" in stats.by_command
    assert stats.by_command["commit"].invocations == 3
    assert stats.by_command["commit"].cache_hits == 1
    assert stats.by_command["commit"].cache_hit_rate == 33.3

    assert "pr_summary" in stats.by_command
    assert stats.by_command["pr_summary"].invocations == 1

    # Check model breakdown
    assert "gemini-3.5-flash-lite" in stats.by_model
    assert "gemini-3.5-flash" in stats.by_model

    # Check since filtering
    since_dt = datetime(2026, 9, 14, tzinfo=UTC)
    filtered = get_aggregate_stats(since=since_dt)
    assert filtered.total_invocations == 1
    assert filtered.by_command["pr_summary"].invocations == 1
