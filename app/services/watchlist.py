# app/services/watchlist.py
"""
Player Watchlist Service

Manages a watchlist of suspicious players with different severity levels.
Admins can add/edit/remove entries; staff can view only.
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, asdict, field
import threading

from app.core.config import DATA_DIR, PROTECTED_PLAYERS

logger = logging.getLogger(__name__)

# Watchlist file path
WATCHLIST_FILE = DATA_DIR / "watchlist.json"

# Thread lock for file operations
_file_lock = threading.Lock()

# Valid watchlist levels
WATCHLIST_LEVELS = frozenset(["suspicious", "high-priority", "confirmed-cheater"])

# Valid watchlist statuses
WATCHLIST_STATUSES = frozenset(["active", "resolved", "false-positive"])

# Valid tags
VALID_TAGS = frozenset([
    "fly-hack", "speed-hack", "x-ray", "kill-aura", "reach-hack",
    "anti-knockback", "auto-clicker", "scaffold", "bhop", "timer",
    "inventory-hack", "duping", "exploiting", "botting", "other"
])


@dataclass
class WatchlistEntry:
    """Represents a watchlist entry for a suspicious player"""
    id: str
    player: str                          # Minecraft username (lowercase)
    level: str                           # suspicious | high-priority | confirmed-cheater
    reason: str                          # Why marked
    evidence_notes: str                  # Evidence details
    added_by: str                        # Admin email
    added_at: str                        # ISO timestamp
    status: str                          # active | resolved | false-positive
    tags: List[str] = field(default_factory=list)
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None


def _load_watchlist() -> dict:
    """Load watchlist from JSON file"""
    if not WATCHLIST_FILE.exists():
        return {"entries": []}

    try:
        with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Error loading watchlist file: %s", e)
        return {"entries": []}


def _save_watchlist(data: dict) -> bool:
    """Save watchlist to JSON file"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(WATCHLIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        logger.error("Error saving watchlist file: %s", e)
        return False


def is_protected_player(player: str) -> bool:
    """Check if player is protected and cannot be watchlisted"""
    return player.lower() in [p.lower() for p in PROTECTED_PLAYERS]


def add_to_watchlist(
    player: str,
    level: str,
    reason: str,
    evidence_notes: str,
    admin_email: str,
    tags: Optional[List[str]] = None
) -> Optional[WatchlistEntry]:
    """
    Add a player to the watchlist.

    Args:
        player: Minecraft username
        level: suspicious | high-priority | confirmed-cheater
        reason: Why the player is being watched
        evidence_notes: Detailed evidence
        admin_email: Admin who added the entry
        tags: List of cheat type tags

    Returns:
        WatchlistEntry if successful, None if validation fails
    """
    # Validate level
    if level not in WATCHLIST_LEVELS:
        return None

    # Check protected players
    if is_protected_player(player):
        return None

    # Validate tags
    clean_tags = []
    if tags:
        for tag in tags:
            if tag in VALID_TAGS:
                clean_tags.append(tag)

    entry = WatchlistEntry(
        id=f"wl_{str(uuid.uuid4())[:8]}",
        player=player.lower(),
        level=level,
        reason=reason,
        evidence_notes=evidence_notes,
        added_by=admin_email,
        added_at=datetime.now().isoformat(),
        status="active",
        tags=clean_tags
    )

    with _file_lock:
        data = _load_watchlist()

        # Check if player already has an active entry
        for existing in data["entries"]:
            if existing.get("player", "").lower() == player.lower() and existing.get("status") == "active":
                return None  # Player already on active watchlist

        data["entries"].append(asdict(entry))
        _save_watchlist(data)

    return entry


def get_watchlist(include_resolved: bool = False) -> List[WatchlistEntry]:
    """
    Get all watchlist entries.

    Args:
        include_resolved: Include resolved/false-positive entries

    Returns:
        List of WatchlistEntry objects, newest first
    """
    with _file_lock:
        data = _load_watchlist()

    entries = []
    for e in data.get("entries", []):
        if not include_resolved and e.get("status") != "active":
            continue
        entries.append(WatchlistEntry(**e))

    # Sort by added_at, newest first
    entries.sort(key=lambda e: e.added_at, reverse=True)
    return entries


def get_watchlist_entry(entry_id: str) -> Optional[WatchlistEntry]:
    """Get a specific watchlist entry by ID"""
    with _file_lock:
        data = _load_watchlist()

    for e in data.get("entries", []):
        if e.get("id") == entry_id:
            return WatchlistEntry(**e)
    return None


def get_watchlist_entry_by_player(player: str, active_only: bool = True) -> Optional[WatchlistEntry]:
    """
    Get watchlist entry for a specific player.

    Args:
        player: Minecraft username
        active_only: Only return active entries

    Returns:
        WatchlistEntry if found, None otherwise
    """
    player_lower = player.lower()

    with _file_lock:
        data = _load_watchlist()

    for e in data.get("entries", []):
        if e.get("player", "").lower() == player_lower:
            if active_only and e.get("status") != "active":
                continue
            return WatchlistEntry(**e)
    return None


def is_watchlisted(player: str) -> bool:
    """Check if a player is on the active watchlist"""
    return get_watchlist_entry_by_player(player, active_only=True) is not None


def update_watchlist_entry(
    entry_id: str,
    admin_email: str,
    level: Optional[str] = None,
    reason: Optional[str] = None,
    evidence_notes: Optional[str] = None,
    tags: Optional[List[str]] = None
) -> Optional[WatchlistEntry]:
    """
    Update a watchlist entry.

    Args:
        entry_id: The entry ID
        admin_email: Admin making the update
        level: New level (optional)
        reason: New reason (optional)
        evidence_notes: New evidence notes (optional)
        tags: New tags (optional)

    Returns:
        Updated WatchlistEntry if successful, None if not found
    """
    if level and level not in WATCHLIST_LEVELS:
        return None

    with _file_lock:
        data = _load_watchlist()

        for e in data["entries"]:
            if e.get("id") == entry_id:
                if level:
                    e["level"] = level
                if reason:
                    e["reason"] = reason
                if evidence_notes:
                    e["evidence_notes"] = evidence_notes
                if tags is not None:
                    clean_tags = [t for t in tags if t in VALID_TAGS]
                    e["tags"] = clean_tags

                e["updated_at"] = datetime.now().isoformat()
                e["updated_by"] = admin_email

                _save_watchlist(data)
                return WatchlistEntry(**e)

    return None


def resolve_watchlist_entry(
    entry_id: str,
    admin_email: str,
    resolution: str,  # resolved | false-positive
    notes: Optional[str] = None
) -> Optional[WatchlistEntry]:
    """
    Resolve a watchlist entry.

    Args:
        entry_id: The entry ID
        admin_email: Admin resolving the entry
        resolution: resolved | false-positive
        notes: Resolution notes

    Returns:
        Resolved WatchlistEntry if successful, None if not found
    """
    if resolution not in ["resolved", "false-positive"]:
        return None

    with _file_lock:
        data = _load_watchlist()

        for e in data["entries"]:
            if e.get("id") == entry_id:
                e["status"] = resolution
                e["resolved_at"] = datetime.now().isoformat()
                e["resolved_by"] = admin_email
                e["resolution_notes"] = notes

                _save_watchlist(data)
                return WatchlistEntry(**e)

    return None


def delete_watchlist_entry(entry_id: str, admin_email: str) -> bool:
    """
    Delete a watchlist entry permanently.

    Args:
        entry_id: The entry ID
        admin_email: Admin deleting (for audit)

    Returns:
        True if deleted, False if not found
    """
    with _file_lock:
        data = _load_watchlist()
        original_count = len(data.get("entries", []))

        data["entries"] = [
            e for e in data.get("entries", [])
            if e.get("id") != entry_id
        ]

        if len(data["entries"]) < original_count:
            _save_watchlist(data)
            return True

    return False


def get_watchlist_by_level(level: str) -> List[WatchlistEntry]:
    """Get all active watchlist entries with a specific level"""
    entries = get_watchlist(include_resolved=False)
    return [e for e in entries if e.level == level]


def get_watchlist_stats() -> dict:
    """Get watchlist statistics"""
    entries = get_watchlist(include_resolved=True)

    active = [e for e in entries if e.status == "active"]

    return {
        "total_active": len(active),
        "suspicious": len([e for e in active if e.level == "suspicious"]),
        "high_priority": len([e for e in active if e.level == "high-priority"]),
        "confirmed_cheaters": len([e for e in active if e.level == "confirmed-cheater"]),
        "resolved": len([e for e in entries if e.status == "resolved"]),
        "false_positives": len([e for e in entries if e.status == "false-positive"])
    }
