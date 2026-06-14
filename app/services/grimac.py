# app/services/grimac.py
"""
GrimAC Violations Database Service

Provides direct access to GrimAC's SQLite database for querying player violation history.
This is more reliable than RCON commands which don't return responses.
"""

import sqlite3
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import uuid as uuid_lib

from app.services import minecraft_settings


# GrimAC violations database path
GRIMAC_DB_PATH = minecraft_settings.get_grimac_db_path()
_DEFAULT_GRIMAC_DB_PATH = GRIMAC_DB_PATH


def _grimac_db_path() -> Path:
    configured = minecraft_settings.get_grimac_db_path()
    current = Path(GRIMAC_DB_PATH)
    if current != _DEFAULT_GRIMAC_DB_PATH and current != configured:
        return current
    return configured


@dataclass
class GrimViolation:
    """Represents a single GrimAC violation record."""
    id: int
    uuid: str
    check_name: str
    verbose: str
    violation_level: int
    created_at: str
    grim_version: str
    client_brand: str
    client_version: str
    server_version: str


def is_database_available() -> bool:
    """Check if the GrimAC database exists and is accessible."""
    return _grimac_db_path().exists()


def _hex_to_uuid(hex_bytes: bytes) -> str:
    """Convert UUID bytes from SQLite to standard UUID string format."""
    if isinstance(hex_bytes, bytes) and len(hex_bytes) == 16:
        # Convert bytes to hex string and format as UUID
        hex_str = hex_bytes.hex()
        return f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"
    return str(hex_bytes)


def _get_connection() -> Optional[sqlite3.Connection]:
    """Get a connection to the GrimAC database."""
    if not is_database_available():
        return None
    try:
        conn = sqlite3.connect(str(_grimac_db_path()), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def get_player_uuid_from_name(player_name: str) -> Optional[str]:
    """
    Get player UUID from their name using the server's usercache.
    Returns None if not found.
    """
    import json
    usercache_path = minecraft_settings.get_usercache_path()
    
    if not usercache_path.exists():
        return None
    
    try:
        with open(usercache_path, "r") as f:
            cache = json.load(f)
        
        for entry in cache:
            if entry.get("name", "").lower() == player_name.lower():
                # Remove dashes from UUID for database lookup
                return entry.get("uuid", "").replace("-", "").upper()
        return None
    except Exception:
        return None


def get_player_violations(
    player_name: str,
    limit: int = 50,
    check_filter: Optional[str] = None
) -> dict:
    """
    Get violation history for a specific player.
    
    Args:
        player_name: The player's Minecraft username
        limit: Maximum number of violations to return
        check_filter: Optional filter for specific check types
    
    Returns:
        Dict with status, violations list, and summary statistics
    """
    if not is_database_available():
        return {
            "success": False,
            "error": "GrimAC database not available"
        }
    
    # Get player UUID
    player_uuid = get_player_uuid_from_name(player_name)
    if not player_uuid:
        return {
            "success": True,
            "player": player_name,
            "violations": [],
            "summary": {
                "total_count": 0,
                "unique_checks": 0,
                "checks_breakdown": {}
            },
            "note": "Player not found in usercache - they may not have joined recently"
        }
    
    conn = _get_connection()
    if not conn:
        return {
            "success": False,
            "error": "Failed to connect to GrimAC database"
        }
    
    try:
        cursor = conn.cursor()
        
        # Convert hex UUID to bytes for query
        uuid_bytes = bytes.fromhex(player_uuid)
        
        # Build query
        base_query = """
            SELECT 
                v.id,
                v.uuid,
                c.check_name_string as check_name,
                v.verbose,
                v.vl as violation_level,
                v.created_at,
                COALESCE(gv.grim_version_string, 'unknown') as grim_version,
                COALESCE(cb.client_brand_string, 'unknown') as client_brand,
                COALESCE(cv.client_version_string, 'unknown') as client_version,
                COALESCE(sv.server_version_string, 'unknown') as server_version
            FROM grim_history_violations v
            JOIN grim_history_check_names c ON v.check_name_id = c.id
            LEFT JOIN grim_history_versions gv ON v.grim_version_id = gv.id
            LEFT JOIN grim_history_client_brands cb ON v.client_brand_id = cb.id
            LEFT JOIN grim_history_client_versions cv ON v.client_version_id = cv.id
            LEFT JOIN grim_history_server_versions sv ON v.server_version_id = sv.id
            WHERE v.uuid = ?
        """
        
        params = [uuid_bytes]
        
        if check_filter:
            base_query += " AND c.check_name_string LIKE ?"
            params.append(f"%{check_filter}%")
        
        base_query += " ORDER BY v.created_at DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(base_query, params)
        rows = cursor.fetchall()
        
        violations = []
        checks_breakdown = {}
        
        for row in rows:
            # Convert timestamp (milliseconds) to readable format
            created_at_ms = row["created_at"]
            created_at_str = datetime.fromtimestamp(created_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
            
            check_name = row["check_name"]
            checks_breakdown[check_name] = checks_breakdown.get(check_name, 0) + 1
            
            violations.append({
                "id": row["id"],
                "check_name": check_name,
                "verbose": row["verbose"] or "",
                "violation_level": row["violation_level"],
                "created_at": created_at_str,
                "client_brand": row["client_brand"],
                "client_version": row["client_version"]
            })
        
        # Get total count for this player
        cursor.execute(
            "SELECT COUNT(*) as total FROM grim_history_violations WHERE uuid = ?",
            [uuid_bytes]
        )
        total_count = cursor.fetchone()["total"]
        
        conn.close()
        
        return {
            "success": True,
            "player": player_name,
            "uuid": player_uuid,
            "violations": violations,
            "summary": {
                "total_count": total_count,
                "showing": len(violations),
                "unique_checks": len(checks_breakdown),
                "checks_breakdown": checks_breakdown
            }
        }
        
    except Exception as e:
        conn.close()
        return {
            "success": False,
            "error": f"Database query error: {str(e)}"
        }


def get_recent_violations(limit: int = 50) -> dict:
    """
    Get the most recent violations across all players.
    
    Args:
        limit: Maximum number of violations to return
    
    Returns:
        Dict with status and violations list
    """
    if not is_database_available():
        return {
            "success": False,
            "error": "GrimAC database not available"
        }
    
    conn = _get_connection()
    if not conn:
        return {
            "success": False,
            "error": "Failed to connect to GrimAC database"
        }
    
    try:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                v.id,
                hex(v.uuid) as uuid_hex,
                c.check_name_string as check_name,
                v.verbose,
                v.vl as violation_level,
                v.created_at
            FROM grim_history_violations v
            JOIN grim_history_check_names c ON v.check_name_id = c.id
            ORDER BY v.created_at DESC
            LIMIT ?
        """, [limit])
        
        rows = cursor.fetchall()
        conn.close()
        
        violations = []
        for row in rows:
            created_at_ms = row["created_at"]
            created_at_str = datetime.fromtimestamp(created_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
            
            violations.append({
                "id": row["id"],
                "uuid": row["uuid_hex"],
                "check_name": row["check_name"],
                "verbose": row["verbose"] or "",
                "violation_level": row["violation_level"],
                "created_at": created_at_str
            })
        
        return {
            "success": True,
            "violations": violations,
            "count": len(violations)
        }
        
    except Exception as e:
        conn.close()
        return {
            "success": False,
            "error": f"Database query error: {str(e)}"
        }


def get_violation_stats() -> dict:
    """
    Get overall violation statistics.
    
    Returns:
        Dict with various statistics about violations
    """
    if not is_database_available():
        return {
            "success": False,
            "error": "GrimAC database not available"
        }
    
    conn = _get_connection()
    if not conn:
        return {
            "success": False,
            "error": "Failed to connect to GrimAC database"
        }
    
    try:
        cursor = conn.cursor()
        
        # Total violations
        cursor.execute("SELECT COUNT(*) as total FROM grim_history_violations")
        total = cursor.fetchone()["total"]
        
        # Unique players
        cursor.execute("SELECT COUNT(DISTINCT uuid) as unique_players FROM grim_history_violations")
        unique_players = cursor.fetchone()["unique_players"]
        
        # Top checks
        cursor.execute("""
            SELECT c.check_name_string as check_name, COUNT(*) as count
            FROM grim_history_violations v
            JOIN grim_history_check_names c ON v.check_name_id = c.id
            GROUP BY c.check_name_string
            ORDER BY count DESC
            LIMIT 10
        """)
        top_checks = [{"check": row["check_name"], "count": row["count"]} for row in cursor.fetchall()]
        
        # Violations in last 24 hours
        cursor.execute("""
            SELECT COUNT(*) as recent
            FROM grim_history_violations
            WHERE created_at > ?
        """, [(datetime.now().timestamp() - 86400) * 1000])
        recent_24h = cursor.fetchone()["recent"]
        
        conn.close()
        
        return {
            "success": True,
            "stats": {
                "total_violations": total,
                "unique_players": unique_players,
                "violations_24h": recent_24h,
                "top_checks": top_checks
            }
        }
        
    except Exception as e:
        conn.close()
        return {
            "success": False,
            "error": f"Database query error: {str(e)}"
        }
