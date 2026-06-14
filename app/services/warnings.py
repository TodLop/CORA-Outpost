# app/services/warnings.py
"""
Player Warning System Service

Provides warning/discipline tracking for players.
Stored in JSON file format for simplicity and portability.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, asdict
import threading

from app.core.config import DATA_DIR

# Warnings file path
WARNINGS_FILE = DATA_DIR / "warnings.json"

# Thread lock for file operations
_file_lock = threading.Lock()


@dataclass
class Warning:
    """Represents a player warning record"""
    id: str
    player: str
    issued_by: str  # Staff email
    reason: str
    timestamp: str
    notified: bool = False  # Whether player was notified in-game


def _load_warnings() -> dict:
    """Load warnings from JSON file"""
    if not WARNINGS_FILE.exists():
        return {"warnings": []}

    try:
        with open(WARNINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[Warnings] Error loading warnings file: {e}")
        return {"warnings": []}


def _save_warnings(data: dict) -> bool:
    """Save warnings to JSON file"""
    try:
        # Ensure data directory exists
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        with open(WARNINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        print(f"[Warnings] Error saving warnings file: {e}")
        return False


def issue_warning(player: str, reason: str, staff_email: str) -> Warning:
    """
    Issue a warning to a player.

    Args:
        player: Player's Minecraft username
        reason: Reason for the warning
        staff_email: Email of the staff member issuing the warning

    Returns:
        The created Warning object
    """
    warning = Warning(
        id=str(uuid.uuid4())[:8],  # Short ID for easy reference
        player=player.lower(),  # Normalize to lowercase
        issued_by=staff_email,
        reason=reason,
        timestamp=datetime.now().isoformat(),
        notified=False
    )

    with _file_lock:
        data = _load_warnings()
        data["warnings"].append(asdict(warning))
        _save_warnings(data)

    return warning


def get_player_warnings(player: str) -> List[Warning]:
    """
    Get all warnings for a specific player.

    Args:
        player: Player's Minecraft username

    Returns:
        List of Warning objects for the player, newest first
    """
    player_lower = player.lower()

    with _file_lock:
        data = _load_warnings()

    warnings = [
        Warning(**w) for w in data.get("warnings", [])
        if w.get("player", "").lower() == player_lower
    ]

    # Sort by timestamp, newest first
    warnings.sort(key=lambda w: w.timestamp, reverse=True)

    return warnings


def get_all_warnings(limit: int = 100) -> List[Warning]:
    """
    Get all warnings, newest first.

    Args:
        limit: Maximum number of warnings to return

    Returns:
        List of Warning objects
    """
    with _file_lock:
        data = _load_warnings()

    warnings = [Warning(**w) for w in data.get("warnings", [])]

    # Sort by timestamp, newest first
    warnings.sort(key=lambda w: w.timestamp, reverse=True)

    return warnings[:limit]


def get_warning_by_id(warning_id: str) -> Optional[Warning]:
    """
    Get a specific warning by ID.

    Args:
        warning_id: The warning's unique ID

    Returns:
        Warning object if found, None otherwise
    """
    with _file_lock:
        data = _load_warnings()

    for w in data.get("warnings", []):
        if w.get("id") == warning_id:
            return Warning(**w)

    return None


def delete_warning(warning_id: str, staff_email: str) -> bool:
    """
    Delete a warning by ID.

    Args:
        warning_id: The warning's unique ID
        staff_email: Email of the staff member deleting (for audit)

    Returns:
        True if warning was deleted, False if not found
    """
    with _file_lock:
        data = _load_warnings()
        original_count = len(data.get("warnings", []))

        data["warnings"] = [
            w for w in data.get("warnings", [])
            if w.get("id") != warning_id
        ]

        if len(data["warnings"]) < original_count:
            _save_warnings(data)
            return True

    return False


def mark_warning_notified(warning_id: str) -> bool:
    """
    Mark a warning as notified (player saw it in-game).

    Args:
        warning_id: The warning's unique ID

    Returns:
        True if warning was updated, False if not found
    """
    with _file_lock:
        data = _load_warnings()

        for w in data.get("warnings", []):
            if w.get("id") == warning_id:
                w["notified"] = True
                _save_warnings(data)
                return True

    return False


def get_warning_count(player: str) -> int:
    """
    Get the total number of warnings for a player.

    Args:
        player: Player's Minecraft username

    Returns:
        Number of warnings
    """
    return len(get_player_warnings(player))


def get_escalation_recommendation(player: str) -> Optional[str]:
    """
    Get an escalation recommendation based on warning count.

    Args:
        player: Player's Minecraft username

    Returns:
        Recommendation string, or None if no escalation needed
    """
    count = get_warning_count(player)

    if count >= 5:
        return "Consider permanent ban (5+ warnings)"
    elif count >= 3:
        return "Consider 7-day tempban (3+ warnings)"
    elif count >= 2:
        return "Consider 24-hour tempban (2+ warnings)"

    return None
