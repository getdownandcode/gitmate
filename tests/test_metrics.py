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
    assert "T" in rec.timestamp  # ISO 8601


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
