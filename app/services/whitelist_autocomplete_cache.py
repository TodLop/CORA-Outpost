"""Shared cache for Minecraft whitelist autocomplete suggestions."""

from __future__ import annotations

import time
from typing import Iterable


WHITELIST_CACHE_TTL = 300
_whitelist_cache = {"players": [], "last_fetch": 0.0}


def get_fresh_players(now: float | None = None) -> list[str] | None:
    current_time = time.time() if now is None else now
    players = list(_whitelist_cache["players"])
    if players and current_time - float(_whitelist_cache["last_fetch"]) < WHITELIST_CACHE_TTL:
        return players
    return None


def get_stale_players() -> list[str]:
    return list(_whitelist_cache["players"])


def store_players(players: Iterable[str], now: float | None = None) -> list[str]:
    sorted_players = sorted([str(player) for player in players if str(player).strip()], key=str.lower)
    _whitelist_cache["players"] = sorted_players
    _whitelist_cache["last_fetch"] = time.time() if now is None else now
    return list(sorted_players)


def invalidate() -> None:
    _whitelist_cache["players"] = []
    _whitelist_cache["last_fetch"] = 0.0
