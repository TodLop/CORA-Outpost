# app/services/coreprotect.py
"""
CoreProtect Database Query Service

Provides read-only access to CoreProtect's SQLite database for grief investigation.
Staff can query block changes but CANNOT rollback or modify data.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass

from app.services import minecraft_settings

# CoreProtect database path
COREPROTECT_DB = minecraft_settings.get_coreprotect_db_path()
_DEFAULT_COREPROTECT_DB = COREPROTECT_DB


def _coreprotect_db_path() -> Path:
    configured = minecraft_settings.get_coreprotect_db_path()
    current = Path(COREPROTECT_DB)
    if current != _DEFAULT_COREPROTECT_DB and current != configured:
        return current
    return configured

# Maximum days to look back (security limit)
MAX_LOOKUP_DAYS = 7

# Maximum results per query (performance limit)
MAX_RESULTS = 100


@dataclass
class BlockChange:
    """Represents a single block change record"""
    id: int
    timestamp: str
    player: str
    action: str  # 'break', 'place', 'interact', 'kill', 'container'
    world: str
    x: int
    y: int
    z: int
    block_type: str
    data: Optional[str] = None


# Action type mapping (CoreProtect uses integers)
ACTION_TYPES = {
    0: "break",
    1: "place",
    2: "interact",
    3: "kill",
}


def get_db_connection():
    """Get a read-only connection to the CoreProtect database"""
    db_path = _coreprotect_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"CoreProtect database not found at {db_path}")

    # Open in read-only mode for safety
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_user_id(conn, username: str) -> Optional[int]:
    """Get CoreProtect user ID from username"""
    cursor = conn.execute(
        "SELECT rowid FROM co_user WHERE user = ? COLLATE NOCASE",
        (username,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_user_name(conn, user_id: int) -> str:
    """Get username from CoreProtect user ID"""
    cursor = conn.execute(
        "SELECT user FROM co_user WHERE rowid = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else f"Unknown({user_id})"


def get_world_name(conn, world_id: int) -> str:
    """Get world name from CoreProtect world ID"""
    cursor = conn.execute(
        "SELECT world FROM co_world WHERE rowid = ?",
        (world_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else f"Unknown({world_id})"


def get_material_name(conn, material_id: int) -> str:
    """Get material name from CoreProtect material ID"""
    cursor = conn.execute(
        "SELECT material FROM co_material_map WHERE rowid = ?",
        (material_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else f"Unknown({material_id})"


def lookup_by_player(username: str, limit: int = MAX_RESULTS) -> List[BlockChange]:
    """
    Look up recent block changes by a specific player.

    Args:
        username: Minecraft player name
        limit: Maximum number of results (capped at MAX_RESULTS)

    Returns:
        List of BlockChange records, newest first
    """
    limit = min(limit, MAX_RESULTS)
    results = []

    # Calculate time limit (7 days ago)
    time_limit = int((datetime.now() - timedelta(days=MAX_LOOKUP_DAYS)).timestamp())

    try:
        conn = get_db_connection()

        # Get user ID
        user_id = get_user_id(conn, username)
        if not user_id:
            conn.close()
            return []

        # Query block changes
        cursor = conn.execute("""
            SELECT rowid, time, user, action, wid, x, y, z, type, data
            FROM co_block
            WHERE user = ? AND time > ?
            ORDER BY time DESC
            LIMIT ?
        """, (user_id, time_limit, limit))

        for row in cursor:
            results.append(BlockChange(
                id=row["rowid"],
                timestamp=datetime.fromtimestamp(row["time"]).strftime("%Y-%m-%d %H:%M:%S"),
                player=username,
                action=ACTION_TYPES.get(row["action"], f"action_{row['action']}"),
                world=get_world_name(conn, row["wid"]),
                x=row["x"],
                y=row["y"],
                z=row["z"],
                block_type=get_material_name(conn, row["type"]),
                data=str(row["data"]) if row["data"] else None
            ))

        conn.close()

    except FileNotFoundError:
        return []
    except sqlite3.Error as e:
        print(f"[CoreProtect] Database error: {e}")
        return []

    return results


def lookup_by_coordinates(
    x: int,
    y: int,
    z: int,
    radius: int = 5,
    limit: int = MAX_RESULTS
) -> List[BlockChange]:
    """
    Look up recent block changes near specific coordinates.

    Args:
        x, y, z: Center coordinates
        radius: Search radius (default 5 blocks, max 10)
        limit: Maximum number of results (capped at MAX_RESULTS)

    Returns:
        List of BlockChange records, newest first
    """
    limit = min(limit, MAX_RESULTS)
    radius = min(radius, 10)  # Cap radius for performance
    results = []

    # Calculate time limit (7 days ago)
    time_limit = int((datetime.now() - timedelta(days=MAX_LOOKUP_DAYS)).timestamp())

    try:
        conn = get_db_connection()

        # Query block changes within radius
        cursor = conn.execute("""
            SELECT rowid, time, user, action, wid, x, y, z, type, data
            FROM co_block
            WHERE x BETWEEN ? AND ?
              AND y BETWEEN ? AND ?
              AND z BETWEEN ? AND ?
              AND time > ?
            ORDER BY time DESC
            LIMIT ?
        """, (
            x - radius, x + radius,
            y - radius, y + radius,
            z - radius, z + radius,
            time_limit,
            limit
        ))

        for row in cursor:
            results.append(BlockChange(
                id=row["rowid"],
                timestamp=datetime.fromtimestamp(row["time"]).strftime("%Y-%m-%d %H:%M:%S"),
                player=get_user_name(conn, row["user"]),
                action=ACTION_TYPES.get(row["action"], f"action_{row['action']}"),
                world=get_world_name(conn, row["wid"]),
                x=row["x"],
                y=row["y"],
                z=row["z"],
                block_type=get_material_name(conn, row["type"]),
                data=str(row["data"]) if row["data"] else None
            ))

        conn.close()

    except FileNotFoundError:
        return []
    except sqlite3.Error as e:
        print(f"[CoreProtect] Database error: {e}")
        return []

    return results


def is_database_available() -> bool:
    """Check if CoreProtect database is available"""
    return _coreprotect_db_path().exists()
