# app/services/investigation.py
"""
Investigation Session Service

Manages investigation sessions for watchlisted players.
Staff can only investigate players who are on the watchlist.
All actions are logged to an audit file.
"""

import json
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, asdict, field
import threading

from app.core.config import DATA_DIR, ROOT_DIR
from app.services.watchlist import get_watchlist_entry_by_player, is_watchlisted

logger = logging.getLogger(__name__)

# Investigation sessions file path
INVESTIGATIONS_FILE = DATA_DIR / "investigations.json"

# Audit log file path
LOGS_DIR = ROOT_DIR / "logs"
AUDIT_LOG_FILE = LOGS_DIR / "investigation_audit.log"

# Thread lock for file operations
_file_lock = threading.Lock()

# Configure audit logger
def _setup_audit_logger():
    """Set up dedicated audit logger for investigations"""
    logger = logging.getLogger("investigation_audit")

    if not logger.handlers:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        handler = logging.FileHandler(AUDIT_LOG_FILE, encoding='utf-8')
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    return logger

audit_logger = _setup_audit_logger()

# Valid investigation statuses
INVESTIGATION_STATUSES = frozenset(["active", "completed", "abandoned"])

# Valid recommendations
INVESTIGATION_RECOMMENDATIONS = frozenset(["watch", "warn", "ban", "clear"])


@dataclass
class CommandLog:
    """Represents a command executed during investigation"""
    command: str
    response: str
    timestamp: str
    success: bool


@dataclass
class InvestigationSession:
    """Represents an investigation session for a watchlisted player"""
    id: str
    watchlist_id: str                    # Reference to watchlist entry
    player: str                          # Target player
    staff_email: str                     # Investigator
    started_at: str                      # ISO timestamp
    status: str                          # active | completed | abandoned
    commands_executed: List[dict] = field(default_factory=list)
    ended_at: Optional[str] = None
    findings: Optional[str] = None
    recommendation: Optional[str] = None  # watch | warn | ban | clear


def _load_investigations() -> dict:
    """Load investigations from JSON file"""
    if not INVESTIGATIONS_FILE.exists():
        return {"sessions": []}

    try:
        with open(INVESTIGATIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Error loading file: %s", e)
        return {"sessions": []}


def _save_investigations(data: dict) -> bool:
    """Save investigations to JSON file"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(INVESTIGATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        logger.error("Error saving file: %s", e)
        return False


def _log_audit(action: str, staff_email: str, player: str = None, details: str = None):
    """Log an action to the audit file"""
    parts = [f"action={action}", f"staff={staff_email}"]
    if player:
        parts.append(f"player={player}")
    if details:
        parts.append(f"details={details}")

    audit_logger.info(" | ".join(parts))


def start_investigation(
    player: str,
    staff_email: str,
    is_admin: bool = False
) -> Optional[InvestigationSession]:
    """
    Start an investigation session for a player.

    Staff can only investigate watchlisted players.
    Admins can investigate anyone.

    Args:
        player: Minecraft username
        staff_email: Investigator's email
        is_admin: Whether the user is an admin

    Returns:
        InvestigationSession if successful, None if not allowed
    """
    player_lower = player.lower()

    # Staff can only investigate watchlisted players
    if not is_admin:
        if not is_watchlisted(player_lower):
            _log_audit("start_denied", staff_email, player_lower, "Player not on watchlist")
            return None

    # Get watchlist entry (if exists)
    watchlist_entry = get_watchlist_entry_by_player(player_lower, active_only=True)
    watchlist_id = watchlist_entry.id if watchlist_entry else None

    with _file_lock:
        data = _load_investigations()

        # Check if staff already has an active investigation
        for session in data["sessions"]:
            if session.get("staff_email") == staff_email and session.get("status") == "active":
                return None  # Already has active investigation

        # Create new session
        session = InvestigationSession(
            id=f"inv_{str(uuid.uuid4())[:8]}",
            watchlist_id=watchlist_id,
            player=player_lower,
            staff_email=staff_email,
            started_at=datetime.now().isoformat(),
            status="active",
            commands_executed=[]
        )

        data["sessions"].append(asdict(session))
        _save_investigations(data)

    _log_audit("investigation_started", staff_email, player_lower, f"session_id={session.id}")
    return session


def get_active_investigation(staff_email: str) -> Optional[InvestigationSession]:
    """Get the active investigation for a staff member"""
    with _file_lock:
        data = _load_investigations()

    for s in data.get("sessions", []):
        if s.get("staff_email") == staff_email and s.get("status") == "active":
            return InvestigationSession(**s)

    return None


def get_investigation_by_id(session_id: str) -> Optional[InvestigationSession]:
    """Get an investigation session by ID"""
    with _file_lock:
        data = _load_investigations()

    for s in data.get("sessions", []):
        if s.get("id") == session_id:
            return InvestigationSession(**s)

    return None


def log_command_execution(
    session_id: str,
    command: str,
    response: str,
    success: bool,
    staff_email: str
) -> bool:
    """
    Log a command execution to an investigation session.

    Args:
        session_id: Investigation session ID
        command: Command that was executed
        response: Response from the command
        success: Whether the command succeeded
        staff_email: Must match session owner

    Returns:
        True if logged, False if session not found or not authorized
    """
    with _file_lock:
        data = _load_investigations()

        for s in data["sessions"]:
            if s.get("id") == session_id:
                # Verify ownership
                if s.get("staff_email") != staff_email:
                    return False

                # Only log to active sessions
                if s.get("status") != "active":
                    return False

                cmd_log = {
                    "command": command,
                    "response": response[:2000],  # Truncate long responses
                    "timestamp": datetime.now().isoformat(),
                    "success": success
                }

                s["commands_executed"].append(cmd_log)
                _save_investigations(data)

                # Also log to audit file
                _log_audit(
                    "command_executed",
                    staff_email,
                    s.get("player"),
                    f"cmd={command}, success={success}"
                )
                return True

    return False


def end_investigation(
    session_id: str,
    staff_email: str,
    findings: str,
    recommendation: str
) -> Optional[InvestigationSession]:
    """
    End an investigation session with findings.

    Args:
        session_id: Investigation session ID
        staff_email: Must match session owner
        findings: Investigation findings
        recommendation: watch | warn | ban | clear

    Returns:
        Completed InvestigationSession if successful
    """
    if recommendation not in INVESTIGATION_RECOMMENDATIONS:
        return None

    with _file_lock:
        data = _load_investigations()

        for s in data["sessions"]:
            if s.get("id") == session_id:
                # Verify ownership
                if s.get("staff_email") != staff_email:
                    return None

                # Only end active sessions
                if s.get("status") != "active":
                    return None

                s["status"] = "completed"
                s["ended_at"] = datetime.now().isoformat()
                s["findings"] = findings
                s["recommendation"] = recommendation

                _save_investigations(data)

                _log_audit(
                    "investigation_completed",
                    staff_email,
                    s.get("player"),
                    f"recommendation={recommendation}"
                )

                return InvestigationSession(**s)

    return None


def abandon_investigation(
    session_id: str,
    staff_email: str,
    is_admin: bool = False
) -> bool:
    """
    Abandon an investigation session (without findings).

    Admins can abandon any session.

    Args:
        session_id: Investigation session ID
        staff_email: Requesting user's email
        is_admin: Whether user is admin

    Returns:
        True if abandoned, False if not found or unauthorized
    """
    with _file_lock:
        data = _load_investigations()

        for s in data["sessions"]:
            if s.get("id") == session_id:
                # Check authorization
                if not is_admin and s.get("staff_email") != staff_email:
                    return False

                # Only abandon active sessions
                if s.get("status") != "active":
                    return False

                s["status"] = "abandoned"
                s["ended_at"] = datetime.now().isoformat()

                _save_investigations(data)

                _log_audit(
                    "investigation_abandoned",
                    staff_email,
                    s.get("player"),
                    f"session_id={session_id}"
                )
                return True

    return False


def get_player_investigation_history(player: str) -> List[InvestigationSession]:
    """Get all investigations for a specific player"""
    player_lower = player.lower()

    with _file_lock:
        data = _load_investigations()

    sessions = [
        InvestigationSession(**s) for s in data.get("sessions", [])
        if s.get("player", "").lower() == player_lower
    ]

    # Sort by started_at, newest first
    sessions.sort(key=lambda s: s.started_at, reverse=True)
    return sessions


def get_staff_investigation_history(staff_email: str) -> List[InvestigationSession]:
    """Get all investigations by a specific staff member"""
    with _file_lock:
        data = _load_investigations()

    sessions = [
        InvestigationSession(**s) for s in data.get("sessions", [])
        if s.get("staff_email") == staff_email
    ]

    sessions.sort(key=lambda s: s.started_at, reverse=True)
    return sessions


def get_all_active_investigations() -> List[InvestigationSession]:
    """Get all currently active investigation sessions"""
    with _file_lock:
        data = _load_investigations()

    return [
        InvestigationSession(**s) for s in data.get("sessions", [])
        if s.get("status") == "active"
    ]


def get_recent_investigations(limit: int = 50) -> List[InvestigationSession]:
    """Get recent investigations"""
    with _file_lock:
        data = _load_investigations()

    sessions = [InvestigationSession(**s) for s in data.get("sessions", [])]
    sessions.sort(key=lambda s: s.started_at, reverse=True)
    return sessions[:limit]


def get_investigation_stats() -> dict:
    """Get investigation statistics"""
    with _file_lock:
        data = _load_investigations()

    sessions = data.get("sessions", [])
    completed = [s for s in sessions if s.get("status") == "completed"]

    return {
        "total": len(sessions),
        "active": len([s for s in sessions if s.get("status") == "active"]),
        "completed": len(completed),
        "abandoned": len([s for s in sessions if s.get("status") == "abandoned"]),
        "recommendations": {
            "watch": len([s for s in completed if s.get("recommendation") == "watch"]),
            "warn": len([s for s in completed if s.get("recommendation") == "warn"]),
            "ban": len([s for s in completed if s.get("recommendation") == "ban"]),
            "clear": len([s for s in completed if s.get("recommendation") == "clear"])
        }
    }


async def execute_grimac_history(player: str, session_id: str, staff_email: str) -> dict:
    """
    Execute grimac history command and log to session.

    Args:
        player: Target player
        session_id: Investigation session ID
        staff_email: Staff executing

    Returns:
        Command result dict
    """
    from app.services.minecraft_server import send_command

    command = f"grimac history {player}"
    result = await send_command(command)

    # Log to investigation session
    log_command_execution(
        session_id=session_id,
        command=command,
        response=result.get("response", result.get("error", "No response")),
        success=result.get("success", False),
        staff_email=staff_email
    )

    return result


async def execute_mtrack_check(player: str, session_id: str, staff_email: str) -> dict:
    """
    Execute mtrack check command and log to session.

    Args:
        player: Target player
        session_id: Investigation session ID
        staff_email: Staff executing

    Returns:
        Command result dict
    """
    from app.services.minecraft_server import send_command

    command = f"mtrack check {player}"
    result = await send_command(command)

    # Log to investigation session
    log_command_execution(
        session_id=session_id,
        command=command,
        response=result.get("response", result.get("error", "No response")),
        success=result.get("success", False),
        staff_email=staff_email
    )

    return result
