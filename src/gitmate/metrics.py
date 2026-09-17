"""Per-generation telemetry recording to SQLite."""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("gitmate.metrics")

APP_NAME = "gitmate"
DEFAULT_DB_FILENAME = "metrics.db"
METRICS_DIR_ENV_VAR = "GITMATE_METRICS_DIR"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS invocations (
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
CREATE INDEX IF NOT EXISTS idx_invocations_timestamp ON invocations(timestamp);
CREATE INDEX IF NOT EXISTS idx_invocations_command ON invocations(command);
"""


@dataclass(frozen=True)
class InvocationRecord:
    """A recorded telemetry row representing an LLM invocation or fallback."""

    id: int
    timestamp: str
    command: str
    model: str
    tokens_in: int | None
    tokens_out: int | None
    cache_hit: bool
    latency_ms: int | None
    fallback_used: bool


def get_metrics_db_path(db_path: Path | str | None = None) -> Path:
    """Resolve the SQLite metrics database file path.

    Order of precedence:
    1. Explicit db_path argument (if provided; appends metrics.db if path is a directory).
    2. GITMATE_METRICS_DIR environment variable directory.
    3. XDG_DATA_HOME environment variable / gitmate / metrics.db.
    4. Default: ~/.local/share/gitmate/metrics.db.
    """
    if db_path is not None:
        p = Path(db_path).expanduser()
        if p.is_dir():
            return p / DEFAULT_DB_FILENAME
        return p

    override_dir = os.environ.get(METRICS_DIR_ENV_VAR)
    if override_dir:
        return Path(override_dir).expanduser() / DEFAULT_DB_FILENAME

    xdg_data = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return base / APP_NAME / DEFAULT_DB_FILENAME


def init_db(db_path: Path | str | None = None) -> Path:
    """Initialize the metrics database and invocations table if not present.

    Returns the resolved database path.
    """
    target = get_metrics_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0)
    with contextlib.closing(conn):
        conn.executescript(CREATE_TABLE_SQL)
    return target


def record_invocation(
    command: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_hit: bool,
    latency_ms: int | None,
    fallback_used: bool,
    db_path: Path | str | None = None,
    timestamp: str | None = None,
) -> None:
    """Record an invocation into SQLite database at db_path or default.

    Non-blocking: write errors or filesystem issues are logged as warnings
    and never raise exceptions to disrupt git workflow.
    """
    try:
        target = get_metrics_db_path(db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ts = timestamp if timestamp is not None else datetime.now(UTC).isoformat()

        conn = sqlite3.connect(target, timeout=5.0)
        with contextlib.closing(conn):
            conn.executescript(CREATE_TABLE_SQL)
            with conn:
                conn.execute(
                    """
                    INSERT INTO invocations (
                        timestamp, command, model, tokens_in, tokens_out,
                        cache_hit, latency_ms, fallback_used
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        command,
                        model,
                        tokens_in,
                        tokens_out,
                        1 if cache_hit else 0,
                        latency_ms,
                        1 if fallback_used else 0,
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record invocation metrics: %s", exc)


def get_invocations(
    db_path: Path | str | None = None,
    limit: int | None = None,
) -> list[InvocationRecord]:
    """Retrieve recorded invocations from the metrics database.

    Returns an empty list if the database file does not exist or errors occur.
    """
    try:
        target = get_metrics_db_path(db_path)
        if not target.exists():
            return []
        conn = sqlite3.connect(target, timeout=5.0)
        with contextlib.closing(conn):
            conn.row_factory = sqlite3.Row
            query = (
                "SELECT id, timestamp, command, model, tokens_in, tokens_out, "
                "cache_hit, latency_ms, fallback_used FROM invocations ORDER BY id ASC"
            )
            if limit is not None:
                query += f" LIMIT {int(limit)}"
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            return [
                InvocationRecord(
                    id=row["id"],
                    timestamp=row["timestamp"],
                    command=row["command"],
                    model=row["model"],
                    tokens_in=row["tokens_in"],
                    tokens_out=row["tokens_out"],
                    cache_hit=bool(row["cache_hit"]),
                    latency_ms=row["latency_ms"],
                    fallback_used=bool(row["fallback_used"]),
                )
                for row in rows
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to retrieve invocations from metrics DB: %s", exc)
        return []


def clear_metrics(db_path: Path | str | None = None) -> None:
    """Remove all records from invocations table (helper for tests)."""
    try:
        target = get_metrics_db_path(db_path)
        if not target.exists():
            return
        conn = sqlite3.connect(target, timeout=5.0)
        with contextlib.closing(conn), conn:
            conn.execute("DELETE FROM invocations")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to clear metrics: %s", exc)
