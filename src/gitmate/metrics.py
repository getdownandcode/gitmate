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

#: Model pricing in USD per 1,000,000 tokens (input_rate, output_rate).
#: NOTE: Upstream rates change over time. Verify against https://ai.google.dev/pricing
#: and https://docs.anthropic.com/pricing before relying on this for real financial reports.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-flash-lite": (0.30, 2.50),
    "gemini-3.5-flash": (0.50, 3.00),
    "gemini-3-flash": (0.50, 3.00),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-flash": (0.50, 3.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
}
DEFAULT_PRICING: tuple[float, float] = (0.50, 3.00)

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
    fallback_used INTEGER NOT NULL,
    estimated_cost_usd REAL,
    free_tier INTEGER NOT NULL DEFAULT 0
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
    estimated_cost_usd: float = 0.0
    free_tier: bool | None = None


@dataclass(frozen=True)
class BreakdownStats:
    """Aggregated metrics for a specific subset (e.g. command or model)."""

    invocations: int
    cache_hits: int
    cache_hit_rate: float
    tokens_in: int
    tokens_out: int
    total_tokens: int
    cost_usd: float
    avg_latency_ms: float


@dataclass(frozen=True)
class AggregateStats:
    """Overall aggregated telemetry metrics and category breakdowns."""

    total_invocations: int
    cache_hits: int
    cache_hit_rate: float
    total_tokens_in: int
    total_tokens_out: int
    total_tokens: int
    total_cost_usd: float
    avg_latency_ms: float
    avg_latency_hit_ms: float
    avg_latency_miss_ms: float
    fallback_count: int
    by_command: dict[str, BreakdownStats]
    by_model: dict[str, BreakdownStats]


def calculate_cost(
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_hit: bool = False,
    fallback_used: bool = False,
    free_tier: bool = False,
) -> float:
    """Calculate estimated cost in USD based on model pricing table.

    Cache hits, template fallbacks, and free-tier Gemini usage consume zero billed API tokens ($0.00).
    """
    if cache_hit or fallback_used or (free_tier and model.startswith("gemini")):
        return 0.0
    in_tokens = tokens_in or 0
    out_tokens = tokens_out or 0
    if in_tokens == 0 and out_tokens == 0:
        return 0.0

    if model not in MODEL_PRICING:
        logger.warning("Unknown model '%s'; estimated cost recorded as $0.0", model)
        return 0.0

    in_rate, out_rate = MODEL_PRICING[model]
    cost = (in_tokens / 1_000_000.0 * in_rate) + (out_tokens / 1_000_000.0 * out_rate)
    return round(cost, 6)


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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure table exists and perform non-destructive schema migrations."""
    conn.executescript(CREATE_TABLE_SQL)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(invocations)")
    columns = {row[1] for row in cursor.fetchall()}
    if "estimated_cost_usd" not in columns:
        conn.execute("ALTER TABLE invocations ADD COLUMN estimated_cost_usd REAL")
    if "free_tier" not in columns:
        # Old rows are unknown: free-tier usage was not recorded, so don't guess.
        conn.execute("ALTER TABLE invocations ADD COLUMN free_tier INTEGER")


def init_db(db_path: Path | str | None = None) -> Path:
    """Initialize the metrics database and ensure schema is up-to-date."""
    target = get_metrics_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0)
    with contextlib.closing(conn), conn:
        _ensure_schema(conn)
    return target


def record_invocation(
    command: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_hit: bool,
    latency_ms: int | None,
    fallback_used: bool,
    estimated_cost_usd: float | None = None,
    db_path: Path | str | None = None,
    timestamp: str | None = None,
    free_tier: bool = False,
) -> None:
    """Record an invocation into SQLite database at db_path or default.

    Non-blocking: write errors or filesystem issues are logged as warnings
    and never raise exceptions to disrupt git workflow.
    """
    try:
        target = get_metrics_db_path(db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ts = timestamp if timestamp is not None else datetime.now(UTC).isoformat()
        cost = (
            estimated_cost_usd
            if estimated_cost_usd is not None
            else calculate_cost(
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_hit=cache_hit,
                fallback_used=fallback_used,
                free_tier=free_tier,
            )
        )

        conn = sqlite3.connect(target, timeout=5.0)
        with contextlib.closing(conn):
            _ensure_schema(conn)
            with conn:
                conn.execute(
                    """
                    INSERT INTO invocations (
                        timestamp, command, model, tokens_in, tokens_out,
                        cache_hit, latency_ms, fallback_used, estimated_cost_usd, free_tier
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        cost,
                        1 if free_tier else 0,
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record invocation metrics: %s", exc)


def get_invocations(
    db_path: Path | str | None = None,
    limit: int | None = None,
) -> list[InvocationRecord]:
    """Retrieve recorded invocations from the metrics database."""
    try:
        target = get_metrics_db_path(db_path)
        if not target.exists():
            return []
        conn = sqlite3.connect(target, timeout=5.0)
        with contextlib.closing(conn):
            _ensure_schema(conn)
            conn.row_factory = sqlite3.Row
            query = (
                "SELECT id, timestamp, command, model, tokens_in, tokens_out, "
                "cache_hit, latency_ms, fallback_used, "
                "estimated_cost_usd, free_tier "
                "FROM invocations ORDER BY id ASC"
            )
            if limit is not None:
                query += f" LIMIT {int(limit)}"
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            records: list[InvocationRecord] = []
            for row in rows:
                stored_cost = row["estimated_cost_usd"]
                raw_cost = float(stored_cost or 0.0)
                free_tier = None if row["free_tier"] is None else bool(row["free_tier"])
                # Reprice only known non-free rows, or rows predating cost persistence.
                if (
                    raw_cost == 0.0
                    and (free_tier is False or (free_tier is None and stored_cost is None))
                    and not bool(row["cache_hit"])
                    and not bool(row["fallback_used"])
                    and ((row["tokens_in"] or 0) > 0 or (row["tokens_out"] or 0) > 0)
                ):
                    raw_cost = calculate_cost(
                        model=row["model"],
                        tokens_in=row["tokens_in"],
                        tokens_out=row["tokens_out"],
                    )

                records.append(
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
                        estimated_cost_usd=raw_cost,
                        free_tier=free_tier,
                    )
                )
            return records
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to retrieve invocations from metrics DB: %s", exc)
        return []


def get_monthly_spend(
    year: int | None = None,
    month: int | None = None,
    db_path: Path | str | None = None,
) -> float:
    """Calculate total estimated spend in USD for a given calendar month.

    Defaults to current UTC year and month if not specified.
    """
    now = datetime.now(UTC)
    y = year if year is not None else now.year
    m = month if month is not None else now.month
    prefix = f"{y:04d}-{m:02d}"

    records = get_invocations(db_path=db_path)
    month_spend = sum(r.estimated_cost_usd for r in records if r.timestamp.startswith(prefix))
    return round(month_spend, 6)


def _build_breakdown(records: list[InvocationRecord]) -> BreakdownStats:
    """Build BreakdownStats from a list of InvocationRecords."""
    total = len(records)
    hits = sum(1 for r in records if r.cache_hit)
    hit_rate = (hits / total * 100.0) if total > 0 else 0.0
    t_in = sum(r.tokens_in or 0 for r in records)
    t_out = sum(r.tokens_out or 0 for r in records)
    cost = sum(r.estimated_cost_usd for r in records)
    valid_latencies = [r.latency_ms for r in records if r.latency_ms is not None]
    avg_lat = sum(valid_latencies) / len(valid_latencies) if valid_latencies else 0.0

    return BreakdownStats(
        invocations=total,
        cache_hits=hits,
        cache_hit_rate=round(hit_rate, 1),
        tokens_in=t_in,
        tokens_out=t_out,
        total_tokens=t_in + t_out,
        cost_usd=round(cost, 6),
        avg_latency_ms=round(avg_lat, 1),
    )


def get_aggregate_stats(
    since: datetime | None = None,
    command: str | None = None,
    db_path: Path | str | None = None,
) -> AggregateStats:
    """Compute aggregated metrics and breakdowns across invocations."""
    records = get_invocations(db_path=db_path)

    if since is not None:
        since_iso = since.isoformat()
        records = [r for r in records if r.timestamp >= since_iso]

    if command is not None:
        records = [r for r in records if r.command == command]

    if not records:
        return AggregateStats(
            total_invocations=0,
            cache_hits=0,
            cache_hit_rate=0.0,
            total_tokens_in=0,
            total_tokens_out=0,
            total_tokens=0,
            total_cost_usd=0.0,
            avg_latency_ms=0.0,
            avg_latency_hit_ms=0.0,
            avg_latency_miss_ms=0.0,
            fallback_count=0,
            by_command={},
            by_model={},
        )

    total_invocations = len(records)
    cache_hits = sum(1 for r in records if r.cache_hit)
    cache_hit_rate = round(cache_hits / total_invocations * 100.0, 1)
    total_tokens_in = sum(r.tokens_in or 0 for r in records)
    total_tokens_out = sum(r.tokens_out or 0 for r in records)
    total_tokens = total_tokens_in + total_tokens_out
    total_cost = round(sum(r.estimated_cost_usd for r in records), 6)
    fallback_count = sum(1 for r in records if r.fallback_used)

    valid_latencies = [r.latency_ms for r in records if r.latency_ms is not None]
    avg_latency = round(sum(valid_latencies) / len(valid_latencies), 1) if valid_latencies else 0.0

    hit_latencies = [r.latency_ms for r in records if r.cache_hit and r.latency_ms is not None]
    avg_hit_lat = round(sum(hit_latencies) / len(hit_latencies), 1) if hit_latencies else 0.0

    miss_latencies = [r.latency_ms for r in records if not r.cache_hit and r.latency_ms is not None]
    avg_miss_lat = round(sum(miss_latencies) / len(miss_latencies), 1) if miss_latencies else 0.0

    # Group by command
    by_command: dict[str, list[InvocationRecord]] = {}
    for r in records:
        by_command.setdefault(r.command, []).append(r)
    cmd_breakdown = {cmd: _build_breakdown(recs) for cmd, recs in by_command.items()}

    # Group by model
    by_model: dict[str, list[InvocationRecord]] = {}
    for r in records:
        by_model.setdefault(r.model, []).append(r)
    model_breakdown = {m: _build_breakdown(recs) for m, recs in by_model.items()}

    return AggregateStats(
        total_invocations=total_invocations,
        cache_hits=cache_hits,
        cache_hit_rate=cache_hit_rate,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        avg_latency_ms=avg_latency,
        avg_latency_hit_ms=avg_hit_lat,
        avg_latency_miss_ms=avg_miss_lat,
        fallback_count=fallback_count,
        by_command=cmd_breakdown,
        by_model=model_breakdown,
    )


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
