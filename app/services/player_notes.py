# app/services/player_notes.py
"""
Player Notes Service

Provides persistent note-taking for any player.
Both staff and admins can add/view/edit notes.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, asdict
import threading

from app.core.config import DATA_DIR

# Notes file path
NOTES_FILE = DATA_DIR / "player_notes.json"

# Thread lock for file operations
_file_lock = threading.Lock()


@dataclass
class PlayerNote:
    """Represents a note about a player"""
    id: str
    player: str                     # Minecraft username (lowercase)
    content: str                    # Note content
    author: str                     # Staff/admin email
    author_name: str                # Display name
    created_at: str                 # ISO timestamp
    updated_at: Optional[str] = None
    category: str = "general"       # general | behavior | investigation | ban-related


# Valid note categories
VALID_CATEGORIES = frozenset(["general", "behavior", "investigation", "ban-related"])


def _load_notes() -> dict:
    """Load notes from JSON file"""
    if not NOTES_FILE.exists():
        return {"notes": []}

    try:
        with open(NOTES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[PlayerNotes] Error loading notes file: {e}")
        return {"notes": []}


def _save_notes(data: dict) -> bool:
    """Save notes to JSON file"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(NOTES_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        print(f"[PlayerNotes] Error saving notes file: {e}")
        return False


def add_note(
    player: str,
    content: str,
    author_email: str,
    author_name: str,
    category: str = "general"
) -> Optional[PlayerNote]:
    """
    Add a note about a player.

    Args:
        player: Minecraft username
        content: Note content
        author_email: Staff/admin email
        author_name: Display name
        category: Note category

    Returns:
        PlayerNote if successful, None if validation fails
    """
    if not content or not content.strip():
        return None

    if category not in VALID_CATEGORIES:
        category = "general"

    note = PlayerNote(
        id=f"note_{str(uuid.uuid4())[:8]}",
        player=player.lower(),
        content=content.strip(),
        author=author_email,
        author_name=author_name,
        created_at=datetime.now().isoformat(),
        category=category
    )

    with _file_lock:
        data = _load_notes()
        data["notes"].append(asdict(note))
        _save_notes(data)

    return note


def get_player_notes(player: str, category: Optional[str] = None) -> List[PlayerNote]:
    """
    Get all notes for a specific player.

    Args:
        player: Minecraft username
        category: Filter by category (optional)

    Returns:
        List of PlayerNote objects, newest first
    """
    player_lower = player.lower()

    with _file_lock:
        data = _load_notes()

    notes = []
    for n in data.get("notes", []):
        if n.get("player", "").lower() != player_lower:
            continue
        if category and n.get("category") != category:
            continue
        notes.append(PlayerNote(**n))

    # Sort by created_at, newest first
    notes.sort(key=lambda n: n.created_at, reverse=True)
    return notes


def get_note_by_id(note_id: str) -> Optional[PlayerNote]:
    """Get a specific note by ID"""
    with _file_lock:
        data = _load_notes()

    for n in data.get("notes", []):
        if n.get("id") == note_id:
            return PlayerNote(**n)
    return None


def update_note(
    note_id: str,
    author_email: str,
    content: Optional[str] = None,
    category: Optional[str] = None
) -> Optional[PlayerNote]:
    """
    Update a note. Only the original author can edit.

    Args:
        note_id: Note ID
        author_email: Must match original author
        content: New content (optional)
        category: New category (optional)

    Returns:
        Updated PlayerNote if successful, None if not found or unauthorized
    """
    with _file_lock:
        data = _load_notes()

        for n in data["notes"]:
            if n.get("id") == note_id:
                # Only author can edit their own notes
                if n.get("author") != author_email:
                    return None

                if content and content.strip():
                    n["content"] = content.strip()
                if category and category in VALID_CATEGORIES:
                    n["category"] = category

                n["updated_at"] = datetime.now().isoformat()
                _save_notes(data)
                return PlayerNote(**n)

    return None


def delete_note(note_id: str, author_email: str, is_admin: bool = False) -> bool:
    """
    Delete a note. Users can only delete their own notes; admins can delete any.

    Args:
        note_id: Note ID
        author_email: Requesting user's email
        is_admin: Whether the user is an admin

    Returns:
        True if deleted, False if not found or unauthorized
    """
    with _file_lock:
        data = _load_notes()
        original_count = len(data.get("notes", []))

        new_notes = []
        for n in data.get("notes", []):
            if n.get("id") == note_id:
                # Check permission
                if not is_admin and n.get("author") != author_email:
                    return False  # Not authorized
                continue  # Skip (delete) this note
            new_notes.append(n)

        if len(new_notes) < original_count:
            data["notes"] = new_notes
            _save_notes(data)
            return True

    return False


def get_all_notes(limit: int = 100) -> List[PlayerNote]:
    """
    Get all notes across all players.

    Args:
        limit: Maximum number of notes to return

    Returns:
        List of PlayerNote objects, newest first
    """
    with _file_lock:
        data = _load_notes()

    notes = [PlayerNote(**n) for n in data.get("notes", [])]
    notes.sort(key=lambda n: n.created_at, reverse=True)
    return notes[:limit]


def get_notes_by_author(author_email: str) -> List[PlayerNote]:
    """Get all notes by a specific author"""
    with _file_lock:
        data = _load_notes()

    notes = [
        PlayerNote(**n) for n in data.get("notes", [])
        if n.get("author") == author_email
    ]
    notes.sort(key=lambda n: n.created_at, reverse=True)
    return notes


def search_notes(query: str, limit: int = 50) -> List[PlayerNote]:
    """
    Search notes by content or player name.

    Args:
        query: Search string
        limit: Maximum results

    Returns:
        Matching PlayerNote objects
    """
    query_lower = query.lower()

    with _file_lock:
        data = _load_notes()

    notes = []
    for n in data.get("notes", []):
        if (query_lower in n.get("content", "").lower() or
            query_lower in n.get("player", "").lower()):
            notes.append(PlayerNote(**n))

    notes.sort(key=lambda n: n.created_at, reverse=True)
    return notes[:limit]


def get_note_count_for_player(player: str) -> int:
    """Get the count of notes for a player"""
    return len(get_player_notes(player))


def get_notes_stats() -> dict:
    """Get notes statistics"""
    with _file_lock:
        data = _load_notes()

    notes = data.get("notes", [])
    players = set(n.get("player", "") for n in notes)

    return {
        "total_notes": len(notes),
        "players_with_notes": len(players),
        "by_category": {
            "general": len([n for n in notes if n.get("category") == "general"]),
            "behavior": len([n for n in notes if n.get("category") == "behavior"]),
            "investigation": len([n for n in notes if n.get("category") == "investigation"]),
            "ban_related": len([n for n in notes if n.get("category") == "ban-related"])
        }
    }
