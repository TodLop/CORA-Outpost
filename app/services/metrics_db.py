# app/services/metrics_db.py
"""
SQLite metrics database for Minecraft server performance monitoring.

Tiered storage with automatic downsampling:
- metrics_raw: 3s samples, retained 24h
- metrics_1m: 1-minute averages, retained 7d
- metrics_5m: 5-minute averages, retained 30d
- metrics_1h: 1-hour averages, retained 1yr
- disk_size: periodic disk usage snapshots
"""

import sqlite3
import logging
import time
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

from app.core.config import METRICS_DB_PATH

logger = logging.getLogger(__name__)

# Retention periods in seconds
RETENTION = {
    "metrics_raw": 24 * 3600,       # 24 hours
    "metrics_1m": 7 * 24 * 3600,    # 7 days
    "metrics_5m": 30 * 24 * 3600,   # 30 days
    "metrics_1h": 365 * 24 * 3600,  # 1 year
}

# Nominal sample interval per table (seconds)
_TABLE_RESOLUTION_SECONDS = {
    "metrics_raw": 3,
    "metrics_1m": 60,
    "metrics_5m": 300,
    "metrics_1h": 3600,
}

_METRICS_TABLES = tuple(_TABLE_RESOLUTION_SECONDS.keys())

# Auto-select thresholds (seconds)
_TABLE_THRESHOLDS = [
    (6 * 3600, "metrics_raw"),        # <= 6h → raw
    (2 * 24 * 3600, "metrics_1m"),    # <= 2d → 1min
    (14 * 24 * 3600, "metrics_5m"),   # <= 14d → 5min
    (float("inf"), "metrics_1h"),     # else → 1hr
]

_METRICS_SCHEMA_COLUMNS = (
    ("timestamp", "REAL NOT NULL DEFAULT 0"),
    ("cpu_percent", "REAL NOT NULL DEFAULT 0"),
    ("cpu_max", "REAL NOT NULL DEFAULT 0"),
    ("ram_mb", "REAL NOT NULL DEFAULT 0"),
    ("ram_max", "REAL NOT NULL DEFAULT 0"),
    ("players", "INTEGER NOT NULL DEFAULT 0"),
    ("tps", "REAL"),
    ("tps_max", "REAL"),
    ("mspt", "REAL"),
    ("mspt_max", "REAL"),
    ("sample_count", "INTEGER NOT NULL DEFAULT 1"),
)

_DISK_SCHEMA_COLUMNS = (
    ("timestamp", "REAL NOT NULL DEFAULT 0"),
    ("size_mb", "REAL NOT NULL DEFAULT 0"),
)


@contextmanager
def _connect():
    """Thread-safe connection with WAL mode."""
    conn = sqlite3.connect(str(METRICS_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_table_schema(conn: sqlite3.Connection, table: str, schema_columns: tuple):
    """Create table if missing and add missing columns for safe schema evolution."""
    columns_sql = ",\n                    ".join(
        f"{name} {definition}" for name, definition in schema_columns
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {columns_sql}
        )
    """)

    existing_columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in schema_columns:
        if name not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            logger.info("Added missing column %s to %s", name, table)


def init_db():
    """Create tables and indexes if they don't exist."""
    METRICS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as conn:
        for table in _METRICS_TABLES:
            _ensure_table_schema(conn, table, _METRICS_SCHEMA_COLUMNS)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table}(timestamp)
            """)

        _ensure_table_schema(conn, "disk_size", _DISK_SCHEMA_COLUMNS)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_disk_ts ON disk_size(timestamp)")

    logger.info("Metrics database initialized at %s", METRICS_DB_PATH)


def insert_raw_metric(cpu_percent: float, ram_mb: float, players: int = 0,
                      tps: Optional[float] = None, mspt: Optional[float] = None):
    """Insert a single raw metric sample."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO metrics_raw (timestamp, cpu_percent, cpu_max, ram_mb, ram_max, players, tps, tps_max, mspt, mspt_max) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, cpu_percent, cpu_percent, ram_mb, ram_mb, players, tps, tps, mspt, mspt)
        )


def insert_disk_size(size_mb: float):
    """Insert a disk size measurement."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO disk_size (timestamp, size_mb) VALUES (?, ?)",
            (now, size_mb)
        )


def downsample():
    """
    Aggregate raw data into coarser tiers and purge expired data.

    Called periodically (every hour) by the metrics collector.
    """
    now = time.time()

    # Downsampling config: (source, target, bucket_seconds)
    aggregations = [
        ("metrics_raw", "metrics_1m", 60),
        ("metrics_1m", "metrics_5m", 300),
        ("metrics_5m", "metrics_1h", 3600),
    ]

    with _connect() as conn:
        for source, target, bucket_sec in aggregations:
            # Find the latest timestamp already in the target table
            row = conn.execute(f"SELECT MAX(timestamp) as max_ts FROM {target}").fetchone()
            last_ts = row["max_ts"] or 0.0

            # Aggregate from source in buckets. Filter by bucket_ts (not raw timestamp)
            # to avoid reinserting an already-aggregated boundary bucket.
            rows = conn.execute(f"""
                SELECT
                    CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                    AVG(cpu_percent) AS cpu_avg,
                    MAX(cpu_max) AS cpu_max,
                    AVG(ram_mb) AS ram_avg,
                    MAX(ram_max) AS ram_max,
                    CAST(AVG(players) AS INTEGER) AS players_avg,
                    AVG(tps) AS tps_avg,
                    MAX(tps_max) AS tps_max,
                    AVG(mspt) AS mspt_avg,
                    MAX(mspt_max) AS mspt_max,
                    COUNT(*) AS sample_count
                FROM {source}
                WHERE timestamp >= ?
                GROUP BY bucket_ts
                HAVING bucket_ts > ? AND bucket_ts < ?
            """, (bucket_sec, bucket_sec, last_ts, last_ts, now - bucket_sec)).fetchall()

            if rows:
                conn.executemany(
                    f"INSERT INTO {target} (timestamp, cpu_percent, cpu_max, ram_mb, ram_max, players, tps, tps_max, mspt, mspt_max, sample_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(r["bucket_ts"], r["cpu_avg"], r["cpu_max"], r["ram_avg"], r["ram_max"], r["players_avg"],
                      r["tps_avg"], r["tps_max"], r["mspt_avg"], r["mspt_max"], r["sample_count"]) for r in rows]
                )
                logger.debug("Downsampled %d rows from %s to %s", len(rows), source, target)

        # Purge expired data from each tier
        for table, retention_sec in RETENTION.items():
            cutoff = now - retention_sec
            result = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
            if result.rowcount > 0:
                logger.debug("Purged %d expired rows from %s", result.rowcount, table)


def _select_table(time_range_sec: float) -> str:
    """Pick the appropriate table based on requested time range."""
    for threshold, table in _TABLE_THRESHOLDS:
        if time_range_sec <= threshold:
            return table
    return "metrics_1h"


def _query_metrics_from_table(table: str, start: float, end: float) -> List[Dict[str, Any]]:
    """Query metrics rows from a specific table."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT timestamp, cpu_percent, cpu_max, ram_mb, ram_max, players, tps, mspt "
            f"FROM {table} WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (start, end)
        ).fetchall()

    return [dict(r) for r in rows]


def _query_tps_mspt_from_table(table: str, start: float, end: float) -> List[Dict[str, Any]]:
    """Query only non-null TPS/MSPT points from a specific table."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT timestamp, tps, mspt FROM {table} "
            f"WHERE timestamp BETWEEN ? AND ? "
            f"AND (tps IS NOT NULL OR mspt IS NOT NULL) "
            f"ORDER BY timestamp",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def _query_latest_tps_mspt_before(end: float) -> Dict[str, Any] | None:
    """Find latest non-null TPS/MSPT point across all metric tiers up to `end`."""
    with _connect() as conn:
        candidates: list[tuple[float, str, Dict[str, Any]]] = []
        for table in _METRICS_TABLES:
            row = conn.execute(
                f"SELECT timestamp, tps, mspt FROM {table} "
                f"WHERE timestamp <= ? AND (tps IS NOT NULL OR mspt IS NOT NULL) "
                f"ORDER BY timestamp DESC LIMIT 1",
                (end,),
            ).fetchone()
            if row:
                as_dict = dict(row)
                candidates.append((float(as_dict["timestamp"]), table, as_dict))
        if not candidates:
            return None

        ts, table, point = max(candidates, key=lambda item: item[0])
        point["source_table"] = table
        return point


def query_metrics_with_metadata(start: float, end: float) -> Dict[str, Any]:
    """
    Query metrics plus selected source metadata for the time range.

    Returns dict with keys: metrics, table, resolution_seconds.
    """
    time_range = max(0.0, end - start)
    table = _select_table(time_range)
    metrics = _query_metrics_from_table(table, start, end)
    return {
        "metrics": metrics,
        "table": table,
        "resolution_seconds": _TABLE_RESOLUTION_SECONDS.get(table),
    }


def query_tps_mspt_with_metadata(start: float, end: float) -> Dict[str, Any]:
    """
    Query TPS/MSPT points with table fallback when selected tier is sparse.

    Returns dict with keys: metrics, table, resolution_seconds.
    """
    time_range = max(0.0, end - start)
    preferred_table = _select_table(time_range)
    fallback_tables = [table for table in _METRICS_TABLES if table != preferred_table]
    table_order = [preferred_table] + fallback_tables

    for table in table_order:
        points = _query_tps_mspt_from_table(table, start, end)
        if points:
            return {
                "metrics": points,
                "table": table,
                "resolution_seconds": _TABLE_RESOLUTION_SECONDS.get(table),
            }

    latest = _query_latest_tps_mspt_before(end)
    if latest:
        source_table = latest.pop("source_table", preferred_table)
        return {
            "metrics": [latest],
            "table": source_table,
            "resolution_seconds": _TABLE_RESOLUTION_SECONDS.get(source_table),
        }

    return {
        "metrics": [],
        "table": preferred_table,
        "resolution_seconds": _TABLE_RESOLUTION_SECONDS.get(preferred_table),
    }


def query_metrics(start: float, end: float) -> List[Dict[str, Any]]:
    """
    Query metrics for a time range, auto-selecting the best resolution table.

    Returns list of dicts with: timestamp, cpu_percent, cpu_max, ram_mb, ram_max, players
    """
    return query_metrics_with_metadata(start, end)["metrics"]


def query_disk_size(start: float, end: float) -> List[Dict[str, Any]]:
    """Query disk size measurements for a time range."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT timestamp, size_mb FROM disk_size "
            "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (start, end)
        ).fetchall()

    return [dict(r) for r in rows]


def get_latest_metric() -> Optional[Dict[str, Any]]:
    """Get the most recent raw metric."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT timestamp, cpu_percent, ram_mb, players, tps, mspt "
            "FROM metrics_raw ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_latest_disk_size() -> Optional[float]:
    """Get the most recent disk size in MB."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT size_mb FROM disk_size ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return row["size_mb"] if row else None
