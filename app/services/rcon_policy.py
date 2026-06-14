from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RconDecision:
    allowed: bool
    base_command: str
    reason: str = ""


def decide_rcon_command(*, command: str, dangerous_commands: set[str]) -> RconDecision:
    parts = command.split()
    base = parts[0].lstrip("/").lower() if parts else ""
    if base and base in dangerous_commands:
        return RconDecision(allowed=False, base_command=base, reason="dangerous_command")
    return RconDecision(allowed=True, base_command=base)
