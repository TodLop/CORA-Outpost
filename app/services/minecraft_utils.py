# app/services/minecraft_utils.py
"""
Shared Minecraft utilities used by both admin and staff routers.

Contains:
- Player name validation
- Username extraction from display names
- Reason/text sanitization for RCON commands
- Player list parsing from RCON responses
- GrimAC report formatting
"""

import re
from collections import defaultdict

# Minecraft username validation: 3-16 chars, alphanumeric + underscore only
PLAYER_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_]{3,16}$')


def extract_username(display_name: str) -> str:
    """
    Extract the actual Minecraft username from a display name that may include
    titles/prefixes like [Dragon_Rider]ExampleAdmin or [VIP]player_name

    Patterns handled:
    - [Title]username -> username
    - [Title] username -> username (with space)
    - username -> username (no title)
    """
    match = re.match(r'(?:\[.*?\]\s*)?([a-zA-Z0-9_]{3,16})$', display_name.strip())
    if match:
        return match.group(1)
    return display_name.strip()


def sanitize_reason(text: str, max_len: int = 100, default: str = "Staff action") -> str:
    """
    Sanitize a reason/message field for safe use in RCON commands.
    Strips control characters, limits length, normalizes whitespace.
    """
    text = text[:max_len]
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = re.sub(r'[^\w\s.,!?\-]', '', text)
    text = ' '.join(text.split())
    return text if text else default


def parse_player_list(rcon_response: str) -> list[str]:
    """
    Parse player names from an RCON /list response.

    Handles format: "There are X of Y players online: player1, player2"
    Returns list of clean usernames (with titles stripped).
    """
    if ":" not in rcon_response:
        return []
    players_part = rcon_response.split(":")[-1].strip()
    if not players_part:
        return []
    return [extract_username(p.strip()) for p in players_part.split(",") if p.strip()]


def format_grimac_report(player: str, result: dict) -> str:
    """
    Format a GrimAC violation result dict into a readable text report.

    Args:
        player: The player name
        result: Dict with 'summary' and 'violations' keys from grimac service

    Returns:
        Formatted string for display
    """
    summary = result.get('summary', {})
    violations = result.get('violations', [])

    if summary.get('total_count', 0) == 0:
        formatted = f"No violations found for {player}"
        if result.get('note'):
            formatted += f"\n({result.get('note')})"
        return formatted

    lines = [
        f"╔══════════════════════════════════════════════════════════════╗",
        f"║  GrimAC History: {player:<43} ║",
        f"╠══════════════════════════════════════════════════════════════╣",
        f"║  Total Violations: {summary.get('total_count', 0):<8} | Showing: {summary.get('showing', 0):<8} | Checks: {summary.get('unique_checks', 0):<3} ║",
        f"╚══════════════════════════════════════════════════════════════╝",
        "",
        "[ Check Breakdown ]"
    ]
    for check, count in sorted(summary.get('checks_breakdown', {}).items(), key=lambda x: -x[1]):
        bar = '█' * min(count, 20)
        lines.append(f"  {check:<15} {count:>4}  {bar}")

    lines.append("")
    lines.append("[ Violations by Date ]")

    # Group violations by date
    by_date = defaultdict(list)
    for v in violations:
        date_part = v['created_at'].split(' ')[0]
        by_date[date_part].append(v)

    for date in sorted(by_date.keys(), reverse=True):
        day_violations = by_date[date]
        lines.append(f"")
        lines.append(f"─── {date} ({len(day_violations)} violations) ───")
        for v in day_violations:
            time_part = v['created_at'].split(' ')[1]
            verbose = v['verbose'][:40] + '...' if len(v['verbose']) > 40 else v['verbose']
            lines.append(f"  {time_part} │ {v['check_name']:<12} VL:{v['violation_level']:<3} │ {verbose}")

    if summary.get('total_count', 0) > summary.get('showing', 0):
        lines.append("")
        lines.append(f"⚠️  Showing {summary.get('showing')} of {summary.get('total_count')} total violations")

    return "\n".join(lines)
