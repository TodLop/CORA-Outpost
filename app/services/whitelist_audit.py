"""
Whitelist inactivity audit service.

Builds a persistent activity index from Minecraft log files so the admin panel
can review whitelist age and last-seen data without rescanning the full log set
on every request.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.core.config import DATA_DIR, MINECRAFT_SERVER_PATH, PROTECTED_PLAYERS
from app.services import minecraft_settings

logger = logging.getLogger(__name__)

WHITELIST_AUDIT_DB_PATH = DATA_DIR / "whitelist_activity.db"
AUDIT_DB_PATH = WHITELIST_AUDIT_DB_PATH
LOGS_DIR = MINECRAFT_SERVER_PATH / "logs"
WHITELIST_JSON_PATH = MINECRAFT_SERVER_PATH / "whitelist.json"
WHITELIST_PATH = WHITELIST_JSON_PATH
USERCACHE_PATH = MINECRAFT_SERVER_PATH / "usercache.json"
_DEFAULT_LOGS_DIR = LOGS_DIR
_DEFAULT_WHITELIST_PATH = WHITELIST_PATH
_DEFAULT_USERCACHE_PATH = USERCACHE_PATH

_SOURCE_ARCHIVE = "archive"
_SOURCE_LIVE = "live"
_DATE_FILE_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-\d+\.log(?:\.gz)?$")
_TIME_PATTERN = re.compile(r"\[(?P<clock>\d{2}:\d{2}:\d{2})[^\]]*\]")
_WHITELIST_ADD_PATTERN = re.compile(r"Added (?P<player>[A-Za-z0-9_.]{1,32}) to the whitelist")
_WHITELIST_REMOVE_PATTERN = re.compile(r"Removed (?P<player>[A-Za-z0-9_.]{1,32}) from the whitelist")
_JOIN_PATTERN = re.compile(r"(?P<player>[A-Za-z0-9_.]{1,32}) joined the game")
_LEAVE_PATTERN = re.compile(r"(?P<player>[A-Za-z0-9_.]{1,32}) left the game")

_db_lock = threading.Lock()
_PROTECTED_PLAYER_KEYS = {player.lower() for player in PROTECTED_PLAYERS}
DEFAULT_INACTIVE_DAYS = 30
LONG_TERM_INACTIVE_DAYS = 100
_EMPTY_AUDIT_SUMMARY = {
    "total_current": 0,
    "recommended_count": 0,
    "never_joined_count": 0,
    "unknown_added_at_count": 0,
    "protected_count": 0,
    "total": 0,
    "recommended": 0,
    "neverJoined": 0,
    "unknownAdded": 0,
}
_EMPTY_MANUAL_PRUNE_SUMMARY = {
    "total": 0,
    "totalArchive": 0,
    "total_archive": 0,
    "loaded": 0,
    "loaded_count": 0,
    "currentlyRemoved": 0,
    "rewhitelisted": 0,
    "returned": 0,
    "neverJoined": 0,
    "never_joined": 0,
}
_EMPTY_LONG_TERM_INACTIVITY_SUMMARY = {
    "thresholdDays": LONG_TERM_INACTIVE_DAYS,
    "total": 0,
    "offWhitelist": 0,
    "currentWhitelisted": 0,
    "rewhitelistedNoNewJoin": 0,
    "neverJoined": 0,
}
_EMPTY_REWHITELIST_SUMMARY = {
    "total": 0,
    "currentWhitelisted": 0,
    "joinedAfterReturn": 0,
    "pendingJoin": 0,
    "inferred": 0,
}


def _path_with_legacy_override(current: Path, default: Path, configured: Path) -> Path:
    current_path = Path(current)
    if current_path != default and current_path != configured:
        return current_path
    return configured


def _logs_dir() -> Path:
    return _path_with_legacy_override(LOGS_DIR, _DEFAULT_LOGS_DIR, minecraft_settings.get_logs_dir())


def _whitelist_path() -> Path:
    return _path_with_legacy_override(
        WHITELIST_PATH,
        _DEFAULT_WHITELIST_PATH,
        minecraft_settings.get_whitelist_path(),
    )


def _usercache_path() -> Path:
    return _path_with_legacy_override(
        USERCACHE_PATH,
        _DEFAULT_USERCACHE_PATH,
        minecraft_settings.get_usercache_path(),
    )


@dataclass
class _ActivityDelta:
    display_name: str
    first_whitelist_added_at: str | None = None
    last_whitelist_added_at: str | None = None
    last_whitelist_removed_at: str | None = None
    last_join_at: str | None = None
    last_leave_at: str | None = None
    last_seen_at: str | None = None


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _isoformat(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _pick_min_iso(left: str | None, right: str | None) -> str | None:
    if not left:
        return right
    if not right:
        return left
    return left if left <= right else right


def _pick_max_iso(left: str | None, right: str | None) -> str | None:
    if not left:
        return right
    if not right:
        return left
    return left if left >= right else right


def _shift_iso(value: str | None, *, days: int) -> str | None:
    if not value or not days:
        return value
    parsed = _parse_iso(value)
    if parsed is None:
        return value
    return _isoformat(parsed + timedelta(days=days))


def _connect() -> sqlite3.Connection:
    AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AUDIT_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _initialize_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_state (
            path TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            offset INTEGER NOT NULL DEFAULT 0,
            cursor_date TEXT,
            cursor_time TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS player_activity_archive (
            player_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            first_whitelist_added_at TEXT,
            last_whitelist_added_at TEXT,
            last_whitelist_removed_at TEXT,
            last_join_at TEXT,
            last_leave_at TEXT,
            last_seen_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS player_activity_live (
            player_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            first_whitelist_added_at TEXT,
            last_whitelist_added_at TEXT,
            last_whitelist_removed_at TEXT,
            last_join_at TEXT,
            last_leave_at TEXT,
            last_seen_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_player_activity_archive_name
            ON player_activity_archive(display_name);
        CREATE INDEX IF NOT EXISTS idx_player_activity_live_name
            ON player_activity_live(display_name);

        CREATE TABLE IF NOT EXISTS shared_audit_snapshot (
            inactive_days INTEGER PRIMARY KEY,
            payload_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            generated_by TEXT,
            whitelist_size INTEGER NOT NULL DEFAULT 0,
            whitelist_mtime_ns INTEGER NOT NULL DEFAULT 0,
            latest_size INTEGER NOT NULL DEFAULT 0,
            latest_mtime_ns INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS manual_prune_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            removed_at TEXT NOT NULL,
            removed_by TEXT,
            removed_from TEXT NOT NULL,
            inactive_days_threshold INTEGER NOT NULL,
            added_at TEXT,
            last_seen_at TEXT,
            days_since_added INTEGER,
            inactivity_days INTEGER,
            never_joined INTEGER NOT NULL DEFAULT 0,
            recommendation_reason TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_manual_prune_history_player_key
            ON manual_prune_history(player_key);
        CREATE INDEX IF NOT EXISTS idx_manual_prune_history_removed_at
            ON manual_prune_history(removed_at DESC);

        CREATE TABLE IF NOT EXISTS whitelist_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            player_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            actor_email TEXT,
            actor_surface TEXT NOT NULL DEFAULT 'unknown',
            source TEXT NOT NULL DEFAULT 'app',
            related_manual_prune_id INTEGER,
            previous_removed_at TEXT,
            previous_last_seen_at TEXT,
            days_after_removal INTEGER,
            days_since_previous_seen INTEGER,
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_whitelist_lifecycle_events_player_key
            ON whitelist_lifecycle_events(player_key, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_whitelist_lifecycle_events_type
            ON whitelist_lifecycle_events(event_type, occurred_at DESC);
        """
    )
    conn.commit()


def _table_name(source_kind: str) -> str:
    return "player_activity_live" if source_kind == _SOURCE_LIVE else "player_activity_archive"


def _latest_log_path() -> Path:
    return _logs_dir() / "latest.log"


def _get_source_state(conn: sqlite3.Connection, path: Path) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM source_state WHERE path = ?",
        (str(path.resolve()),),
    ).fetchone()


def _save_source_state(
    conn: sqlite3.Connection,
    *,
    path: Path,
    source_kind: str,
    size: int,
    mtime_ns: int,
    offset: int,
    cursor_date: str | None,
    cursor_time: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO source_state (
            path, source_kind, size, mtime_ns, offset, cursor_date, cursor_time, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            source_kind = excluded.source_kind,
            size = excluded.size,
            mtime_ns = excluded.mtime_ns,
            offset = excluded.offset,
            cursor_date = excluded.cursor_date,
            cursor_time = excluded.cursor_time,
            updated_at = excluded.updated_at
        """,
        (
            str(path.resolve()),
            source_kind,
            size,
            mtime_ns,
            offset,
            cursor_date,
            cursor_time,
            _now_iso(),
        ),
    )


def _clear_live_overlay(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM player_activity_live")


def _iter_archive_logs() -> Iterable[Path]:
    logs_dir = _logs_dir()
    if not logs_dir.exists():
        return []

    paths = []
    for path in logs_dir.iterdir():
        if path.name == "latest.log":
            continue
        if _DATE_FILE_PATTERN.match(path.name):
            paths.append(path)
    return sorted(paths, key=lambda item: item.name)


def _extract_log_date(path: Path) -> str | None:
    match = _DATE_FILE_PATTERN.match(path.name)
    if not match:
        return None
    return match.group("date")


def _parse_time_token(line: str) -> str | None:
    match = _TIME_PATTERN.search(line)
    if not match:
        return None
    return match.group("clock")


def _update_delta(
    deltas: dict[str, _ActivityDelta],
    *,
    player: str,
    event_type: str,
    occurred_at: datetime,
) -> None:
    player_name = str(player or "").strip()
    if not player_name:
        return

    player_key = player_name.lower()
    iso_time = _isoformat(occurred_at)
    delta = deltas.setdefault(player_key, _ActivityDelta(display_name=player_name))
    delta.display_name = player_name

    if event_type == "whitelist_add":
        delta.first_whitelist_added_at = _pick_min_iso(delta.first_whitelist_added_at, iso_time)
        delta.last_whitelist_added_at = _pick_max_iso(delta.last_whitelist_added_at, iso_time)
        return

    if event_type == "whitelist_remove":
        delta.last_whitelist_removed_at = _pick_max_iso(delta.last_whitelist_removed_at, iso_time)
        return

    if event_type == "join":
        delta.last_join_at = _pick_max_iso(delta.last_join_at, iso_time)
        delta.last_seen_at = _pick_max_iso(delta.last_seen_at, iso_time)
        return

    if event_type == "leave":
        delta.last_leave_at = _pick_max_iso(delta.last_leave_at, iso_time)
        delta.last_seen_at = _pick_max_iso(delta.last_seen_at, iso_time)


def _parse_line(deltas: dict[str, _ActivityDelta], *, line: str, occurred_at: datetime) -> None:
    for pattern, event_type in (
        (_WHITELIST_ADD_PATTERN, "whitelist_add"),
        (_WHITELIST_REMOVE_PATTERN, "whitelist_remove"),
        (_JOIN_PATTERN, "join"),
        (_LEAVE_PATTERN, "leave"),
    ):
        match = pattern.search(line)
        if match:
            _update_delta(
                deltas,
                player=match.group("player"),
                event_type=event_type,
                occurred_at=occurred_at,
            )
            return


def _parse_archive_log(path: Path, *, date_str: str) -> dict[str, _ActivityDelta]:
    deltas: dict[str, _ActivityDelta] = {}
    opener = gzip.open if path.suffix == ".gz" else open

    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                clock = _parse_time_token(raw_line)
                if not clock:
                    continue
                occurred_at = datetime.strptime(f"{date_str} {clock}", "%Y-%m-%d %H:%M:%S")
                _parse_line(deltas, line=raw_line, occurred_at=occurred_at)
    except OSError as exc:
        logger.warning("Failed to parse archive log %s: %s", path, exc)

    return deltas


def _parse_latest_log(
    path: Path,
    *,
    start_offset: int,
    cursor_date: date,
    cursor_time: str | None,
) -> tuple[dict[str, _ActivityDelta], date, str | None]:
    deltas: dict[str, _ActivityDelta] = {}
    current_date = cursor_date
    previous_clock = cursor_time

    try:
        with open(path, "rb") as handle:
            handle.seek(start_offset)
            chunk = handle.read()
    except OSError as exc:
        logger.warning("Failed to read latest.log from offset %s: %s", start_offset, exc)
        return deltas, current_date, previous_clock

    text = chunk.decode("utf-8", errors="replace")
    for raw_line in text.splitlines():
        clock = _parse_time_token(raw_line)
        if not clock:
            continue
        if previous_clock and clock < previous_clock:
            current_date += timedelta(days=1)
        previous_clock = clock
        occurred_at = datetime.strptime(
            f"{current_date.isoformat()} {clock}",
            "%Y-%m-%d %H:%M:%S",
        )
        _parse_line(deltas, line=raw_line, occurred_at=occurred_at)

    return deltas, current_date, previous_clock


def _shift_deltas(
    deltas: dict[str, _ActivityDelta],
    *,
    days: int,
) -> dict[str, _ActivityDelta]:
    if not days:
        return deltas

    shifted: dict[str, _ActivityDelta] = {}
    for player_key, delta in deltas.items():
        shifted[player_key] = _ActivityDelta(
            display_name=delta.display_name,
            first_whitelist_added_at=_shift_iso(delta.first_whitelist_added_at, days=days),
            last_whitelist_added_at=_shift_iso(delta.last_whitelist_added_at, days=days),
            last_whitelist_removed_at=_shift_iso(delta.last_whitelist_removed_at, days=days),
            last_join_at=_shift_iso(delta.last_join_at, days=days),
            last_leave_at=_shift_iso(delta.last_leave_at, days=days),
            last_seen_at=_shift_iso(delta.last_seen_at, days=days),
        )
    return shifted


def _merge_deltas(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    deltas: dict[str, _ActivityDelta],
) -> None:
    if not deltas:
        return

    table = _table_name(source_kind)
    now_iso = _now_iso()
    existing_rows = {
        row["player_key"]: row
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE player_key IN ({','.join('?' for _ in deltas)})",
            tuple(deltas.keys()),
        ).fetchall()
    }

    for player_key, delta in deltas.items():
        existing = existing_rows.get(player_key)
        display_name = delta.display_name
        first_add = delta.first_whitelist_added_at
        last_add = delta.last_whitelist_added_at
        last_remove = delta.last_whitelist_removed_at
        last_join = delta.last_join_at
        last_leave = delta.last_leave_at
        last_seen = delta.last_seen_at

        if existing is not None:
            display_name = display_name or existing["display_name"]
            first_add = _pick_min_iso(existing["first_whitelist_added_at"], first_add)
            last_add = _pick_max_iso(existing["last_whitelist_added_at"], last_add)
            last_remove = _pick_max_iso(existing["last_whitelist_removed_at"], last_remove)
            last_join = _pick_max_iso(existing["last_join_at"], last_join)
            last_leave = _pick_max_iso(existing["last_leave_at"], last_leave)
            last_seen = _pick_max_iso(existing["last_seen_at"], last_seen)

        conn.execute(
            f"""
            INSERT INTO {table} (
                player_key,
                display_name,
                first_whitelist_added_at,
                last_whitelist_added_at,
                last_whitelist_removed_at,
                last_join_at,
                last_leave_at,
                last_seen_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_key) DO UPDATE SET
                display_name = excluded.display_name,
                first_whitelist_added_at = excluded.first_whitelist_added_at,
                last_whitelist_added_at = excluded.last_whitelist_added_at,
                last_whitelist_removed_at = excluded.last_whitelist_removed_at,
                last_join_at = excluded.last_join_at,
                last_leave_at = excluded.last_leave_at,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (
                player_key,
                display_name,
                first_add,
                last_add,
                last_remove,
                last_join,
                last_leave,
                last_seen,
                now_iso,
            ),
        )


def _sync_archives(conn: sqlite3.Connection) -> int:
    processed = 0

    for path in _iter_archive_logs():
        file_date = _extract_log_date(path)
        if not file_date:
            continue

        try:
            stat = path.stat()
        except OSError:
            continue

        state = _get_source_state(conn, path)
        if (
            state is not None
            and state["source_kind"] == _SOURCE_ARCHIVE
            and state["size"] == stat.st_size
            and state["mtime_ns"] == stat.st_mtime_ns
        ):
            continue

        deltas = _parse_archive_log(path, date_str=file_date)
        _merge_deltas(conn, source_kind=_SOURCE_ARCHIVE, deltas=deltas)
        _save_source_state(
            conn,
            path=path,
            source_kind=_SOURCE_ARCHIVE,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            offset=stat.st_size,
            cursor_date=file_date,
            cursor_time=None,
        )
        processed += 1

    return processed


def _sync_latest(conn: sqlite3.Connection) -> bool:
    latest_log_path = _latest_log_path()
    if not latest_log_path.exists():
        return False

    stat = latest_log_path.stat()
    state = _get_source_state(conn, latest_log_path)

    start_offset = 0
    anchor_date = datetime.fromtimestamp(stat.st_mtime).date()
    cursor_date = anchor_date
    cursor_time = None
    rotated = False

    if state is not None and state["source_kind"] == _SOURCE_LIVE:
        state_offset = int(state["offset"] or 0)
        if stat.st_size >= state_offset:
            start_offset = state_offset
            if state["cursor_date"]:
                try:
                    cursor_date = datetime.strptime(state["cursor_date"], "%Y-%m-%d").date()
                except ValueError:
                    cursor_date = anchor_date
            cursor_time = state["cursor_time"]
        else:
            rotated = True

    if rotated:
        _clear_live_overlay(conn)
        start_offset = 0
        cursor_date = anchor_date
        cursor_time = None

    if state is not None and not rotated and stat.st_size == start_offset and state["mtime_ns"] == stat.st_mtime_ns:
        return False

    deltas, final_date, final_clock = _parse_latest_log(
        latest_log_path,
        start_offset=start_offset,
        cursor_date=cursor_date,
        cursor_time=cursor_time,
    )
    if start_offset == 0 and cursor_time is None and final_date > anchor_date:
        # Initial or post-rotation scans only know latest.log's mtime date. If the
        # file crosses midnight, align the parsed window so its last event stays on
        # the anchor date instead of drifting into the future by one or more days.
        overflow_days = (final_date - anchor_date).days
        deltas = _shift_deltas(deltas, days=-overflow_days)
        final_date = anchor_date
    _merge_deltas(conn, source_kind=_SOURCE_LIVE, deltas=deltas)
    _save_source_state(
        conn,
        path=latest_log_path,
        source_kind=_SOURCE_LIVE,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        offset=stat.st_size,
        cursor_date=final_date.isoformat(),
        cursor_time=final_clock,
    )
    return True


def _load_current_whitelist() -> list[dict[str, Any]]:
    whitelist_path = _whitelist_path()
    if not whitelist_path.exists():
        return []

    try:
        with open(whitelist_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read whitelist.json", exc_info=True)
        return []

    players: list[dict[str, Any]] = []
    for index, entry in enumerate(payload if isinstance(payload, list) else []):
        name = str((entry or {}).get("name", "")).strip()
        if not name:
            continue
        uuid = str((entry or {}).get("uuid", "")).strip() or None
        players.append(
            {
                "index": index,
                "player_key": name.lower(),
                "name": name,
                "uuid": uuid,
            }
        )
    return players


def get_current_whitelist_members() -> list[dict[str, Any]]:
    """Return current whitelist members from whitelist.json in file order."""
    return _load_current_whitelist()


def _load_activity_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    player_keys: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    if player_keys is None:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        return {row["player_key"]: dict(row) for row in rows}

    normalized_keys = [str(key or "").strip().lower() for key in player_keys if str(key or "").strip()]
    if not normalized_keys:
        return {}

    rows: dict[str, dict[str, Any]] = {}
    chunk_size = 500
    for start in range(0, len(normalized_keys), chunk_size):
        chunk = normalized_keys[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        chunk_rows = conn.execute(
            f"SELECT * FROM {table} WHERE player_key IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        rows.update({row["player_key"]: dict(row) for row in chunk_rows})
    return rows


def _file_marker(path: Path) -> dict[str, int]:
    try:
        stat = path.stat()
        return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
    except OSError:
        return {"size": 0, "mtime_ns": 0}


def _collect_snapshot_markers() -> dict[str, int]:
    latest_marker = _file_marker(_latest_log_path())
    whitelist_marker = _file_marker(_whitelist_path())
    return {
        "whitelist_size": whitelist_marker["size"],
        "whitelist_mtime_ns": whitelist_marker["mtime_ns"],
        "latest_size": latest_marker["size"],
        "latest_mtime_ns": latest_marker["mtime_ns"],
    }


def _load_shared_snapshot_row(conn: sqlite3.Connection, inactive_days: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM shared_audit_snapshot WHERE inactive_days = ?",
        (inactive_days,),
    ).fetchone()


def _load_latest_shared_snapshot_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM shared_audit_snapshot
        ORDER BY generated_at DESC, updated_at DESC, inactive_days DESC
        LIMIT 1
        """
    ).fetchone()


def _snapshot_row_is_stale(row: sqlite3.Row) -> bool:
    markers = _collect_snapshot_markers()
    return any(
        int(row[column] or 0) != markers[column]
        for column in ("whitelist_size", "whitelist_mtime_ns", "latest_size", "latest_mtime_ns")
    )


def _store_shared_snapshot(
    conn: sqlite3.Connection,
    *,
    inactive_days: int,
    payload: dict[str, Any],
    generated_by: str | None,
) -> None:
    markers = _collect_snapshot_markers()
    generated_at = str(payload.get("generated_at") or _now_iso())
    sanitized_payload = {
        **payload,
        "generated_at": generated_at,
        "indexed_at": generated_at,
        "generated_by": generated_by or None,
        "shared_snapshot_available": True,
        "stale": False,
    }
    conn.execute(
        """
        INSERT INTO shared_audit_snapshot (
            inactive_days,
            payload_json,
            generated_at,
            generated_by,
            whitelist_size,
            whitelist_mtime_ns,
            latest_size,
            latest_mtime_ns,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(inactive_days) DO UPDATE SET
            payload_json = excluded.payload_json,
            generated_at = excluded.generated_at,
            generated_by = excluded.generated_by,
            whitelist_size = excluded.whitelist_size,
            whitelist_mtime_ns = excluded.whitelist_mtime_ns,
            latest_size = excluded.latest_size,
            latest_mtime_ns = excluded.latest_mtime_ns,
            updated_at = excluded.updated_at
        """,
        (
            inactive_days,
            json.dumps(sanitized_payload),
            generated_at,
            generated_by or None,
            markers["whitelist_size"],
            markers["whitelist_mtime_ns"],
            markers["latest_size"],
            markers["latest_mtime_ns"],
            _now_iso(),
        ),
    )


def _empty_snapshot(inactive_days: int) -> dict[str, Any]:
    return {
        "status": "ok",
        "inactive_days": inactive_days,
        "generated_at": None,
        "indexed_at": None,
        "generated_by": None,
        "shared_snapshot_available": False,
        "stale": False,
        "sync": {
            "archive_files_processed": 0,
            "latest_updated": False,
        },
        "summary": dict(_EMPTY_AUDIT_SUMMARY),
        "entries": [],
        "manual_prune_history": [],
        "manual_prune_summary": dict(_EMPTY_MANUAL_PRUNE_SUMMARY),
        "long_term_inactivity_alerts": [],
        "long_term_inactivity_summary": dict(_EMPTY_LONG_TERM_INACTIVITY_SUMMARY),
        "rewhitelist_history": [],
        "rewhitelist_summary": dict(_EMPTY_REWHITELIST_SUMMARY),
    }


def _payload_from_shared_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _empty_snapshot(int(row["inactive_days"]))
    try:
        raw_payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        logger.warning("Failed to decode shared whitelist audit snapshot", exc_info=True)
        raw_payload = {}

    payload.update(raw_payload if isinstance(raw_payload, dict) else {})
    payload["inactive_days"] = int(row["inactive_days"])
    payload["generated_at"] = payload.get("generated_at") or row["generated_at"]
    payload["indexed_at"] = payload.get("indexed_at") or payload["generated_at"]
    payload["generated_by"] = payload.get("generated_by") or row["generated_by"]
    payload["shared_snapshot_available"] = True
    payload["stale"] = _snapshot_row_is_stale(row)
    payload["summary"] = payload.get("summary") or dict(_EMPTY_AUDIT_SUMMARY)
    payload["entries"] = payload.get("entries") or []
    payload["manual_prune_history"] = payload.get("manual_prune_history") or []
    payload["manual_prune_summary"] = payload.get("manual_prune_summary") or dict(_EMPTY_MANUAL_PRUNE_SUMMARY)
    payload["long_term_inactivity_alerts"] = payload.get("long_term_inactivity_alerts") or []
    payload["long_term_inactivity_summary"] = payload.get("long_term_inactivity_summary") or dict(
        _EMPTY_LONG_TERM_INACTIVITY_SUMMARY
    )
    payload["rewhitelist_history"] = payload.get("rewhitelist_history") or []
    payload["rewhitelist_summary"] = payload.get("rewhitelist_summary") or dict(_EMPTY_REWHITELIST_SUMMARY)
    return payload


def _days_between(later_iso: str | None, earlier_iso: str | None) -> int | None:
    later = _parse_iso(later_iso)
    earlier = _parse_iso(earlier_iso)
    if later is None or earlier is None:
        return None
    return max(0, int((later - earlier).total_seconds() // 86400))


def _is_same_or_after(value: str | None, floor: str | None) -> bool:
    value_dt = _parse_iso(value)
    floor_dt = _parse_iso(floor)
    return bool(value_dt is not None and floor_dt is not None and value_dt >= floor_dt)


def _load_current_whitelist_map() -> dict[str, dict[str, Any]]:
    return {member["player_key"]: member for member in _load_current_whitelist()}


AUDIT_SECTION_DEFAULT_LIMIT = 25
AUDIT_SECTION_MAX_LIMIT = 100


def _clamp_audit_page_limit(value: Any, *, default: int = AUDIT_SECTION_DEFAULT_LIMIT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, AUDIT_SECTION_MAX_LIMIT))


def _clamp_audit_page_offset(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, parsed)


def _normalize_audit_query(value: Any) -> str:
    return str(value or "").strip().lower()


def _load_latest_manual_prune_rows(
    conn: sqlite3.Connection,
    *,
    limit: int | None = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    page_clause = ""
    if limit is not None:
        page_clause = "LIMIT ? OFFSET ?"
        params.extend((max(1, min(int(limit or 200), 500)), max(0, int(offset or 0))))

    rows = conn.execute(
        """
        SELECT h.*
        FROM manual_prune_history AS h
        INNER JOIN (
            SELECT player_key, MAX(id) AS latest_id
            FROM manual_prune_history
            GROUP BY player_key
        ) AS latest
            ON latest.latest_id = h.id
        ORDER BY h.removed_at DESC, h.id DESC
        """
        + page_clause,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _format_manual_prune_row(
    row: dict[str, Any],
    *,
    current_members: dict[str, dict[str, Any]],
    activity_by_player: dict[str, dict[str, Any]],
    now_iso: str,
) -> dict[str, Any]:
    player_key = str(row["player_key"] or "").strip().lower()
    current_member = current_members.get(player_key)
    activity = activity_by_player.get(player_key) or {}
    threshold_days = int(row["inactive_days_threshold"])
    days_since_added = int(row["days_since_added"]) if row["days_since_added"] is not None else None
    inactive_days_at_removal = int(row["inactivity_days"]) if row["inactivity_days"] is not None else None
    days_since_removal = _days_between(now_iso, row["removed_at"])
    last_seen_at_at_removal = row["last_seen_at"]
    current_last_seen_at = activity.get("last_seen_at")
    current_inactive_days = _days_between(now_iso, current_last_seen_at)
    return {
        "id": int(row["id"]),
        "name": row["display_name"],
        "removed_at": row["removed_at"],
        "removed_by": row["removed_by"],
        "removed_from": row["removed_from"],
        "inactive_days_threshold": threshold_days,
        "removal_threshold_days": threshold_days,
        "added_at": row["added_at"],
        "last_seen_at": last_seen_at_at_removal,
        "last_seen_at_at_removal": last_seen_at_at_removal,
        "last_seen_at_removal": last_seen_at_at_removal,
        "current_last_seen_at": current_last_seen_at,
        "days_since_added": days_since_added,
        "days_since_added_at_removal": days_since_added,
        "inactive_days": inactive_days_at_removal,
        "inactive_days_at_removal": inactive_days_at_removal,
        "current_inactive_days": current_inactive_days,
        "days_since_last_seen": current_inactive_days,
        "days_since_removal": days_since_removal,
        "never_joined": bool(row["never_joined"]),
        "recommendation_reason": row["recommendation_reason"] or "",
        "note": row["note"] or "",
        "current_whitelisted": current_member is not None,
        "current_whitelist_name": current_member["name"] if current_member else None,
    }


def _manual_prune_status_key(item: dict[str, Any]) -> str:
    if item.get("never_joined"):
        return "never_joined"
    return "returned" if item.get("current_whitelisted") else "removed"


def _manual_prune_search_text(item: dict[str, Any]) -> str:
    status = "back on whitelist" if item.get("current_whitelisted") else "still removed"
    activity = (
        f"whitelist age at removal {item.get('days_since_added_at_removal')} days"
        if item.get("never_joined")
        else f"inactive at removal {item.get('inactive_days_at_removal')} days"
    )
    return " ".join(
        str(value)
        for value in (
            item.get("name"),
            item.get("removed_by"),
            item.get("removed_from"),
            item.get("current_whitelist_name"),
            item.get("recommendation_reason"),
            item.get("note"),
            item.get("removed_at"),
            item.get("last_seen_at_at_removal"),
            item.get("current_last_seen_at"),
            status,
            activity,
            "never joined" if item.get("never_joined") else None,
        )
        if value
    ).lower()


def _manual_prune_item_matches(item: dict[str, Any], *, query: str, status: str) -> bool:
    normalized_status = str(status or "all").strip().lower()
    if normalized_status == "returned" and not item.get("current_whitelisted"):
        return False
    if normalized_status == "removed" and item.get("current_whitelisted"):
        return False
    if normalized_status == "never_joined" and not item.get("never_joined"):
        return False
    if normalized_status not in {"all", "returned", "removed", "never_joined"}:
        return False
    if query and query not in _manual_prune_search_text(item):
        return False
    return True


def _build_manual_prune_page_summary(
    *,
    total: int,
    filtered_items: list[dict[str, Any]],
    limit: int,
    offset: int,
) -> dict[str, int | bool]:
    filtered = len(filtered_items)
    returned = sum(1 for item in filtered_items if item.get("current_whitelisted"))
    never_joined = sum(1 for item in filtered_items if item.get("never_joined"))
    currently_removed = filtered - returned
    return {
        "total": total,
        "totalArchive": total,
        "total_archive": total,
        "filtered": filtered,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < filtered,
        "hasMore": offset + limit < filtered,
        "currently_removed": currently_removed,
        "currentlyRemoved": currently_removed,
        "rewhitelisted": returned,
        "returned": returned,
        "never_joined": never_joined,
        "neverJoined": never_joined,
    }


def _manual_prune_sort_key(item: dict[str, Any], sort: str) -> tuple[Any, ...]:
    normalized_sort = str(sort or "removed_desc").strip().lower()
    removed_at = str(item.get("removed_at") or "")
    name = str(item.get("name") or "").lower()
    if normalized_sort == "removed_asc":
        return (removed_at, name)
    if normalized_sort == "name_asc":
        return (name, removed_at)
    if normalized_sort == "name_desc":
        return (_reverse_sort_text(name), removed_at)
    return (_reverse_sort_text(removed_at), name)


def _reverse_sort_text(value: str) -> tuple[int, ...]:
    return tuple(-ord(char) for char in value)


def _load_manual_prune_history_page(
    conn: sqlite3.Connection,
    *,
    limit: int = AUDIT_SECTION_DEFAULT_LIMIT,
    offset: int = 0,
    q: str = "",
    status: str = "all",
    sort: str = "removed_desc",
) -> dict[str, Any]:
    capped_limit = _clamp_audit_page_limit(limit)
    safe_offset = _clamp_audit_page_offset(offset)
    query = _normalize_audit_query(q)
    latest_rows = _load_latest_manual_prune_rows(conn, limit=None)
    current_members = _load_current_whitelist_map()
    activity_by_player = _load_combined_activity_map(
        conn,
        [row["player_key"] for row in latest_rows],
    )
    now_iso = _now_iso()
    all_items = [
        _format_manual_prune_row(
            row,
            current_members=current_members,
            activity_by_player=activity_by_player,
            now_iso=now_iso,
        )
        for row in latest_rows
    ]
    filtered_items = [
        item
        for item in all_items
        if _manual_prune_item_matches(item, query=query, status=status)
    ]
    filtered_items.sort(key=lambda item: _manual_prune_sort_key(item, sort))
    page_items = filtered_items[safe_offset : safe_offset + capped_limit]
    has_more = safe_offset + capped_limit < len(filtered_items)
    return {
        "status": "ok",
        "items": page_items,
        "summary": _build_manual_prune_page_summary(
            total=len(all_items),
            filtered_items=filtered_items,
            limit=capped_limit,
            offset=safe_offset,
        ),
        "limit": capped_limit,
        "offset": safe_offset,
        "has_more": has_more,
        "hasMore": has_more,
    }


def get_manual_prune_history_page(
    *,
    limit: int = AUDIT_SECTION_DEFAULT_LIMIT,
    offset: int = 0,
    q: str = "",
    status: str = "all",
    sort: str = "removed_desc",
) -> dict[str, Any]:
    conn = _connect()
    try:
        _initialize_db(conn)
        return _load_manual_prune_history_page(
            conn,
            limit=limit,
            offset=offset,
            q=q,
            status=status,
            sort=sort,
        )
    finally:
        conn.close()


def _count_latest_manual_prune_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM (
            SELECT player_key
            FROM manual_prune_history
            GROUP BY player_key
        )
        """
    ).fetchone()
    return int(row["total"] or 0) if row is not None else 0


def _load_latest_manual_prune_row_for_player(
    conn: sqlite3.Connection,
    *,
    player_key: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM manual_prune_history
        WHERE player_key = ?
        ORDER BY removed_at DESC, id DESC
        LIMIT 1
        """,
        (player_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def _load_combined_activity_map(
    conn: sqlite3.Connection,
    player_keys: Iterable[str],
) -> dict[str, dict[str, Any]]:
    keys = list(
        dict.fromkeys(
            str(key or "").strip().lower()
            for key in player_keys
            if str(key or "").strip()
        )
    )
    if not keys:
        return {}

    archive_map = _load_activity_rows(conn, "player_activity_archive", player_keys=keys)
    live_map = _load_activity_rows(conn, "player_activity_live", player_keys=keys)
    return {
        key: _merge_activity_row(archive_map.get(key), live_map.get(key))
        for key in set(keys) | set(archive_map) | set(live_map)
    }


def _build_manual_prune_summary(items: list[dict[str, Any]], *, total_archive: int | None = None) -> dict[str, int]:
    loaded = len(items)
    archive_total = total_archive if total_archive is not None else loaded
    rewhitelisted = sum(1 for item in items if item.get("current_whitelisted"))
    never_joined = sum(1 for item in items if item.get("never_joined"))
    return {
        "total": loaded,
        "totalArchive": archive_total,
        "total_archive": archive_total,
        "loaded": loaded,
        "loaded_count": loaded,
        "count": loaded,
        "currentlyRemoved": loaded - rewhitelisted,
        "rewhitelisted": rewhitelisted,
        "returned": rewhitelisted,
        "neverJoined": never_joined,
        "never_joined": never_joined,
    }


def _load_manual_prune_history(conn: sqlite3.Connection, *, limit: int = 50) -> tuple[list[dict[str, Any]], dict[str, int]]:
    latest_rows = _load_latest_manual_prune_rows(conn, limit=limit)
    total_archive = _count_latest_manual_prune_rows(conn)
    current_members = _load_current_whitelist_map()
    activity_by_player = _load_combined_activity_map(
        conn,
        [row["player_key"] for row in latest_rows],
    )
    now_iso = _now_iso()
    items = [
        _format_manual_prune_row(
            row,
            current_members=current_members,
            activity_by_player=activity_by_player,
            now_iso=now_iso,
        )
        for row in latest_rows
    ]
    return items, _build_manual_prune_summary(items, total_archive=total_archive)


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_rewhitelist_history_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    total = len(items)
    joined = sum(1 for item in items if item.get("joined_after_rewhitelist"))
    return {
        "total": total,
        "currentWhitelisted": sum(1 for item in items if item.get("current_whitelisted")),
        "joinedAfterReturn": joined,
        "pendingJoin": total - joined,
        "inferred": sum(1 for item in items if item.get("inferred")),
    }


def _format_rewhitelist_event(
    *,
    event: dict[str, Any],
    activity: dict[str, Any] | None,
    current_members: dict[str, dict[str, Any]],
    inferred: bool,
) -> dict[str, Any]:
    player_key = str(event.get("player_key") or "").strip().lower()
    current_member = current_members.get(player_key)
    occurred_at = event.get("occurred_at")
    previous_removed_at = event.get("previous_removed_at")
    previous_last_seen_at = event.get("previous_last_seen_at")
    activity = activity or {}
    last_seen_at = activity.get("last_seen_at")
    joined_after_return = _is_same_or_after(last_seen_at, occurred_at)

    return {
        "id": event.get("id"),
        "name": current_member["name"] if current_member else event.get("display_name"),
        "occurred_at": occurred_at,
        "actor_email": event.get("actor_email"),
        "actor_surface": event.get("actor_surface") or "unknown",
        "source": event.get("source") or ("log_index" if inferred else "app"),
        "related_manual_prune_id": event.get("related_manual_prune_id"),
        "previous_removed_at": previous_removed_at,
        "previous_last_seen_at": previous_last_seen_at,
        "days_after_removal": _coerce_optional_int(event.get("days_after_removal"))
        if event.get("days_after_removal") is not None
        else _days_between(occurred_at, previous_removed_at),
        "days_since_previous_seen": _coerce_optional_int(event.get("days_since_previous_seen"))
        if event.get("days_since_previous_seen") is not None
        else _days_between(occurred_at, previous_last_seen_at),
        "current_whitelisted": current_member is not None,
        "current_whitelist_name": current_member["name"] if current_member else None,
        "joined_after_rewhitelist": joined_after_return,
        "last_seen_after_rewhitelist_at": last_seen_at if joined_after_return else None,
        "last_seen_at": last_seen_at,
        "inferred": inferred,
        "note": event.get("note") or "Player was re-added after an audit-based whitelist removal.",
    }


def _load_rewhitelist_history(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    capped_limit = max(1, min(int(limit or 50), 200))
    stored_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM whitelist_lifecycle_events
            WHERE event_type = 'rewhitelist'
            ORDER BY occurred_at DESC, id DESC
            LIMIT ?
            """,
            (capped_limit,),
        ).fetchall()
    ]
    manual_rows = _load_latest_manual_prune_rows(conn, limit=200)
    current_members = _load_current_whitelist_map()
    activity_keys = {
        *(row["player_key"] for row in stored_rows if row.get("player_key")),
        *(row["player_key"] for row in manual_rows if row.get("player_key")),
    }
    activity_map = _load_combined_activity_map(conn, activity_keys)

    items: list[dict[str, Any]] = []
    seen_event_keys: set[tuple[str, str, str]] = set()
    stored_prune_keys: set[tuple[str, str]] = set()

    for row in stored_rows:
        player_key = str(row.get("player_key") or "").strip().lower()
        previous_removed_at = str(row.get("previous_removed_at") or "")
        event_key = (player_key, str(row.get("occurred_at") or ""), previous_removed_at)
        if not player_key or event_key in seen_event_keys:
            continue
        seen_event_keys.add(event_key)
        if previous_removed_at:
            stored_prune_keys.add((player_key, previous_removed_at))
        items.append(
            _format_rewhitelist_event(
                event=row,
                activity=activity_map.get(player_key),
                current_members=current_members,
                inferred=False,
            )
        )

    for row in manual_rows:
        player_key = str(row.get("player_key") or "").strip().lower()
        removed_at = str(row.get("removed_at") or "")
        if not player_key or not removed_at or (player_key, removed_at) in stored_prune_keys:
            continue
        activity = activity_map.get(player_key) or {}
        rewhitelisted_at = activity.get("last_whitelist_added_at")
        if not _is_same_or_after(rewhitelisted_at, removed_at):
            continue

        event_key = (player_key, str(rewhitelisted_at or ""), removed_at)
        if event_key in seen_event_keys:
            continue
        seen_event_keys.add(event_key)

        items.append(
            _format_rewhitelist_event(
                event={
                    "id": f"inferred-{row.get('id')}",
                    "player_key": player_key,
                    "display_name": row.get("display_name"),
                    "occurred_at": rewhitelisted_at,
                    "actor_email": None,
                    "actor_surface": "external",
                    "source": "log_index",
                    "related_manual_prune_id": row.get("id"),
                    "previous_removed_at": removed_at,
                    "previous_last_seen_at": row.get("last_seen_at"),
                    "note": "Inferred from retained whitelist add logs after an audit removal.",
                },
                activity=activity,
                current_members=current_members,
                inferred=True,
            )
        )

    items.sort(key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("name") or "").lower()), reverse=True)
    items = items[:capped_limit]
    return items, _build_rewhitelist_history_summary(items)


def _build_long_term_inactivity_summary(
    items: list[dict[str, Any]],
    *,
    threshold_days: int = LONG_TERM_INACTIVE_DAYS,
    total: int | None = None,
    filtered: int | None = None,
    limit: int | None = None,
    offset: int | None = None,
    has_more: bool | None = None,
) -> dict[str, int | bool]:
    total_count = len(items) if total is None else total
    filtered_count = len(items) if filtered is None else filtered
    off_whitelist = sum(1 for item in items if not item.get("current_whitelisted"))
    current_whitelisted = sum(1 for item in items if item.get("current_whitelisted"))
    rewhitelisted_no_new_join = sum(1 for item in items if item.get("status") == "rewhitelisted_no_new_join")
    never_joined = sum(1 for item in items if item.get("status") == "never_joined_long_term")
    summary: dict[str, int | bool] = {
        "thresholdDays": threshold_days,
        "threshold_days": threshold_days,
        "total": total_count,
        "filtered": filtered_count,
        "offWhitelist": off_whitelist,
        "off_whitelist": off_whitelist,
        "currentWhitelisted": current_whitelisted,
        "current_whitelisted": current_whitelisted,
        "rewhitelistedNoNewJoin": rewhitelisted_no_new_join,
        "rewhitelisted_no_new_join": rewhitelisted_no_new_join,
        "neverJoined": never_joined,
        "never_joined": never_joined,
    }
    if limit is not None:
        summary["limit"] = limit
    if offset is not None:
        summary["offset"] = offset
    if has_more is not None:
        summary["has_more"] = has_more
        summary["hasMore"] = has_more
    return summary


def _build_legacy_long_term_inactivity_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "thresholdDays": LONG_TERM_INACTIVE_DAYS,
        "total": len(items),
        "offWhitelist": sum(1 for item in items if not item.get("current_whitelisted")),
        "currentWhitelisted": sum(1 for item in items if item.get("current_whitelisted")),
        "rewhitelistedNoNewJoin": sum(1 for item in items if item.get("status") == "rewhitelisted_no_new_join"),
        "neverJoined": sum(1 for item in items if item.get("status") == "never_joined_long_term"),
    }


def _long_term_alert_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    status = str(item.get("status") or "")
    if status == "rewhitelisted_no_new_join":
        priority = 0
    elif item.get("current_whitelisted"):
        priority = 1
    else:
        priority = 2
    age = _coerce_optional_int(item.get("inactive_days")) or _coerce_optional_int(item.get("days_since_added")) or -1
    return (priority, -age, str(item.get("name") or "").lower())


def _long_term_page_sort_key(item: dict[str, Any], sort: str) -> tuple[Any, ...]:
    normalized_sort = str(sort or "review_desc").strip().lower()
    name = str(item.get("name") or "").lower()
    age = _coerce_optional_int(item.get("inactive_days")) or _coerce_optional_int(item.get("days_since_added")) or -1
    if normalized_sort == "inactive_asc":
        return (age, name)
    if normalized_sort == "name_asc":
        return (name, -age)
    if normalized_sort == "name_desc":
        return (_reverse_sort_text(name), -age)
    return _long_term_alert_sort_key(item)


def _load_long_term_inactivity_alerts(
    conn: sqlite3.Connection,
    *,
    threshold_days: int = LONG_TERM_INACTIVE_DAYS,
    limit: int | None = 100,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    threshold = max(1, min(int(threshold_days or LONG_TERM_INACTIVE_DAYS), 3650))
    capped_limit = max(1, min(int(limit or 100), 500)) if limit is not None else None
    current_members = _load_current_whitelist_map()
    manual_rows = _load_latest_manual_prune_rows(conn, limit=None)
    manual_map = {
        str(row.get("player_key") or "").strip().lower(): row
        for row in manual_rows
        if str(row.get("player_key") or "").strip()
    }
    candidate_keys = set(current_members) | set(manual_map)
    activity_map = _load_combined_activity_map(conn, candidate_keys)
    now = datetime.now()
    alerts: list[dict[str, Any]] = []

    for player_key in sorted(candidate_keys):
        current_member = current_members.get(player_key)
        manual_row = manual_map.get(player_key)
        activity = activity_map.get(player_key) or {}
        display_name = (
            current_member["name"]
            if current_member
            else activity.get("display_name")
            or (manual_row or {}).get("display_name")
        )
        if not display_name:
            continue

        added_at = activity.get("last_whitelist_added_at") or (manual_row or {}).get("added_at")
        last_seen_at = activity.get("last_seen_at") or (manual_row or {}).get("last_seen_at")
        last_pruned_at = (manual_row or {}).get("removed_at")
        rewhitelisted_at = added_at if _is_same_or_after(added_at, last_pruned_at) and last_pruned_at else None
        joined_after_rewhitelist = bool(rewhitelisted_at and _is_same_or_after(last_seen_at, rewhitelisted_at))
        inactive_days = _days_since(now, last_seen_at)
        days_since_added = _days_since(now, added_at)

        if last_seen_at:
            if inactive_days is None or inactive_days < threshold:
                continue
            status = "lands_review_due"
            reason = f"Last seen {inactive_days} day(s) ago; Lands review threshold is {threshold} day(s)."
        elif added_at:
            if days_since_added is None or days_since_added < threshold:
                continue
            status = "never_joined_long_term"
            reason = (
                f"No join record after {days_since_added} day(s) on whitelist; "
                f"Lands review threshold is {threshold} day(s)."
            )
        else:
            continue

        if rewhitelisted_at and not joined_after_rewhitelist:
            status = "rewhitelisted_no_new_join"
            reason = (
                "Player was re-added after an audit removal, but no later join record was found; "
                "use the previous activity date for the 100-day Lands review."
            )

        alerts.append(
            {
                "name": display_name,
                "threshold_days": threshold,
                "last_seen_at": last_seen_at,
                "inactive_days": inactive_days,
                "added_at": added_at,
                "days_since_added": days_since_added,
                "last_pruned_at": last_pruned_at,
                "removed_by": (manual_row or {}).get("removed_by"),
                "current_whitelisted": current_member is not None,
                "current_whitelist_name": current_member["name"] if current_member else None,
                "rewhitelisted_at": rewhitelisted_at,
                "joined_after_rewhitelist": joined_after_rewhitelist,
                "needs_lands_review": True,
                "status": status,
                "protected_player": player_key in _PROTECTED_PLAYER_KEYS,
                "recommendation_reason": "lands_review_due",
                "note": reason,
            }
        )

    alerts.sort(key=_long_term_alert_sort_key)
    if capped_limit is not None:
        alerts = alerts[:capped_limit]
    return alerts, _build_legacy_long_term_inactivity_summary(alerts)


def _long_term_alert_search_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(value)
        for value in (
            item.get("name"),
            item.get("current_whitelist_name"),
            item.get("removed_by"),
            item.get("status"),
            item.get("note"),
            item.get("last_seen_at"),
            item.get("added_at"),
            item.get("last_pruned_at"),
            "whitelisted" if item.get("current_whitelisted") else "off whitelist",
        )
        if value
    ).lower()


def _long_term_alert_matches(
    item: dict[str, Any],
    *,
    query: str,
    status: str,
    whitelist: str,
) -> bool:
    normalized_status = str(status or "all").strip().lower()
    normalized_whitelist = str(whitelist or "all").strip().lower()
    if normalized_status != "all" and str(item.get("status") or "") != normalized_status:
        return False
    if normalized_whitelist == "current" and not item.get("current_whitelisted"):
        return False
    if normalized_whitelist == "off_whitelist" and item.get("current_whitelisted"):
        return False
    if query and query not in _long_term_alert_search_text(item):
        return False
    return True


def _load_long_term_inactivity_page(
    conn: sqlite3.Connection,
    *,
    threshold_days: int = LONG_TERM_INACTIVE_DAYS,
    limit: int = AUDIT_SECTION_DEFAULT_LIMIT,
    offset: int = 0,
    q: str = "",
    status: str = "all",
    whitelist: str = "all",
    sort: str = "review_desc",
) -> dict[str, Any]:
    threshold = max(1, min(int(threshold_days or LONG_TERM_INACTIVE_DAYS), 3650))
    capped_limit = _clamp_audit_page_limit(limit)
    safe_offset = _clamp_audit_page_offset(offset)
    query = _normalize_audit_query(q)
    all_items, _legacy_summary = _load_long_term_inactivity_alerts(
        conn,
        threshold_days=threshold,
        limit=None,
    )
    filtered_items = [
        item
        for item in all_items
        if _long_term_alert_matches(
            item,
            query=query,
            status=status,
            whitelist=whitelist,
        )
    ]
    filtered_items.sort(key=lambda item: _long_term_page_sort_key(item, sort))
    page_items = filtered_items[safe_offset : safe_offset + capped_limit]
    has_more = safe_offset + capped_limit < len(filtered_items)
    return {
        "status": "ok",
        "items": page_items,
        "summary": _build_long_term_inactivity_summary(
            filtered_items,
            threshold_days=threshold,
            total=len(all_items),
            filtered=len(filtered_items),
            limit=capped_limit,
            offset=safe_offset,
            has_more=has_more,
        ),
        "limit": capped_limit,
        "offset": safe_offset,
        "has_more": has_more,
        "hasMore": has_more,
    }


def get_long_term_inactivity_page(
    *,
    threshold_days: int = LONG_TERM_INACTIVE_DAYS,
    limit: int = AUDIT_SECTION_DEFAULT_LIMIT,
    offset: int = 0,
    q: str = "",
    status: str = "all",
    whitelist: str = "all",
    sort: str = "review_desc",
) -> dict[str, Any]:
    conn = _connect()
    try:
        _initialize_db(conn)
        return _load_long_term_inactivity_page(
            conn,
            threshold_days=threshold_days,
            limit=limit,
            offset=offset,
            q=q,
            status=status,
            whitelist=whitelist,
            sort=sort,
        )
    finally:
        conn.close()


def _attach_review_sections(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> dict[str, Any]:
    history, summary = _load_manual_prune_history(conn)
    snapshot["manual_prune_history"] = history
    snapshot["manual_prune_summary"] = summary

    long_term_alerts, long_term_summary = _load_long_term_inactivity_alerts(conn)
    snapshot["long_term_inactivity_alerts"] = long_term_alerts
    snapshot["long_term_inactivity_summary"] = long_term_summary

    rewhitelist_history, rewhitelist_summary = _load_rewhitelist_history(conn)
    snapshot["rewhitelist_history"] = rewhitelist_history
    snapshot["rewhitelist_summary"] = rewhitelist_summary
    return snapshot


def _merge_activity_row(
    archive_row: dict[str, Any] | None,
    live_row: dict[str, Any] | None,
) -> dict[str, Any]:
    display_name = None
    for row in (live_row, archive_row):
        if row and row.get("display_name"):
            display_name = row["display_name"]
            break

    return {
        "display_name": display_name,
        "first_whitelist_added_at": _pick_min_iso(
            archive_row.get("first_whitelist_added_at") if archive_row else None,
            live_row.get("first_whitelist_added_at") if live_row else None,
        ),
        "last_whitelist_added_at": _pick_max_iso(
            archive_row.get("last_whitelist_added_at") if archive_row else None,
            live_row.get("last_whitelist_added_at") if live_row else None,
        ),
        "last_whitelist_removed_at": _pick_max_iso(
            archive_row.get("last_whitelist_removed_at") if archive_row else None,
            live_row.get("last_whitelist_removed_at") if live_row else None,
        ),
        "last_join_at": _pick_max_iso(
            archive_row.get("last_join_at") if archive_row else None,
            live_row.get("last_join_at") if live_row else None,
        ),
        "last_leave_at": _pick_max_iso(
            archive_row.get("last_leave_at") if archive_row else None,
            live_row.get("last_leave_at") if live_row else None,
        ),
        "last_seen_at": _pick_max_iso(
            archive_row.get("last_seen_at") if archive_row else None,
            live_row.get("last_seen_at") if live_row else None,
        ),
    }


def _days_since(now: datetime, iso_value: str | None) -> int | None:
    stamp = _parse_iso(iso_value)
    if stamp is None:
        return None
    delta = now - stamp
    return max(0, int(delta.total_seconds() // 86400))


def _timestamp_is_stale_for_current_membership(
    value: str | None,
    *,
    added_at: str | None,
    removed_at: str | None,
) -> bool:
    if not value:
        return False
    if removed_at and value <= removed_at:
        return True
    if added_at and value < added_at:
        return True
    return False


def _normalize_current_activity(activity: dict[str, Any]) -> dict[str, Any]:
    current = dict(activity)
    added_at = current.get("last_whitelist_added_at")
    removed_at = current.get("last_whitelist_removed_at")
    if added_at and removed_at and removed_at >= added_at:
        added_at = None

    current["last_whitelist_added_at"] = added_at
    for key in ("last_join_at", "last_leave_at", "last_seen_at"):
        value = current.get(key)
        if _timestamp_is_stale_for_current_membership(
            value,
            added_at=added_at,
            removed_at=removed_at,
        ):
            current[key] = None
    return current


def _build_entry(
    *,
    member: dict[str, Any],
    activity: dict[str, Any] | None,
    inactive_days: int,
    now: datetime,
) -> dict[str, Any]:
    activity = _normalize_current_activity(activity or {})
    added_at = activity.get("last_whitelist_added_at")

    last_seen_at = activity.get("last_seen_at")
    days_since_added = _days_since(now, added_at)
    days_since_seen = _days_since(now, last_seen_at)
    protected = member["name"].lower() in _PROTECTED_PLAYER_KEYS

    recommended = False
    can_prune = not protected
    status = "recent"
    reasons: list[str] = []
    recommendation_reason = "recent"

    if protected:
        status = "protected"
        recommendation_reason = "protected"
        reasons.append("Protected player is excluded from batch prune.")

    if last_seen_at:
        reasons.append(f"Last seen {days_since_seen} day(s) ago.")
        if days_since_seen is not None and days_since_seen >= inactive_days and not protected:
            recommended = True
            status = "candidate"
            recommendation_reason = "inactive"
    elif added_at:
        reasons.append("No join record found in retained logs.")
        if days_since_added is not None:
            reasons.append(f"Current whitelist registration is {days_since_added} day(s) old.")
        if days_since_added is not None and days_since_added >= inactive_days and not protected:
            recommended = True
            status = "never_joined_candidate"
            recommendation_reason = "never_joined"
        elif not protected:
            status = "never_joined_recent"
            recommendation_reason = "never_joined_recent"
    else:
        if not protected:
            status = "unknown"
            recommendation_reason = "insufficient_history"
        reasons.append("Whitelist add date was not found in retained logs.")
        reasons.append("No join record found in retained logs.")

    if status == "recent" and days_since_seen is not None:
        reasons.append(f"Inactive threshold is {inactive_days} day(s).")

    return {
        "index": member["index"],
        "name": member["name"],
        "uuid": member["uuid"],
        "current_whitelisted": True,
        "added_at": added_at,
        "added_at_confidence": "exact" if added_at else "unknown",
        "added_source": "log_index" if added_at else "",
        "last_joined_at": activity.get("last_join_at"),
        "last_left_at": activity.get("last_leave_at"),
        "last_seen_at": last_seen_at,
        "last_seen_confidence": "exact" if last_seen_at else "unknown",
        "last_seen_source": "log_index" if last_seen_at else "",
        "days_since_added": days_since_added,
        "inactive_days": days_since_seen,
        "never_joined": last_seen_at is None,
        "recommended": recommended,
        "recommended_removal": recommended,
        "can_prune": can_prune,
        "status": status,
        "recommendation_reason": recommendation_reason,
        "protected_player": protected,
        "data_confidence": "high" if added_at and last_seen_at else "medium" if added_at or last_seen_at else "low",
        "note": reasons[0] if reasons else "",
        "reasons": reasons,
    }


def _audit_entry_sort_key(entry: dict[str, Any]) -> tuple[int, int, str]:
    never_joined = bool(entry.get("never_joined"))
    recommended = bool(entry.get("recommended"))
    if recommended and never_joined:
        priority = 0
    elif recommended:
        priority = 1
    elif never_joined:
        priority = 2
    else:
        priority = 3

    age_value = entry.get("days_since_added") if never_joined else entry.get("inactive_days")
    age = int(age_value) if isinstance(age_value, int) else -1
    return (priority, -age, str(entry.get("name") or "").lower())


def _build_live_snapshot(conn: sqlite3.Connection, *, inactive_days: int) -> dict[str, Any]:
    threshold = max(1, min(int(inactive_days or DEFAULT_INACTIVE_DAYS), 3650))
    archive_updates = _sync_archives(conn)
    live_updated = _sync_latest(conn)
    whitelist_members = _load_current_whitelist()
    whitelist_keys = [member["player_key"] for member in whitelist_members]
    archive_map = _load_activity_rows(conn, "player_activity_archive", player_keys=whitelist_keys)
    live_map = _load_activity_rows(conn, "player_activity_live", player_keys=whitelist_keys)
    merged_map = {
        key: _merge_activity_row(archive_map.get(key), live_map.get(key))
        for key in set(archive_map) | set(live_map)
    }
    now = datetime.now()
    entries = [
        _build_entry(
            member=member,
            activity=merged_map.get(member["player_key"]),
            inactive_days=threshold,
            now=now,
        )
        for member in whitelist_members
    ]

    entries.sort(key=_audit_entry_sort_key)

    summary = {
        "total_current": len(entries),
        "recommended_count": sum(1 for entry in entries if entry["recommended"]),
        "never_joined_count": sum(1 for entry in entries if entry["last_seen_at"] is None),
        "unknown_added_at_count": sum(1 for entry in entries if entry["added_at"] is None),
        "protected_count": sum(1 for entry in entries if entry["status"] == "protected"),
    }
    summary["total"] = summary["total_current"]
    summary["recommended"] = summary["recommended_count"]
    summary["neverJoined"] = summary["never_joined_count"]
    summary["unknownAdded"] = summary["unknown_added_at_count"]
    generated_at = _now_iso()

    return {
        "status": "ok",
        "inactive_days": threshold,
        "generated_at": generated_at,
        "indexed_at": generated_at,
        "sync": {
            "archive_files_processed": archive_updates,
            "latest_updated": live_updated,
        },
        "summary": summary,
        "entries": entries,
    }


def _compute_live_snapshot(inactive_days: int = DEFAULT_INACTIVE_DAYS) -> dict[str, Any]:
    threshold = max(1, min(int(inactive_days or DEFAULT_INACTIVE_DAYS), 3650))
    with _db_lock:
        conn = _connect()
        try:
            _initialize_db(conn)
            snapshot = _build_live_snapshot(conn, inactive_days=threshold)
            conn.commit()
            return snapshot
        finally:
            conn.close()


def get_whitelist_audit_snapshot(
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    *,
    force_refresh: bool = False,
    actor_email: str | None = None,
    latest_shared: bool = False,
) -> dict[str, Any]:
    threshold = max(1, min(int(inactive_days or DEFAULT_INACTIVE_DAYS), 3650))

    with _db_lock:
        conn = _connect()
        try:
            _initialize_db(conn)

            if force_refresh:
                snapshot = _build_live_snapshot(conn, inactive_days=threshold)
                snapshot["generated_by"] = actor_email or None
                snapshot["shared_snapshot_available"] = True
                snapshot["stale"] = False
                _store_shared_snapshot(
                    conn,
                    inactive_days=threshold,
                    payload=snapshot,
                    generated_by=actor_email,
                )
                conn.commit()
                snapshot["requested_inactive_days"] = threshold
                snapshot["threshold_matches_request"] = True
                _attach_review_sections(conn, snapshot)
                return snapshot

            row = _load_latest_shared_snapshot_row(conn) if latest_shared else _load_shared_snapshot_row(conn, threshold)
            if row is None:
                snapshot = _empty_snapshot(threshold)
                snapshot["requested_inactive_days"] = threshold
                snapshot["threshold_matches_request"] = True
                _attach_review_sections(conn, snapshot)
                return snapshot
            snapshot = _payload_from_shared_snapshot_row(row)
            snapshot["requested_inactive_days"] = threshold
            snapshot["threshold_matches_request"] = int(snapshot.get("inactive_days") or threshold) == threshold
            _attach_review_sections(conn, snapshot)
            return snapshot
        finally:
            conn.close()


def build_manual_prune_plan(
    requested_players: list[Any],
    *,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    protected_players: Iterable[str] | None = None,
    max_players: int = 100,
) -> dict[str, Any]:
    threshold = max(1, min(int(inactive_days or DEFAULT_INACTIVE_DAYS), 3650))
    snapshot = _compute_live_snapshot(inactive_days=threshold)
    current_entries = {
        str(entry["name"]).strip().lower(): entry
        for entry in snapshot.get("entries", [])
        if str(entry.get("name", "")).strip()
    }
    protected_lookup = {
        str(player or "").strip().lower()
        for player in (protected_players or PROTECTED_PLAYERS)
        if str(player or "").strip()
    }

    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for raw_player in requested_players[:max_players]:
        player = str(raw_player or "").strip()
        key = player.lower()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)

        entry = current_entries.get(key)
        if not entry:
            skipped.append({"player": player, "reason": "not_currently_whitelisted"})
            continue

        if key in protected_lookup or entry.get("protected_player"):
            skipped.append({"player": entry["name"], "reason": "protected_player"})
            continue

        if not entry.get("recommended_removal"):
            skipped.append({"player": entry["name"], "reason": "not_inactive_enough"})
            continue

        eligible.append(entry)

    return {
        "inactive_days": threshold,
        "snapshot": snapshot,
        "eligible": eligible,
        "skipped": skipped,
    }


def _insert_lifecycle_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    player_key: str,
    display_name: str,
    occurred_at: str,
    actor_email: str | None = None,
    actor_surface: str = "unknown",
    source: str = "app",
    related_manual_prune_id: int | None = None,
    previous_removed_at: str | None = None,
    previous_last_seen_at: str | None = None,
    days_after_removal: int | None = None,
    days_since_previous_seen: int | None = None,
    note: str | None = None,
) -> bool:
    existing = conn.execute(
        """
        SELECT id
        FROM whitelist_lifecycle_events
        WHERE event_type = ?
          AND player_key = ?
          AND occurred_at = ?
          AND COALESCE(previous_removed_at, '') = COALESCE(?, '')
        LIMIT 1
        """,
        (event_type, player_key, occurred_at, previous_removed_at),
    ).fetchone()
    if existing is not None:
        return False

    conn.execute(
        """
        INSERT INTO whitelist_lifecycle_events (
            event_type,
            player_key,
            display_name,
            occurred_at,
            actor_email,
            actor_surface,
            source,
            related_manual_prune_id,
            previous_removed_at,
            previous_last_seen_at,
            days_after_removal,
            days_since_previous_seen,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            player_key,
            display_name,
            occurred_at,
            str(actor_email or "").strip() or None,
            str(actor_surface or "unknown").strip() or "unknown",
            str(source or "app").strip() or "app",
            related_manual_prune_id,
            previous_removed_at,
            previous_last_seen_at,
            days_after_removal,
            days_since_previous_seen,
            note,
            _now_iso(),
        ),
    )
    return True


def _record_rewhitelist_event_if_needed(
    conn: sqlite3.Connection,
    *,
    player_name: str,
    occurred_at: str,
    actor_email: str | None,
    actor_surface: str,
    source: str,
) -> None:
    player_key = player_name.lower()
    prune_row = _load_latest_manual_prune_row_for_player(conn, player_key=player_key)
    if prune_row is None:
        return

    previous_removed_at = prune_row.get("removed_at")
    if not _is_same_or_after(occurred_at, previous_removed_at):
        return

    _insert_lifecycle_event(
        conn,
        event_type="rewhitelist",
        player_key=player_key,
        display_name=player_name,
        occurred_at=occurred_at,
        actor_email=actor_email,
        actor_surface=actor_surface,
        source=source,
        related_manual_prune_id=prune_row.get("id"),
        previous_removed_at=previous_removed_at,
        previous_last_seen_at=prune_row.get("last_seen_at"),
        days_after_removal=_days_between(occurred_at, previous_removed_at),
        days_since_previous_seen=_days_between(occurred_at, prune_row.get("last_seen_at")),
        note="Player was re-added after an audit-based whitelist removal.",
    )


def record_whitelist_command(
    player: str,
    *,
    event_type: str,
    occurred_at: datetime | None = None,
    actor_email: str | None = None,
    actor_surface: str = "unknown",
    source: str = "app",
) -> None:
    """
    Record an admin-triggered whitelist add/remove immediately in the live overlay.

    This keeps the audit UI responsive even before the next latest.log sync.
    """
    if event_type not in {"whitelist_add", "whitelist_remove"}:
        raise ValueError(f"Unsupported whitelist audit event: {event_type}")

    player_name = str(player or "").strip()
    if not player_name:
        return

    stamp = occurred_at or datetime.now()
    deltas: dict[str, _ActivityDelta] = {}
    _update_delta(deltas, player=player_name, event_type=event_type, occurred_at=stamp)

    with _db_lock:
        conn = _connect()
        try:
            _initialize_db(conn)
            _merge_deltas(conn, source_kind=_SOURCE_LIVE, deltas=deltas)
            if event_type == "whitelist_add":
                _record_rewhitelist_event_if_needed(
                    conn,
                    player_name=player_name,
                    occurred_at=_isoformat(stamp),
                    actor_email=actor_email,
                    actor_surface=actor_surface,
                    source=source,
                )
            conn.commit()
        finally:
            conn.close()


def record_whitelist_add(
    player: str,
    occurred_at: datetime | None = None,
    *,
    actor_email: str | None = None,
    actor_surface: str = "unknown",
    source: str = "app",
) -> None:
    record_whitelist_command(
        player,
        event_type="whitelist_add",
        occurred_at=occurred_at,
        actor_email=actor_email,
        actor_surface=actor_surface,
        source=source,
    )


def record_whitelist_remove(
    player: str,
    occurred_at: datetime | None = None,
    *,
    actor_email: str | None = None,
    actor_surface: str = "unknown",
    source: str = "app",
) -> None:
    record_whitelist_command(
        player,
        event_type="whitelist_remove",
        occurred_at=occurred_at,
        actor_email=actor_email,
        actor_surface=actor_surface,
        source=source,
    )


def record_manual_prune(
    entry: dict[str, Any],
    *,
    actor_email: str | None = None,
    actor_surface: str = "unknown",
    inactive_days_threshold: int | None = None,
    occurred_at: datetime | None = None,
) -> None:
    player_name = str((entry or {}).get("name") or "").strip()
    if not player_name:
        return

    removed_at = _isoformat(occurred_at or datetime.now())
    player_key = player_name.lower()
    threshold = max(
        1,
        min(int(inactive_days_threshold or entry.get("inactive_days_threshold") or DEFAULT_INACTIVE_DAYS), 3650),
    )
    days_since_added = entry.get("days_since_added")
    inactivity_days = entry.get("inactive_days")

    with _db_lock:
        conn = _connect()
        try:
            _initialize_db(conn)
            conn.execute(
                """
                INSERT INTO manual_prune_history (
                    player_key,
                    display_name,
                    removed_at,
                    removed_by,
                    removed_from,
                    inactive_days_threshold,
                    added_at,
                    last_seen_at,
                    days_since_added,
                    inactivity_days,
                    never_joined,
                    recommendation_reason,
                    note,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    player_key,
                    player_name,
                    removed_at,
                    str(actor_email or "").strip() or None,
                    str(actor_surface or "unknown").strip() or "unknown",
                    threshold,
                    entry.get("added_at"),
                    entry.get("last_seen_at"),
                    int(days_since_added) if isinstance(days_since_added, int) else None,
                    int(inactivity_days) if isinstance(inactivity_days, int) else None,
                    1 if entry.get("never_joined") else 0,
                    str(entry.get("recommendation_reason") or "").strip() or None,
                    str(entry.get("note") or "").strip() or None,
                    _now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def sync_index() -> dict[str, Any]:
    return get_whitelist_audit_snapshot(force_refresh=True)


def get_whitelist_activity(inactive_days: int = DEFAULT_INACTIVE_DAYS) -> dict[str, Any]:
    return get_whitelist_audit_snapshot(inactive_days=inactive_days)


class WhitelistAuditService:
    def refresh_index(self, actor_email: str | None = None) -> dict[str, Any]:
        return get_whitelist_audit_snapshot(force_refresh=True, actor_email=actor_email)

    def sync_index(self, actor_email: str | None = None) -> dict[str, Any]:
        return get_whitelist_audit_snapshot(force_refresh=True, actor_email=actor_email)

    def get_current_whitelist(self) -> list[dict[str, Any]]:
        return get_current_whitelist_members()

    def get_audit_snapshot(
        self,
        inactive_days: int = DEFAULT_INACTIVE_DAYS,
        *,
        force_refresh: bool = False,
        actor_email: str | None = None,
        latest_shared: bool = False,
    ) -> dict[str, Any]:
        return get_whitelist_audit_snapshot(
            inactive_days=inactive_days,
            force_refresh=force_refresh,
            actor_email=actor_email,
            latest_shared=latest_shared,
        )

    def get_whitelist_activity(
        self,
        inactive_days: int = DEFAULT_INACTIVE_DAYS,
        *,
        force_refresh: bool = False,
        actor_email: str | None = None,
        latest_shared: bool = False,
    ) -> dict[str, Any]:
        return get_whitelist_audit_snapshot(
            inactive_days=inactive_days,
            force_refresh=force_refresh,
            actor_email=actor_email,
            latest_shared=latest_shared,
        )

    def get_manual_prune_history_page(
        self,
        *,
        limit: int = AUDIT_SECTION_DEFAULT_LIMIT,
        offset: int = 0,
        q: str = "",
        status: str = "all",
        sort: str = "removed_desc",
    ) -> dict[str, Any]:
        return get_manual_prune_history_page(
            limit=limit,
            offset=offset,
            q=q,
            status=status,
            sort=sort,
        )

    def get_long_term_inactivity_page(
        self,
        *,
        threshold_days: int = LONG_TERM_INACTIVE_DAYS,
        limit: int = AUDIT_SECTION_DEFAULT_LIMIT,
        offset: int = 0,
        q: str = "",
        status: str = "all",
        whitelist: str = "all",
        sort: str = "review_desc",
    ) -> dict[str, Any]:
        return get_long_term_inactivity_page(
            threshold_days=threshold_days,
            limit=limit,
            offset=offset,
            q=q,
            status=status,
            whitelist=whitelist,
            sort=sort,
        )

    def build_manual_prune_plan(
        self,
        requested_players: list[Any],
        *,
        inactive_days: int = DEFAULT_INACTIVE_DAYS,
        protected_players: Iterable[str] | None = None,
        max_players: int = 100,
    ) -> dict[str, Any]:
        return build_manual_prune_plan(
            requested_players,
            inactive_days=inactive_days,
            protected_players=protected_players,
            max_players=max_players,
        )

    def record_whitelist_add(
        self,
        player: str,
        occurred_at: datetime | None = None,
        *,
        actor_email: str | None = None,
        actor_surface: str = "unknown",
        source: str = "app",
    ) -> None:
        record_whitelist_add(
            player,
            occurred_at=occurred_at,
            actor_email=actor_email,
            actor_surface=actor_surface,
            source=source,
        )

    def record_whitelist_remove(
        self,
        player: str,
        occurred_at: datetime | None = None,
        *,
        actor_email: str | None = None,
        actor_surface: str = "unknown",
        source: str = "app",
    ) -> None:
        record_whitelist_remove(
            player,
            occurred_at=occurred_at,
            actor_email=actor_email,
            actor_surface=actor_surface,
            source=source,
        )

    def record_manual_prune(
        self,
        entry: dict[str, Any],
        *,
        actor_email: str | None = None,
        actor_surface: str = "unknown",
        inactive_days_threshold: int | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        record_manual_prune(
            entry,
            actor_email=actor_email,
            actor_surface=actor_surface,
            inactive_days_threshold=inactive_days_threshold,
            occurred_at=occurred_at,
        )


whitelist_audit_service = WhitelistAuditService()
