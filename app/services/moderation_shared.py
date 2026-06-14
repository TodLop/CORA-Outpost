from __future__ import annotations

from dataclasses import dataclass

from app.core.config import PROTECTED_PLAYERS
from app.services.minecraft_utils import PLAYER_NAME_PATTERN, extract_username, sanitize_reason


@dataclass(frozen=True)
class ModerationInput:
    player: str
    reason: str


def normalize_player(raw: str) -> str:
    return extract_username((raw or "").strip())


def validate_player_name(player: str) -> tuple[bool, str]:
    if not player:
        return False, "Player name required"
    if not PLAYER_NAME_PATTERN.match(player):
        return False, "Invalid player name. Use 3-16 alphanumeric characters or underscores."
    return True, ""


def is_protected_player(player: str) -> bool:
    lower = player.lower()
    return any(lower == p.lower() for p in PROTECTED_PLAYERS)


def deny_if_protected(*, player: str, allow_protected: bool) -> tuple[bool, str]:
    if allow_protected:
        return True, ""
    if is_protected_player(player):
        return False, f"Cannot act on protected player: {player}"
    return True, ""


def sanitize_moderation_reason(*, reason: str, default: str, max_len: int) -> str:
    return sanitize_reason((reason or "").strip(), max_len=max_len, default=default)
