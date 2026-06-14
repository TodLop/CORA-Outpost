# app/routers/staff.py
"""
Staff Panel Routes

Limited access for server staff members (STAFF_EMAILS).
Allows: start, restart, view status, tempban, kick, broadcast, logs, whitelist, warnings.
Denies: stop, console, plugin updates, CoreProtect rollback.
"""

import re
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import TEMPLATES_DIR, PROTECTED_PLAYERS, DATA_DIR, STAFF_MINECRAFT_IDS
from app.core.deployment_identity import get_public_site_identity
from app.services.minecraft_utils import (
    PLAYER_NAME_PATTERN, extract_username, sanitize_reason,
    parse_player_list, format_grimac_report,
)
from app.services.moderation_shared import (
    deny_if_protected,
    normalize_player,
    sanitize_moderation_reason,
    validate_player_name,
)
from app.services import minecraft_settings
from app.services import minecraft_capabilities

# Audit logger for staff actions
audit_logger = logging.getLogger("staff_audit")
audit_logger.setLevel(logging.INFO)

# Create file handler if not already exists
if not audit_logger.handlers:
    # Ensure logs directory exists
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    handler = logging.FileHandler(logs_dir / "staff_audit.log")
    handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    audit_logger.addHandler(handler)
from app.core.auth import require_staff, require_permission, is_admin
from app.core.minecraft_access import is_minecraft_admin_user
from app.services import minecraft_server
from app.services import permissions as permissions_service
from app.services.whitelist_audit import whitelist_audit_service

router = APIRouter(prefix="/minecraft/staff", tags=["Staff"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_SPECIAL_CASE_STAFF_MC_BINDINGS: dict[str, str] = {}
_GAMEMODE_NOTICE_TEXT = {
    "spectator": "[CORA] Your game mode is now SPECTATOR.",
    "survival": "[CORA] Your game mode is now SURVIVAL.",
}
_VANISH_TOGGLE_COMMAND_TEMPLATE = os.getenv("STAFF_VANISH_TOGGLE_COMMAND", "vanish {player}")
_VANISH_NOTICE_TEXT = "[CORA] Vanish toggle command executed."
_TP_NOTICE_TEMPLATE = "[CORA] Teleported to {target} while in spectator mode."
_WHITELIST_AUDIT_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_.]{1,32}$")


def _get_hardcoded_staff_minecraft_name(staff_email: str) -> Optional[str]:
    """
    Resolve the trusted Minecraft username for a staff account from hardcoded config.
    Returns None when missing or invalid.
    """
    email = (staff_email or "").strip().lower()
    if not email:
        return None

    minecraft_name = str(STAFF_MINECRAFT_IDS.get(email, "")).strip()
    if not minecraft_name:
        return None

    # Normal policy: vanilla Minecraft username format only.
    # Single approved exception for proxy-only account.
    special_case_name = _SPECIAL_CASE_STAFF_MC_BINDINGS.get(email)
    if PLAYER_NAME_PATTERN.match(minecraft_name):
        return minecraft_name

    if special_case_name == minecraft_name:
        return minecraft_name

    logging.getLogger(__name__).warning(
        "Invalid hardcoded Minecraft name for staff email: %s",
        email,
    )
    return None


def _blocked_quick_toggle_security_response(*, staff_email: str, action: str) -> JSONResponse:
    audit_logger.warning(
        "BLOCKED | staff=%s | action=%s | reason=missing_or_invalid_hardcoded_minecraft_id",
        staff_email,
        action,
    )
    return JSONResponse(
        {
            "success": False,
            "error": "Security policy: hardcoded staff Minecraft ID not configured.",
        },
        status_code=403,
    )


def _format_vanish_toggle_command(staff_mc_name: str) -> str:
    template = (_VANISH_TOGGLE_COMMAND_TEMPLATE or "").strip()
    if not template:
        template = "vanish {player}"
    if "{player}" in template:
        return template.format(player=staff_mc_name)
    return f"{template} {staff_mc_name}"


def _extract_response_text(result: dict) -> str:
    return str(result.get("response", "")).strip()


def _response_looks_like_command_failure(response_text: str) -> bool:
    text = (response_text or "").lower()
    if not text:
        return False
    failure_signals = (
        "unknown or incomplete command",
        "unknown command",
        "incorrect argument",
        "no player was found",
        "could not be found",
        "you do not have permission",
    )
    return any(signal in text for signal in failure_signals)


def _active_profile_summary() -> dict:
    """Return staff-safe active profile metadata without local filesystem paths."""
    profile = minecraft_settings.get_active_profile()
    return {
        "id": profile["id"],
        "name": profile["name"],
        "kind": profile["kind"],
        "operations_enabled": bool(profile.get("operations_enabled")),
        "rcon_enabled": bool(profile.get("rcon_enabled")),
        "readonly": bool(profile.get("readonly")),
    }


async def _send_staff_notice(*, staff_email: str, staff_mc_name: str, action: str, notice_text: str) -> bool:
    notify_result = await minecraft_server.send_command(f"msg {staff_mc_name} {notice_text}")
    notification_sent = bool(notify_result.get("success"))
    if not notification_sent:
        audit_logger.warning(
            "WARN | staff=%s | action=%s_notify | mc_name=%s | error=%s",
            staff_email,
            action,
            staff_mc_name,
            notify_result.get("error", "unknown"),
        )
    return notification_sent


async def _get_online_player_names() -> tuple[list[str], Optional[str]]:
    status = minecraft_server.get_server_status()
    if not status.running:
        return [], "Server offline"

    try:
        result = await minecraft_server.send_command("list")
    except Exception as exc:
        return [], str(exc)

    if not result.get("success"):
        return [], result.get("error", "Failed to query online players")

    response_text = _extract_response_text(result)
    if not response_text:
        return [], None

    return parse_player_list(response_text), None


async def _get_cached_online_player_names() -> tuple[list[str], Optional[str]]:
    snapshot = minecraft_server.get_online_players_snapshot()
    if not snapshot.get("running"):
        return [], "Server offline"

    if snapshot.get("error") and not snapshot.get("players"):
        return [], snapshot.get("error", "Failed to query online players")

    return list(snapshot.get("players", [])), None


def _response_indicates_missing_entity(response_text: str) -> bool:
    lowered = (response_text or "").lower()
    return "no entity was found" in lowered or "could not be found" in lowered


def _find_online_player_name(player_name: str, online_players: list[str]) -> Optional[str]:
    lookup = {player.lower(): player for player in online_players}
    return lookup.get((player_name or "").strip().lower())


async def _is_staff_entity_online(staff_mc_name: str) -> tuple[Optional[bool], str]:
    command = f"data get entity {staff_mc_name} UUID"
    result = await minecraft_server.send_command(command)
    if not result.get("success"):
        return None, result.get("error", "Failed to verify player entity")

    response_text = _extract_response_text(result)
    if _response_indicates_missing_entity(response_text):
        return False, response_text

    if _response_looks_like_command_failure(response_text):
        return None, response_text or "Failed to verify player entity"

    if response_text:
        return True, response_text

    return None, "Unexpected entity check response"


async def _ensure_staff_online(
    *,
    staff_email: str,
    staff_mc_name: str,
    action: str,
    online_players: Optional[list[str]] = None,
    require_online_players: bool = False,
) -> tuple[Optional[str], list[str], Optional[JSONResponse]]:
    players = list(online_players) if online_players is not None else []
    players_loaded = online_players is not None

    async def _load_online_players_if_needed() -> Optional[JSONResponse]:
        nonlocal players, players_loaded
        if players_loaded:
            return None

        players, list_error = await _get_online_player_names()
        players_loaded = True

        if list_error == "Server offline":
            return JSONResponse(
                {"success": False, "error": "Server is offline"},
                status_code=503,
            )
        if list_error:
            return JSONResponse(
                {"success": False, "error": list_error},
                status_code=500,
            )
        return None

    entity_online, entity_detail = await _is_staff_entity_online(staff_mc_name)
    if entity_online is None:
        lowered = (entity_detail or "").lower()
        if "server is not running" in lowered:
            audit_logger.warning(
                "BLOCKED | staff=%s | action=%s | reason=server_offline",
                staff_email,
                action,
            )
            return None, [], JSONResponse(
                {"success": False, "error": "Server is offline"},
                status_code=503,
            )

        # Fallback for environments where `data get entity` is not reliable.
        fallback_error = await _load_online_players_if_needed()
        if fallback_error:
            return None, [], fallback_error
        online_name = _find_online_player_name(staff_mc_name, players)
        if online_name:
            audit_logger.warning(
                "WARN | staff=%s | action=%s | reason=staff_online_via_player_list_fallback | mc_name=%s | detail=%s",
                staff_email,
                action,
                staff_mc_name,
                entity_detail,
            )
            return online_name, players, None

        audit_logger.warning(
            "BLOCKED | staff=%s | action=%s | reason=staff_online_check_failed | detail=%s",
            staff_email,
            action,
            entity_detail,
        )
        return None, players, JSONResponse(
            {
                "success": False,
                "error": "Could not verify your bound Minecraft account status. Please try again.",
            },
            status_code=500,
        )

    if not entity_online:
        # Some proxy/non-vanilla usernames can fail entity targeting. Trust /list as fallback.
        fallback_error = await _load_online_players_if_needed()
        if fallback_error:
            return None, [], fallback_error
        online_name = _find_online_player_name(staff_mc_name, players)
        if online_name:
            audit_logger.warning(
                "WARN | staff=%s | action=%s | reason=staff_online_via_list_after_entity_miss | mc_name=%s",
                staff_email,
                action,
                staff_mc_name,
            )
            return online_name, players, None
        audit_logger.warning(
            "BLOCKED | staff=%s | action=%s | reason=staff_not_online | mc_name=%s",
            staff_email,
            action,
            staff_mc_name,
        )
        return None, players, JSONResponse(
            {
                "success": False,
                "error": "Join the server with your bound Minecraft account before using this action.",
            },
            status_code=409,
        )

    if require_online_players:
        list_error_response = await _load_online_players_if_needed()
        if list_error_response:
            return None, [], list_error_response

    online_name = _find_online_player_name(staff_mc_name, players) if players else None
    if online_name:
        return online_name, players, None

    # Fallback to hardcoded name when entity check confirms online but /list parsing missed it.
    # This avoids false negatives from plugin/localized /list formats.
    if require_online_players:
        audit_logger.warning(
            "WARN | staff=%s | action=%s | reason=staff_missing_from_player_list_after_entity_online | mc_name=%s",
            staff_email,
            action,
            staff_mc_name,
        )
    return staff_mc_name, players, None


async def _is_staff_in_spectator_mode(staff_mc_name: str) -> tuple[Optional[bool], str]:
    check_command = f"data get entity {staff_mc_name} playerGameType"
    result = await minecraft_server.send_command(check_command)
    if not result.get("success"):
        return None, result.get("error", "Failed to verify game mode")

    response_text = _extract_response_text(result)
    lowered = response_text.lower()
    if _response_indicates_missing_entity(response_text):
        return False, response_text

    mode_match = re.search(r"playergametype[^0-9-]*(-?\d+)", lowered)
    if mode_match:
        return mode_match.group(1) == "3", response_text

    trailing_number_match = re.search(r"(-?\d+)\s*$", lowered)
    if trailing_number_match:
        return trailing_number_match.group(1) == "3", response_text

    return None, response_text or "Unexpected game mode check response"


async def _switch_staff_gamemode(*, mode: str, user_info: dict) -> JSONResponse:
    staff_email = user_info.get("email", "unknown")
    staff_mc_name = _get_hardcoded_staff_minecraft_name(staff_email)

    if not staff_mc_name:
        return _blocked_quick_toggle_security_response(
            staff_email=staff_email,
            action=f"gamemode_{mode}",
        )

    staff_mc_name, _, online_check_failure = await _ensure_staff_online(
        staff_email=staff_email,
        staff_mc_name=staff_mc_name,
        action=f"gamemode_{mode}",
    )
    if online_check_failure:
        return online_check_failure

    command = f"gamemode {mode} {staff_mc_name}"
    result = await minecraft_server.send_command(command)

    if not result.get("success"):
        return JSONResponse(
            {
                "success": False,
                "error": result.get("error", "Failed to switch game mode"),
            },
            status_code=500,
        )

    audit_logger.info(
        "SUCCESS | staff=%s | action=gamemode_%s | mc_name=%s",
        staff_email,
        mode,
        staff_mc_name,
    )

    notice_text = _GAMEMODE_NOTICE_TEXT.get(mode, f"[CORA] Your game mode is now {mode.upper()}.")
    notification_sent = await _send_staff_notice(
        staff_email=staff_email,
        staff_mc_name=staff_mc_name,
        action=f"gamemode_{mode}",
        notice_text=notice_text,
    )

    return JSONResponse(
        {
            "success": True,
            "mode": mode,
            "minecraft_name": staff_mc_name,
            "message": f"Switched {staff_mc_name} to {mode} mode",
            "response": result.get("response", ""),
            "notification_sent": notification_sent,
        }
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def staff_minecraft_dashboard(request: Request, user_info: dict = Depends(require_staff)):
    """Staff Minecraft dashboard with limited controls"""
    if is_minecraft_admin_user(user_info):
        return RedirectResponse(url="/minecraft/admin", status_code=303)

    server_status = minecraft_server.get_server_status()

    # Get online players list if server is running
    online_players = []
    if server_status.running:
        players, list_error = await _get_online_player_names()
        online_players = players
        if list_error and list_error != "Server offline":
            logging.getLogger(__name__).warning(f"Error getting player list: {list_error}")

    # RBAC: get permissions and visible modules for template rendering
    staff_email = user_info.get("email", "")
    user_is_admin = is_admin(user_info)
    staff_mc_name = _get_hardcoded_staff_minecraft_name(staff_email)
    staff_minecraft_online = False
    if staff_mc_name:
        online_result, _ = await _is_staff_entity_online(staff_mc_name)
        if online_result is True:
            staff_minecraft_online = True
        else:
            staff_minecraft_online = bool(_find_online_player_name(staff_mc_name, online_players))
    if user_is_admin:
        user_permissions = sorted(permissions_service.ALL_PERMISSIONS)
        visible_modules = sorted(set(
            m["module"] for m in permissions_service.PERMISSION_METADATA.values()
        ))
    else:
        user_permissions = sorted(permissions_service.get_effective_permissions(staff_email))
        visible_modules = permissions_service.get_user_visible_modules(staff_email)

    bluemap_capability = minecraft_capabilities.get_capability(
        minecraft_capabilities.BLUEMAP_PROXY,
        request,
    ).to_dict()

    return templates.TemplateResponse("staff/minecraft.html", {
        "request": request,
        "user_info": user_info,
        "is_admin": user_is_admin,
        "server_status": server_status,
        "online_players": online_players,
        "protected_players": list(PROTECTED_PLAYERS),
        "user_permissions": user_permissions,
        "visible_modules": visible_modules,
        "staff_minecraft_name": staff_mc_name,
        "has_hardcoded_minecraft_name": bool(staff_mc_name),
        "staff_minecraft_online": staff_minecraft_online,
        "active_profile": _active_profile_summary(),
        "minecraft_server_address": get_public_site_identity().nearoutpost_server_address,
        "bluemap_capability": bluemap_capability,
    })


@router.get("/api/minecraft/status")
async def get_staff_server_status(user_info: dict = Depends(require_permission("status:view"))):
    """Get server status for staff panel"""
    status = minecraft_server.get_server_status()
    can_view_players = permissions_service.has_permission(user_info.get("email", ""), "players:view")
    return JSONResponse({
        "status": "ok",
        "server": {
            "running": status.running,
            "process_running": status.process_running,
            "healthy": status.healthy,
            "state_reason": status.state_reason,
            "pid": status.pid,
            "game_port_listening": status.game_port_listening,
            "rcon_port_listening": status.rcon_port_listening,
            "players_online": status.players_online,
            "max_players": status.max_players,
            "players": (status.players or []) if can_view_players else [],
            "stale": getattr(status, "stale", False),
            "last_updated": getattr(status, "last_updated", None),
        },
        "active_profile": _active_profile_summary(),
    })


@router.get("/api/minecraft/players")
async def get_online_players(user_info: dict = Depends(require_permission("players:view"))):
    """Get list of online players"""
    players, list_error = await _get_cached_online_player_names()
    if list_error == "Server offline":
        return JSONResponse({"status": "ok", "players": [], "message": "Server offline"})
    if list_error:
        return JSONResponse({"status": "error", "error": list_error}, status_code=500)
    return JSONResponse({"status": "ok", "players": players, "count": len(players)})


@router.get("/api/minecraft/quick-toggle/players")
async def get_quick_toggle_online_players(
    user_info: dict = Depends(require_permission("spectator:quick_toggle"))
):
    """Get online players for quick spectator TP controls."""
    staff_email = user_info.get("email", "")
    staff_mc_name = _get_hardcoded_staff_minecraft_name(staff_email)
    players, list_error = await _get_online_player_names()
    if list_error == "Server offline":
        return JSONResponse({
            "status": "ok",
            "server_running": False,
            "staff_online": False,
            "has_hardcoded_minecraft_name": bool(staff_mc_name),
            "players": [],
            "message": "Server offline",
        })
    if list_error:
        return JSONResponse({"status": "error", "error": list_error}, status_code=500)
    staff_online = False
    if staff_mc_name:
        online_result, _ = await _is_staff_entity_online(staff_mc_name)
        if online_result is True:
            staff_online = True
        else:
            staff_online = bool(_find_online_player_name(staff_mc_name, players))
    return JSONResponse({
        "status": "ok",
        "server_running": True,
        "staff_online": staff_online,
        "has_hardcoded_minecraft_name": bool(staff_mc_name),
        "players": players,
        "count": len(players),
    })


@router.post("/api/minecraft/server/start")
async def staff_start_server(request: Request, user_info: dict = Depends(require_permission("server:start"))):
    """Start the Minecraft server (staff access)"""
    staff_email = user_info.get("email", "unknown")
    from app.services.audit_log import audit_event
    audit_event(logger=audit_logger, actor=staff_email, action="server_start", result="requested")
    from app.services.operations import execute_operation
    result = await execute_operation(
        key="server:start",
        user_info=user_info,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    if result.get("success"):
        audit_event(logger=audit_logger, actor=staff_email, action="server_start", result="success")
    else:
        audit_logger.warning("server_start_failed")
    return JSONResponse(result)


@router.post("/api/minecraft/server/restart")
async def staff_restart_server(request: Request, user_info: dict = Depends(require_permission("server:restart"))):
    """Restart the Minecraft server (staff access)"""
    staff_email = user_info.get("email", "unknown")
    # Permission already enforced by require_permission dependency
    from app.services.audit_log import audit_event
    audit_event(logger=audit_logger, actor=staff_email, action="server_restart", result="requested")
    from app.services.operations import execute_operation
    result = await execute_operation(
        key="server:restart",
        user_info=user_info,
        params={"source": "staff_ui"},
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    if result.get("success"):
        audit_event(logger=audit_logger, actor=staff_email, action="server_restart", result="success")
    else:
        audit_logger.warning("server_restart_failed")
    return JSONResponse(result)


# NOTE: No stop endpoint for staff!


@router.post("/api/minecraft/gamemode/spectator")
async def staff_set_own_spectator_mode(
    user_info: dict = Depends(require_permission("spectator:quick_toggle"))
):
    """Set the staff member's own game mode to spectator."""
    return await _switch_staff_gamemode(mode="spectator", user_info=user_info)


@router.post("/api/minecraft/gamemode/survival")
async def staff_set_own_survival_mode(
    user_info: dict = Depends(require_permission("spectator:quick_toggle"))
):
    """Set the staff member's own game mode to survival."""
    return await _switch_staff_gamemode(mode="survival", user_info=user_info)


@router.post("/api/minecraft/vanish/toggle")
async def staff_toggle_own_vanish(
    user_info: dict = Depends(require_permission("spectator:quick_toggle"))
):
    """Toggle vanish mode for the staff member's own hardcoded Minecraft account."""
    staff_email = user_info.get("email", "unknown")
    staff_mc_name = _get_hardcoded_staff_minecraft_name(staff_email)
    if not staff_mc_name:
        return _blocked_quick_toggle_security_response(
            staff_email=staff_email,
            action="vanish_toggle",
        )

    staff_mc_name, _, online_check_failure = await _ensure_staff_online(
        staff_email=staff_email,
        staff_mc_name=staff_mc_name,
        action="vanish_toggle",
    )
    if online_check_failure:
        return online_check_failure

    command = _format_vanish_toggle_command(staff_mc_name)
    result = await minecraft_server.send_command(command)
    if not result.get("success"):
        return JSONResponse(
            {"success": False, "error": result.get("error", "Failed to toggle vanish")},
            status_code=500,
        )

    response_text = _extract_response_text(result)
    if _response_looks_like_command_failure(response_text):
        return JSONResponse(
            {"success": False, "error": response_text or "Vanish command failed"},
            status_code=500,
        )

    audit_logger.info(
        "SUCCESS | staff=%s | action=vanish_toggle | mc_name=%s | command=%s",
        staff_email,
        staff_mc_name,
        command,
    )

    notification_sent = await _send_staff_notice(
        staff_email=staff_email,
        staff_mc_name=staff_mc_name,
        action="vanish_toggle",
        notice_text=_VANISH_NOTICE_TEXT,
    )

    return JSONResponse(
        {
            "success": True,
            "minecraft_name": staff_mc_name,
            "message": f"Toggled vanish for {staff_mc_name}",
            "response": response_text,
            "notification_sent": notification_sent,
        }
    )


@router.post("/api/minecraft/spectator/tp")
async def staff_tp_in_spectator_mode(
    request: Request,
    user_info: dict = Depends(require_permission("spectator:quick_toggle"))
):
    """Teleport staff to an online player, but only while currently in spectator mode."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON payload"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "Invalid request payload"}, status_code=400)

    target_raw = body.get("target", "")
    if not isinstance(target_raw, str):
        return JSONResponse({"success": False, "error": "target must be a string"}, status_code=400)

    target_player = normalize_player(target_raw)
    ok, err = validate_player_name(target_player)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=400)

    staff_email = user_info.get("email", "unknown")
    staff_mc_name = _get_hardcoded_staff_minecraft_name(staff_email)
    if not staff_mc_name:
        return _blocked_quick_toggle_security_response(
            staff_email=staff_email,
            action="spectator_tp",
        )

    staff_mc_name, online_players, online_check_failure = await _ensure_staff_online(
        staff_email=staff_email,
        staff_mc_name=staff_mc_name,
        action="spectator_tp",
        require_online_players=True,
    )
    if online_check_failure:
        return online_check_failure

    if target_player.lower() == staff_mc_name.lower():
        return JSONResponse(
            {"success": False, "error": "Choose another online player as TP target."},
            status_code=400,
        )

    is_spectator, spectator_state = await _is_staff_in_spectator_mode(staff_mc_name)
    if is_spectator is None:
        return JSONResponse(
            {"success": False, "error": spectator_state or "Failed to verify current game mode."},
            status_code=500,
        )
    if not is_spectator:
        audit_logger.warning(
            "BLOCKED | staff=%s | action=spectator_tp | reason=not_in_spectator_mode | mc_name=%s",
            staff_email,
            staff_mc_name,
        )
        return JSONResponse(
            {
                "success": False,
                "error": "TP is only allowed while you are in spectator mode.",
                "spectator_state": spectator_state,
            },
            status_code=409,
        )

    online_lookup = {player.lower(): player for player in online_players}
    if target_player.lower() not in online_lookup:
        return JSONResponse(
            {"success": False, "error": f"Target player is not online: {target_player}"},
            status_code=404,
        )

    target_player = online_lookup[target_player.lower()]
    tp_command = f"tp {staff_mc_name} {target_player}"
    tp_result = await minecraft_server.send_command(tp_command)
    if not tp_result.get("success"):
        return JSONResponse(
            {"success": False, "error": tp_result.get("error", "Failed to teleport")},
            status_code=500,
        )

    response_text = _extract_response_text(tp_result)
    if _response_looks_like_command_failure(response_text):
        return JSONResponse(
            {"success": False, "error": response_text or "Teleport command failed"},
            status_code=500,
        )

    audit_logger.info(
        "SUCCESS | staff=%s | action=spectator_tp | mc_name=%s | target=%s",
        staff_email,
        staff_mc_name,
        target_player,
    )

    notification_sent = await _send_staff_notice(
        staff_email=staff_email,
        staff_mc_name=staff_mc_name,
        action="spectator_tp",
        notice_text=_TP_NOTICE_TEMPLATE.format(target=target_player),
    )

    return JSONResponse(
        {
            "success": True,
            "minecraft_name": staff_mc_name,
            "target": target_player,
            "message": f"Teleported {staff_mc_name} to {target_player}",
            "response": response_text,
            "notification_sent": notification_sent,
        }
    )


@router.post("/api/minecraft/tempban")
async def staff_tempban_player(request: Request, user_info: dict = Depends(require_permission("moderation:tempban"))):
    """
    Temporarily ban a player (staff access).
    
    Body:
        player: str - Player name to ban
        duration: str - Duration (1h, 6h, 24h, 7d)
        reason: str - Ban reason
    """
    body = await request.json()
    player = normalize_player(body.get("player", ""))
    duration = body.get("duration", "").strip()
    reason = body.get("reason", "Staff action").strip()

    staff_email = user_info.get("email", "unknown")

    ok, err = validate_player_name(player)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=400)

    allowed_durations = ["1h", "6h", "24h", "7d"]
    if duration not in allowed_durations:
        return JSONResponse({
            "success": False,
            "error": f"Invalid duration. Allowed: {', '.join(allowed_durations)}"
        }, status_code=400)

    reason = sanitize_moderation_reason(reason=reason, max_len=100, default="Staff action")

    ok, err = deny_if_protected(player=player, allow_protected=False)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=403)

    # Send tempban command (requires EssentialsX or similar plugin)
    command = f"tempban {player} {duration} {reason}"
    result = await minecraft_server.send_command(command)
    
    if result.get("success"):
        audit_logger.info(f"SUCCESS | staff={staff_email} | action=tempban | target={player} | duration={duration} | reason={reason}")
        return JSONResponse({
            "success": True,
            "message": f"Temporarily banned {player} for {duration}",
            "response": result.get("response", "")
        })
    else:
        return JSONResponse({
            "success": False,
            "error": result.get("error", "Failed to execute ban command")
        }, status_code=500)


# ============================================
# PHASE 1: Kick, Broadcast, Logs
# ============================================

@router.post("/api/minecraft/kick")
async def kick_player(request: Request, user_info: dict = Depends(require_permission("moderation:kick"))):
    """
    Kick a player from the server (staff access).

    Body:
        player: str - Player name to kick
        reason: str - Kick reason (optional)
    """
    body = await request.json()
    player = normalize_player(body.get("player", ""))
    reason = body.get("reason", "Kicked by staff").strip()

    staff_email = user_info.get("email", "unknown")

    ok, err = validate_player_name(player)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=400)

    reason = sanitize_moderation_reason(reason=reason, max_len=100, default="Kicked by staff")

    ok, err = deny_if_protected(player=player, allow_protected=False)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=403)

    # Send kick command
    command = f"kick {player} {reason}"
    result = await minecraft_server.send_command(command)

    if result.get("success"):
        audit_logger.info(f"SUCCESS | staff={staff_email} | action=kick | target={player} | reason={reason}")
        return JSONResponse({
            "success": True,
            "message": f"Kicked {player}",
            "response": result.get("response", "")
        })
    else:
        return JSONResponse({
            "success": False,
            "error": result.get("error", "Failed to execute kick command")
        }, status_code=500)


# Rate limiting for broadcast (1 message per 60 seconds per staff)
_broadcast_cooldowns: dict = {}
BROADCAST_COOLDOWN_SECONDS = 60


@router.post("/api/minecraft/broadcast")
async def broadcast_message(request: Request, user_info: dict = Depends(require_permission("moderation:broadcast"))):
    """
    Send a server-wide broadcast message (staff access).

    Body:
        message: str - Message to broadcast (max 200 chars)
    """
    body = await request.json()
    message = body.get("message", "").strip()

    staff_email = user_info.get("email", "unknown")

    # Validate message
    if not message:
        return JSONResponse({"success": False, "error": "Message is required"}, status_code=400)

    if len(message) > 200:
        return JSONResponse({
            "success": False,
            "error": "Message too long. Maximum 200 characters allowed."
        }, status_code=400)

    # Sanitize message - allow more characters but prevent command injection
    message = message.replace('\n', ' ').replace('\r', ' ')
    # Remove potentially dangerous characters that could affect Minecraft commands
    message = re.sub(r'[/\\@]', '', message)
    message = ' '.join(message.split())

    if not message:
        return JSONResponse({"success": False, "error": "Message is empty after sanitization"}, status_code=400)

    # Rate limit check
    current_time = time.time()
    last_broadcast = _broadcast_cooldowns.get(staff_email, 0)
    if current_time - last_broadcast < BROADCAST_COOLDOWN_SECONDS:
        remaining = int(BROADCAST_COOLDOWN_SECONDS - (current_time - last_broadcast))
        return JSONResponse({
            "success": False,
            "error": f"Please wait {remaining} seconds before sending another broadcast."
        }, status_code=429)

    # Send broadcast via RCON say command with staff tag
    command = f'say [STAFF] {message}'
    result = await minecraft_server.send_command(command)

    if result.get("success"):
        _broadcast_cooldowns[staff_email] = current_time
        audit_logger.info(f"SUCCESS | staff={staff_email} | action=broadcast | message={message[:50]}")
        return JSONResponse({
            "success": True,
            "message": "Broadcast sent successfully",
            "response": result.get("response", "")
        })
    else:
        return JSONResponse({
            "success": False,
            "error": result.get("error", "Failed to send broadcast")
        }, status_code=500)


# Patterns to filter from staff log viewing (security)
LOG_SENSITIVE_PATTERNS = [
    re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),  # IP addresses
    re.compile(r'rcon\.password\s*=\s*\S+', re.IGNORECASE),  # RCON password
    re.compile(r'/op\s+', re.IGNORECASE),  # OP commands
    re.compile(r'/deop\s+', re.IGNORECASE),  # Deop commands
    re.compile(r'/ban\s+', re.IGNORECASE),  # Permanent ban (admin only)
    re.compile(r'/pardon\s+', re.IGNORECASE),  # Pardon commands
]


def filter_sensitive_logs(logs: list) -> list:
    """Filter out sensitive information and protected player actions from log entries"""
    filtered = []
    for log in logs:
        message = log.get("message", "")

        # Skip entries that contain sensitive patterns
        should_skip = False
        for pattern in LOG_SENSITIVE_PATTERNS:
            if pattern.search(message):
                should_skip = True
                break

        # Skip entries containing protected player names (hide their actions from staff logs)
        if not should_skip:
            message_lower = message.lower()
            for player in PROTECTED_PLAYERS:
                if player.lower() in message_lower:
                    should_skip = True
                    break

        if not should_skip:
            # Mask any remaining IP addresses just in case
            masked_message = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP]', message)
            filtered.append({
                "time": log.get("time", ""),
                "message": masked_message
            })

    return filtered


@router.get("/api/minecraft/logs")
async def get_staff_logs(
    lines: int = Query(default=200, le=500, ge=10),
    search: Optional[str] = Query(default=None, max_length=50),
    user_info: dict = Depends(require_permission("logs:view"))
):
    """
    Get filtered server logs (staff access).

    - Returns last N lines (max 500)
    - Filters out sensitive info (IPs, passwords, admin commands)
    - Optional search by player name
    """
    # Get logs from the service
    all_logs = minecraft_server.get_recent_logs(lines=500, filtered=True)

    if not all_logs:
        # Fall back to reading from file
        all_logs = minecraft_server.read_latest_log(lines=500)

    # Filter sensitive information
    filtered_logs = filter_sensitive_logs(all_logs)

    # Optional search filter
    if search:
        search_lower = search.lower()
        # Validate search term (player name format)
        if not re.match(r'^[a-zA-Z0-9_]{1,16}$', search):
            return JSONResponse({
                "success": False,
                "error": "Invalid search term. Use alphanumeric characters only."
            }, status_code=400)

        filtered_logs = [
            log for log in filtered_logs
            if search_lower in log.get("message", "").lower()
        ]

    # Return only requested number of lines
    result_logs = filtered_logs[-lines:]

    return JSONResponse({
        "status": "ok",
        "count": len(result_logs),
        "logs": result_logs
    })


# ============================================
# PHASE 2: Whitelist Management, CoreProtect
# ============================================

@router.get("/api/minecraft/whitelist")
async def get_whitelist(user_info: dict = Depends(require_permission("whitelist:view"))):
    """Get current server whitelist with order information"""
    import json
    
    # Read whitelist.json directly to preserve add order
    whitelist_path = minecraft_settings.get_whitelist_path()
    
    players = []
    if whitelist_path.exists():
        try:
            with open(whitelist_path, 'r', encoding='utf-8') as f:
                whitelist_data = json.load(f)
            
            # Build player list with index (add order)
            for idx, entry in enumerate(whitelist_data):
                players.append({
                    "name": entry.get("name", ""),
                    "uuid": entry.get("uuid", ""),
                    "index": idx  # Original position in file (add order)
                })
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Staff] Error reading whitelist.json: {e}")
            # Fallback to RCON if file read fails
            result = await minecraft_server.send_command("whitelist list")
            if result.get("success") and result.get("response"):
                response = result["response"]
                if ":" in response:
                    players_part = response.split(":")[-1].strip()
                    if players_part:
                        players = [{"name": p.strip(), "uuid": "", "index": idx} 
                                   for idx, p in enumerate(players_part.split(",")) if p.strip()]

    return JSONResponse({
        "status": "ok",
        "players": players,
        "count": len(players)
    })


@router.post("/api/minecraft/whitelist/add")
async def whitelist_add(request: Request, user_info: dict = Depends(require_permission("whitelist:add"))):
    """
    Add a player to the whitelist (staff access).

    Body:
        player: str - Player name to whitelist
    """
    body = await request.json()
    player = body.get("player", "").strip()

    staff_email = user_info.get("email", "unknown")

    # Validate player name
    if not player:
        return JSONResponse({"success": False, "error": "Player name required"}, status_code=400)

    if not PLAYER_NAME_PATTERN.match(player):
        audit_logger.warning(f"REJECTED | staff={staff_email} | action=whitelist_add | reason=invalid_player_name | input={player[:50]}")
        return JSONResponse({
            "success": False,
            "error": "Invalid player name. Use 3-16 alphanumeric characters or underscores."
        }, status_code=400)

    # Execute whitelist add command
    result = await minecraft_server.send_command(f"whitelist add {player}")

    if result.get("success"):
        whitelist_audit_service.record_whitelist_add(
            player,
            actor_email=staff_email,
            actor_surface="staff_panel",
        )
        _invalidate_staff_whitelist_cache()
        audit_logger.info(f"SUCCESS | staff={staff_email} | action=whitelist_add | target={player}")
        return JSONResponse({
            "success": True,
            "message": f"Added {player} to whitelist",
            "response": result.get("response", "")
        })
    else:
        return JSONResponse({
            "success": False,
            "error": result.get("error", "Failed to add to whitelist")
        }, status_code=500)


@router.post("/api/minecraft/whitelist/remove")
async def whitelist_remove(request: Request, user_info: dict = Depends(require_permission("whitelist:remove"))):
    """
    Remove a player from the whitelist (staff access).

    Body:
        player: str - Player name to remove from whitelist
    """
    body = await request.json()
    player = normalize_player(body.get("player", ""))

    staff_email = user_info.get("email", "unknown")
    # Permission already enforced by require_permission dependency

    # Validate player name
    if not player:
        return JSONResponse({"success": False, "error": "Player name required"}, status_code=400)

    if not PLAYER_NAME_PATTERN.match(player):
        audit_logger.warning(f"REJECTED | staff={staff_email} | action=whitelist_remove | reason=invalid_player_name | input={player[:50]}")
        return JSONResponse({
            "success": False,
            "error": "Invalid player name. Use 3-16 alphanumeric characters or underscores."
        }, status_code=400)

    # Check protected players - cannot remove protected players from whitelist
    if player.lower() in [p.lower() for p in PROTECTED_PLAYERS]:
        audit_logger.warning(f"BLOCKED | staff={staff_email} | action=whitelist_remove | target={player} | reason=protected_player")
        return JSONResponse({
            "success": False,
            "error": f"Cannot remove protected player from whitelist: {player}"
        }, status_code=403)

    # Execute whitelist remove command
    result = await minecraft_server.send_command(f"whitelist remove {player}")

    if result.get("success"):
        whitelist_audit_service.record_whitelist_remove(player)
        _invalidate_staff_whitelist_cache()
        audit_logger.info(f"SUCCESS | staff={staff_email} | action=whitelist_remove | target={player}")
        return JSONResponse({
            "success": True,
            "message": f"Removed {player} from whitelist",
            "response": result.get("response", "")
        })
    else:
        return JSONResponse({
            "success": False,
            "error": result.get("error", "Failed to remove from whitelist")
        }, status_code=500)


@router.get("/api/minecraft/whitelist/audit")
async def staff_get_whitelist_audit(
    inactive_days: int = Query(default=30, ge=1, le=3650),
    refresh: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
    latest_shared: bool = Query(default=False),
    user_info: dict = Depends(require_permission("whitelist:audit")),
):
    """Return indexed whitelist activity for staff review."""
    snapshot = whitelist_audit_service.get_audit_snapshot(
        inactive_days=inactive_days,
        force_refresh=bool(refresh or force_refresh),
        actor_email=(user_info or {}).get("email"),
        latest_shared=bool(latest_shared and not (refresh or force_refresh)),
    )
    return JSONResponse({
        "status": "ok",
        **snapshot,
    })


@router.get("/api/minecraft/whitelist/manual-prune-history")
async def staff_get_manual_prune_history(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default=""),
    status: str = Query(default="all"),
    sort: str = Query(default="removed_desc"),
    _user_info: dict = Depends(require_permission("whitelist:audit")),
):
    """Return a paginated archive of audit-based manual removals for staff review."""
    payload = whitelist_audit_service.get_manual_prune_history_page(
        limit=limit,
        offset=offset,
        q=q,
        status=status,
        sort=sort,
    )
    return JSONResponse(payload)


@router.get("/api/minecraft/whitelist/long-term-inactivity")
async def staff_get_long_term_inactivity(
    threshold_days: int = Query(default=100, ge=1, le=3650),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default=""),
    status: str = Query(default="all"),
    whitelist: str = Query(default="all"),
    sort: str = Query(default="review_desc"),
    _user_info: dict = Depends(require_permission("whitelist:audit")),
):
    """Return paginated long-term inactivity candidates for staff review."""
    payload = whitelist_audit_service.get_long_term_inactivity_page(
        threshold_days=threshold_days,
        limit=limit,
        offset=offset,
        q=q,
        status=status,
        whitelist=whitelist,
        sort=sort,
    )
    return JSONResponse(payload)


@router.post("/api/minecraft/whitelist/prune")
async def staff_prune_inactive_whitelist(
    request: Request,
    _audit_user_info: dict = Depends(require_permission("whitelist:audit")),
    user_info: dict = Depends(require_permission("whitelist:prune")),
):
    """
    Manually remove selected inactive players from the whitelist (staff access).

    Body:
        players: list[str] - explicit player names to remove
        inactive_days: int - inactivity threshold used for safety validation
        confirm: bool - must be true
        dry_run: bool - optional preview mode
    """
    body = await request.json()
    requested_players = body.get("players", [])
    confirm = body.get("confirm", False)
    dry_run = bool(body.get("dry_run", False))
    staff_email = str((_audit_user_info or {}).get("email") or (user_info or {}).get("email") or "").strip()

    if not isinstance(requested_players, list):
        return JSONResponse(
            {"success": False, "error": "players must be an array"},
            status_code=400,
        )

    if not confirm:
        return JSONResponse(
            {"success": False, "error": "Manual confirmation is required"},
            status_code=400,
        )

    try:
        inactive_days = int(body.get("inactive_days", 30))
    except (TypeError, ValueError):
        return JSONResponse(
            {"success": False, "error": "inactive_days must be an integer"},
            status_code=400,
        )

    if inactive_days < 1 or inactive_days > 3650:
        return JSONResponse(
            {"success": False, "error": "inactive_days must be between 1 and 3650"},
            status_code=400,
        )

    prune_plan = whitelist_audit_service.build_manual_prune_plan(
        requested_players,
        inactive_days=inactive_days,
        protected_players=PROTECTED_PLAYERS,
        max_players=100,
    )
    eligible = prune_plan["eligible"]
    skipped = prune_plan["skipped"]
    inactive_days = prune_plan["inactive_days"]

    if dry_run:
        audit_logger.info(
            "DRY_RUN | staff=%s | action=whitelist_prune | requested=%s | eligible=%s | skipped=%s | threshold=%s",
            staff_email,
            len(requested_players),
            len(eligible),
            len(skipped),
            inactive_days,
        )
        return JSONResponse({
            "success": True,
            "dry_run": True,
            "inactive_days": inactive_days,
            "eligible": eligible,
            "skipped": skipped,
        })

    removed: list[dict] = []
    failed: list[dict] = []

    for entry in eligible:
        player_name = str(entry["name"]).strip()
        if not _WHITELIST_AUDIT_TARGET_PATTERN.match(player_name):
            failed.append({"player": player_name, "error": "Invalid player name"})
            continue

        result = await minecraft_server.send_command(f"whitelist remove {player_name}")
        if result.get("success"):
            whitelist_audit_service.record_whitelist_remove(player_name)
            whitelist_audit_service.record_manual_prune(
                entry,
                actor_email=staff_email,
                actor_surface="staff_panel",
                inactive_days_threshold=inactive_days,
            )
            audit_logger.info(
                "SUCCESS | staff=%s | action=whitelist_prune | target=%s | threshold=%s",
                staff_email,
                player_name,
                inactive_days,
            )
            removed.append(
                {
                    "player": player_name,
                    "response": result.get("response", ""),
                }
            )
            continue

        audit_logger.warning(
            "FAILED | staff=%s | action=whitelist_prune | target=%s | threshold=%s | reason=%s",
            staff_email,
            player_name,
            inactive_days,
            result.get("error", "Failed to remove from whitelist"),
        )
        failed.append(
            {
                "player": player_name,
                "error": result.get("error", "Failed to remove from whitelist"),
            }
        )

    if removed:
        _invalidate_staff_whitelist_cache()

    partial_success = bool(removed and failed)
    if skipped:
        audit_logger.info(
            "SKIPPED | staff=%s | action=whitelist_prune | skipped=%s | threshold=%s",
            staff_email,
            skipped,
            inactive_days,
        )
    message = f"Removed {len(removed)} inactive whitelist player(s)"
    if partial_success:
        message = (
            f"Removed {len(removed)} inactive whitelist player(s); "
            f"{len(failed)} removal(s) failed"
        )
    elif failed and not removed:
        message = "Failed to remove selected whitelist players"

    return JSONResponse({
        "success": not failed,
        "partial_success": partial_success,
        "inactive_days": inactive_days,
        "message": message,
        "removed": removed,
        "failed": failed,
        "skipped": skipped,
    })


# Import CoreProtect service
from app.services import coreprotect


@router.get("/api/minecraft/coreprotect/lookup")
async def coreprotect_lookup(
    player: Optional[str] = Query(default=None, max_length=16),
    x: Optional[int] = Query(default=None),
    y: Optional[int] = Query(default=None),
    z: Optional[int] = Query(default=None),
    radius: int = Query(default=5, le=10, ge=1),
    limit: int = Query(default=50, le=100, ge=1),
    user_info: dict = Depends(require_permission("lookup:coreprotect"))
):
    """
    Query CoreProtect database for block changes (staff access, read-only).

    Either provide:
    - player: Look up by player name
    - x, y, z: Look up by coordinates (with optional radius)

    Results limited to last 7 days and 100 entries max.
    """
    staff_email = user_info.get("email", "unknown")

    # Check if CoreProtect database is available
    if not coreprotect.is_database_available():
        return JSONResponse({
            "success": False,
            "error": "CoreProtect database not available"
        }, status_code=503)

    # Validate input - must provide either player or coordinates
    if not player and (x is None or y is None or z is None):
        return JSONResponse({
            "success": False,
            "error": "Provide either 'player' or 'x', 'y', 'z' coordinates"
        }, status_code=400)

    results = []

    if player:
        # Validate player name
        if not PLAYER_NAME_PATTERN.match(player):
            return JSONResponse({
                "success": False,
                "error": "Invalid player name format"
            }, status_code=400)

        audit_logger.info(f"LOOKUP | staff={staff_email} | action=coreprotect_lookup | type=player | target={player}")
        results = coreprotect.lookup_by_player(player, limit=limit)

    elif x is not None and y is not None and z is not None:
        audit_logger.info(f"LOOKUP | staff={staff_email} | action=coreprotect_lookup | type=coords | x={x} y={y} z={z} r={radius}")
        results = coreprotect.lookup_by_coordinates(x, y, z, radius=radius, limit=limit)

    # Convert dataclass objects to dicts for JSON response
    results_data = [
        {
            "id": r.id,
            "timestamp": r.timestamp,
            "player": r.player,
            "action": r.action,
            "world": r.world,
            "x": r.x,
            "y": r.y,
            "z": r.z,
            "block": r.block_type,
        }
        for r in results
    ]

    return JSONResponse({
        "status": "ok",
        "count": len(results_data),
        "results": results_data,
        "note": "Read-only lookup. Rollback requires admin access."
    })


# ============================================
# PHASE 3: Warning System
# ============================================

# Import warnings service
from app.services import warnings as warnings_service
from app.services import watchlist as watchlist_service
from app.services import player_notes as notes_service
from app.services import investigation as investigation_service
from app.services import staff_settings as staff_settings_service
from app.services import user_preferences as user_preferences_service


@router.post("/api/minecraft/warn")
async def warn_player(request: Request, user_info: dict = Depends(require_permission("warnings:issue"))):
    """
    Issue a warning to a player (staff access).

    Body:
        player: str - Player name to warn
        reason: str - Warning reason
        notify: bool - Whether to notify player in-game (optional, default true)
    """
    body = await request.json()
    # Extract clean username from display name (strips titles like [Dragon_Rider])
    player = extract_username(body.get("player", "").strip())
    reason = body.get("reason", "").strip()
    notify = body.get("notify", True)

    staff_email = user_info.get("email", "unknown")

    ok, err = validate_player_name(player)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=400)

    # Validate reason
    if not reason:
        return JSONResponse({"success": False, "error": "Warning reason required"}, status_code=400)

    reason = sanitize_moderation_reason(reason=reason, max_len=200, default="Warning")

    ok, err = deny_if_protected(player=player, allow_protected=False)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=403)

    # Issue the warning
    warning = warnings_service.issue_warning(player, reason, staff_email)

    # Get warning count for this player
    warning_count = warnings_service.get_warning_count(player)
    escalation = warnings_service.get_escalation_recommendation(player)

    # Optionally notify player in-game
    notified = False
    if notify:
        # Send warning message via RCON
        notify_command = f'msg {player} [WARNING] You have been warned: {reason[:100]}'
        result = await minecraft_server.send_command(notify_command)
        if result.get("success"):
            warnings_service.mark_warning_notified(warning.id)
            notified = True

    audit_logger.info(f"SUCCESS | staff={staff_email} | action=warn | target={player} | reason={reason[:50]} | warning_id={warning.id}")

    response_data = {
        "success": True,
        "message": f"Warning issued to {player}",
        "warning": {
            "id": warning.id,
            "player": warning.player,
            "reason": warning.reason,
            "timestamp": warning.timestamp,
            "notified": notified
        },
        "total_warnings": warning_count
    }

    if escalation:
        response_data["escalation_recommendation"] = escalation

    return JSONResponse(response_data)


@router.get("/api/minecraft/warnings/{player}")
async def get_player_warnings(player: str, user_info: dict = Depends(require_permission("warnings:view"))):
    """
    Get warning history for a specific player (staff access).
    """
    staff_email = user_info.get("email", "unknown")

    # Validate player name
    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    warnings = warnings_service.get_player_warnings(player)
    escalation = warnings_service.get_escalation_recommendation(player)

    audit_logger.info(f"LOOKUP | staff={staff_email} | action=view_warnings | target={player} | count={len(warnings)}")

    response_data = {
        "status": "ok",
        "player": player,
        "count": len(warnings),
        "warnings": [
            {
                "id": w.id,
                "reason": w.reason,
                "issued_by": w.issued_by,
                "timestamp": w.timestamp,
                "notified": w.notified
            }
            for w in warnings
        ]
    }

    if escalation:
        response_data["escalation_recommendation"] = escalation

    return JSONResponse(response_data)


@router.get("/api/minecraft/warnings")
async def get_all_warnings(
    limit: int = Query(default=50, le=100, ge=1),
    user_info: dict = Depends(require_permission("warnings:view"))
):
    """
    Get all recent warnings (staff access).
    """
    warnings = warnings_service.get_all_warnings(limit=limit)

    return JSONResponse({
        "status": "ok",
        "count": len(warnings),
        "warnings": [
            {
                "id": w.id,
                "player": w.player,
                "reason": w.reason,
                "issued_by": w.issued_by,
                "timestamp": w.timestamp,
                "notified": w.notified
            }
            for w in warnings
        ]
    })


@router.delete("/api/minecraft/warnings/{warning_id}")
async def delete_warning(warning_id: str, user_info: dict = Depends(require_permission("warnings:delete"))):
    """
    Delete a warning by ID (staff access).

    Only the staff member who issued the warning can delete it,
    unless they are an admin.
    """
    staff_email = user_info.get("email", "unknown")

    # Get the warning first
    warning = warnings_service.get_warning_by_id(warning_id)
    if not warning:
        return JSONResponse({
            "success": False,
            "error": "Warning not found"
        }, status_code=404)

    # Check if staff can delete this warning
    # Staff can only delete their own warnings
    if warning.issued_by != staff_email and not is_admin(user_info):
        audit_logger.warning(f"BLOCKED | staff={staff_email} | action=delete_warning | warning_id={warning_id} | reason=not_owner")
        return JSONResponse({
            "success": False,
            "error": "You can only delete warnings you issued"
        }, status_code=403)

    # Delete the warning
    if warnings_service.delete_warning(warning_id, staff_email):
        audit_logger.info(f"SUCCESS | staff={staff_email} | action=delete_warning | warning_id={warning_id} | target={warning.player}")
        return JSONResponse({
            "success": True,
            "message": f"Warning {warning_id} deleted"
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Failed to delete warning"
        }, status_code=500)


# ============================================
# PHASE 4: Watchlist & Investigation
# ============================================

@router.get("/api/watchlist")
async def staff_get_watchlist(user_info: dict = Depends(require_permission("watchlist:view"))):
    """Get all active watchlist entries (staff can view only)."""
    entries = watchlist_service.get_watchlist(include_resolved=False)
    stats = watchlist_service.get_watchlist_stats()

    return JSONResponse({
        "status": "ok",
        "count": len(entries),
        "stats": stats,
        "entries": [
            {
                "id": e.id,
                "player": e.player,
                "level": e.level,
                "reason": e.reason,
                "added_at": e.added_at,
                "status": e.status,
                "tags": e.tags
            }
            for e in entries
        ]
    })


@router.get("/api/watchlist/check/{player}")
async def staff_check_player_watchlist(player: str, user_info: dict = Depends(require_permission("watchlist:view"))):
    """Check if a player is on the watchlist (staff access)."""
    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    entry = watchlist_service.get_watchlist_entry_by_player(player, active_only=True)

    if entry:
        return JSONResponse({
            "status": "ok",
            "watchlisted": True,
            "entry": {
                "id": entry.id,
                "player": entry.player,
                "level": entry.level,
                "reason": entry.reason,
                "tags": entry.tags
            }
        })
    else:
        return JSONResponse({
            "status": "ok",
            "watchlisted": False,
            "player": player
        })


# ============================================
# Player Notes (Staff can view and add)
# ============================================

@router.get("/api/notes/{player}")
async def staff_get_player_notes(player: str, user_info: dict = Depends(require_permission("notes:view"))):
    """Get all notes for a player (staff access)."""
    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    notes = notes_service.get_player_notes(player)

    return JSONResponse({
        "status": "ok",
        "player": player,
        "count": len(notes),
        "notes": [
            {
                "id": n.id,
                "player": n.player,
                "content": n.content,
                "author": n.author,
                "author_name": n.author_name,
                "created_at": n.created_at,
                "updated_at": n.updated_at,
                "category": n.category
            }
            for n in notes
        ]
    })


@router.post("/api/notes")
async def staff_add_note(request: Request, user_info: dict = Depends(require_permission("notes:manage"))):
    """Add a note about a player (staff access)."""
    body = await request.json()
    player = body.get("player", "").strip()
    content = body.get("content", "").strip()
    category = body.get("category", "general")
    author_email = user_info.get("email", "unknown")
    author_name = user_info.get("name", author_email)

    if not player:
        return JSONResponse({"success": False, "error": "Player name required"}, status_code=400)

    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    if not content:
        return JSONResponse({"success": False, "error": "Note content required"}, status_code=400)

    note = notes_service.add_note(
        player=player,
        content=content,
        author_email=author_email,
        author_name=author_name,
        category=category
    )

    if note:
        audit_logger.info(f"SUCCESS | staff={author_email} | action=add_note | target={player}")
        return JSONResponse({
            "success": True,
            "message": "Note added",
            "note": {
                "id": note.id,
                "player": note.player,
                "category": note.category,
                "created_at": note.created_at
            }
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Failed to add note"
        }, status_code=500)


@router.put("/api/notes/{note_id}")
async def staff_update_note(note_id: str, request: Request, user_info: dict = Depends(require_permission("notes:manage"))):
    """Update a note (staff can only edit own notes)."""
    body = await request.json()
    author_email = user_info.get("email", "unknown")

    note = notes_service.update_note(
        note_id=note_id,
        author_email=author_email,
        content=body.get("content"),
        category=body.get("category")
    )

    if note:
        return JSONResponse({
            "success": True,
            "message": "Note updated",
            "note": {
                "id": note.id,
                "updated_at": note.updated_at
            }
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Note not found or you are not the author"
        }, status_code=404)


@router.delete("/api/notes/{note_id}")
async def staff_delete_note(note_id: str, user_info: dict = Depends(require_permission("notes:manage"))):
    """Delete a note (staff can only delete own notes)."""
    author_email = user_info.get("email", "unknown")

    if notes_service.delete_note(note_id, author_email, is_admin=is_admin(user_info)):
        return JSONResponse({
            "success": True,
            "message": f"Note {note_id} deleted"
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Note not found or you are not the author"
        }, status_code=404)


# ============================================
# Investigation Sessions
# ============================================

@router.post("/api/investigation/start")
async def staff_start_investigation(request: Request, user_info: dict = Depends(require_permission("investigation:manage"))):
    """
    Start an investigation session for a watchlisted player.
    Staff can only investigate players on the watchlist.
    """
    body = await request.json()
    player = body.get("player", "").strip()
    staff_email = user_info.get("email", "unknown")

    if not player:
        return JSONResponse({"success": False, "error": "Player name required"}, status_code=400)

    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    # Check if player is watchlisted (for staff)
    if not is_admin(user_info) and not watchlist_service.is_watchlisted(player):
        return JSONResponse({
            "success": False,
            "error": "Can only investigate watchlisted players"
        }, status_code=403)

    session = investigation_service.start_investigation(
        player=player,
        staff_email=staff_email,
        is_admin=is_admin(user_info)
    )

    if session:
        audit_logger.info(f"SUCCESS | staff={staff_email} | action=start_investigation | target={player}")
        return JSONResponse({
            "success": True,
            "message": f"Investigation started for {player}",
            "session": {
                "id": session.id,
                "player": session.player,
                "watchlist_id": session.watchlist_id,
                "started_at": session.started_at
            }
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Failed to start investigation. You may already have an active session."
        }, status_code=400)


@router.get("/api/investigation/active")
async def staff_get_active_investigation(user_info: dict = Depends(require_permission("investigation:view"))):
    """Get the current active investigation for this staff member."""
    staff_email = user_info.get("email", "unknown")

    session = investigation_service.get_active_investigation(staff_email)

    if session:
        return JSONResponse({
            "status": "ok",
            "active": True,
            "session": {
                "id": session.id,
                "player": session.player,
                "watchlist_id": session.watchlist_id,
                "started_at": session.started_at,
                "commands_executed": len(session.commands_executed)
            }
        })
    else:
        return JSONResponse({
            "status": "ok",
            "active": False
        })


@router.post("/api/investigation/{session_id}/end")
async def staff_end_investigation(
    session_id: str,
    request: Request,
    user_info: dict = Depends(require_permission("investigation:manage"))
):
    """End an investigation session with findings."""
    body = await request.json()
    findings = body.get("findings", "").strip()
    recommendation = body.get("recommendation", "watch")
    staff_email = user_info.get("email", "unknown")

    if not findings:
        return JSONResponse({"success": False, "error": "Findings required"}, status_code=400)

    session = investigation_service.end_investigation(
        session_id=session_id,
        staff_email=staff_email,
        findings=findings,
        recommendation=recommendation
    )

    if session:
        audit_logger.info(f"SUCCESS | staff={staff_email} | action=end_investigation | session={session_id} | recommendation={recommendation}")
        return JSONResponse({
            "success": True,
            "message": "Investigation completed",
            "session": {
                "id": session.id,
                "player": session.player,
                "recommendation": session.recommendation,
                "ended_at": session.ended_at
            }
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Session not found or not authorized"
        }, status_code=404)


@router.get("/api/investigation/grimac/{player}")
async def staff_run_grimac(player: str, user_info: dict = Depends(require_permission("investigation:grimac"))):
    """
    Get GrimAC violation history for a player from the database.
    Staff can only run this for watchlisted players.
    """
    from app.services import grimac as grimac_service
    
    staff_email = user_info.get("email", "unknown")

    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    # Check watchlist for staff
    if not is_admin(user_info) and not watchlist_service.is_watchlisted(player):
        return JSONResponse({
            "success": False,
            "error": "Can only investigate watchlisted players"
        }, status_code=403)

    # Get active session for logging
    session = investigation_service.get_active_investigation(staff_email)
    session_id = session.id if session and session.player == player.lower() else None

    # Query the GrimAC database directly - get more records
    result = grimac_service.get_player_violations(player, limit=100)

    # Log the command execution if in active session
    if session_id:
        investigation_service.log_command_execution(
            session_id=session_id,
            command=f"grimac history {player}",
            response=f"Found {result.get('summary', {}).get('total_count', 0)} violations" if result.get('success') else result.get('error', 'Error'),
            success=result.get('success', False),
            staff_email=staff_email
        )

    audit_logger.info(f"COMMAND | staff={staff_email} | action=grimac_history | target={player}")

    if result.get('success'):
        formatted_response = format_grimac_report(player, result)
        return JSONResponse({
            "success": True,
            "response": formatted_response,
            "data": result
        })
    else:
        return JSONResponse({
            "success": False,
            "error": result.get('error', 'Unknown error')
        })


@router.get("/api/investigation/mtrack/{player}")
async def staff_run_mtrack(player: str, user_info: dict = Depends(require_permission("investigation:mtrack"))):
    """
    Run mtrack check command for a player.
    Staff can only run this for watchlisted players.
    """
    staff_email = user_info.get("email", "unknown")

    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    # Check watchlist for staff
    if not is_admin(user_info) and not watchlist_service.is_watchlisted(player):
        return JSONResponse({
            "success": False,
            "error": "Can only investigate watchlisted players"
        }, status_code=403)

    # Get active session for logging
    session = investigation_service.get_active_investigation(staff_email)
    session_id = session.id if session and session.player == player.lower() else None

    if session_id:
        result = await investigation_service.execute_mtrack_check(player, session_id, staff_email)
    else:
        # Run without logging to session
        result = await minecraft_server.send_command(f"mtrack check {player}")

    audit_logger.info(f"COMMAND | staff={staff_email} | action=mtrack_check | target={player}")

    return JSONResponse({
        "success": result.get("success", False),
        "response": result.get("response", ""),
        "error": result.get("error")
    })


@router.get("/api/investigation/history/{player}")
async def staff_get_investigation_history(player: str, user_info: dict = Depends(require_permission("investigation:view"))):
    """Get investigation history for a player."""
    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    sessions = investigation_service.get_player_investigation_history(player)

    return JSONResponse({
        "status": "ok",
        "player": player,
        "count": len(sessions),
        "sessions": [
            {
                "id": s.id,
                "staff_email": s.staff_email,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "status": s.status,
                "recommendation": s.recommendation,
                "findings": s.findings
            }
            for s in sessions
        ]
    })


# ============================================
# Investigation Dashboard Page
# ============================================

@router.get("/investigation", response_class=HTMLResponse)
async def staff_investigation_dashboard(request: Request, user_info: dict = Depends(require_permission("investigation:view"))):
    """Staff Investigation Dashboard page."""
    watchlist_entries = watchlist_service.get_watchlist(include_resolved=False)
    watchlist_stats = watchlist_service.get_watchlist_stats()
    active_investigation = investigation_service.get_active_investigation(user_info.get("email", ""))

    return templates.TemplateResponse("staff/investigation.html", {
        "request": request,
        "user_info": user_info,
        "is_admin": is_admin(user_info),
        "watchlist_entries": watchlist_entries,
        "watchlist_stats": watchlist_stats,
        "active_investigation": active_investigation,
    })


# ============================================
# Whitelist Autocomplete for Staff
# ============================================

# Cache for whitelist (5 minute TTL)
_staff_whitelist_cache = {"players": [], "last_fetch": 0}
WHITELIST_CACHE_TTL = 300  # 5 minutes


def _invalidate_staff_whitelist_cache() -> None:
    _staff_whitelist_cache["players"] = []
    _staff_whitelist_cache["last_fetch"] = 0


@router.get("/api/whitelist/autocomplete")
async def staff_get_whitelist_autocomplete(user_info: dict = Depends(require_permission("whitelist:view"))):
    """Get whitelist for autocomplete (cached)."""
    import time

    current_time = time.time()

    # Check cache
    if current_time - _staff_whitelist_cache["last_fetch"] < WHITELIST_CACHE_TTL and _staff_whitelist_cache["players"]:
        return JSONResponse({
            "status": "ok",
            "players": _staff_whitelist_cache["players"],
            "cached": True
        })

    # Fetch fresh whitelist
    result = await minecraft_server.send_command("whitelist list")

    if result.get("success"):
        response = result.get("response", "")
        players = []
        if ":" in response:
            players_part = response.split(":")[-1].strip()
            if players_part:
                players = [p.strip() for p in players_part.split(",") if p.strip()]

        # Update cache
        _staff_whitelist_cache["players"] = sorted(players, key=str.lower)
        _staff_whitelist_cache["last_fetch"] = current_time

        return JSONResponse({
            "status": "ok",
            "players": _staff_whitelist_cache["players"],
            "cached": False
        })

    return JSONResponse({
        "status": "ok",
        "players": _staff_whitelist_cache["players"],  # Return stale cache on error
        "cached": True
    })


# ============================================
# Watchlist Tags Info
# ============================================

@router.get("/api/watchlist/valid-tags")
async def staff_get_valid_tags(user_info: dict = Depends(require_permission("watchlist:view"))):
    """Get list of valid watchlist tags."""
    return JSONResponse({
        "status": "ok",
        "tags": sorted(list(watchlist_service.VALID_TAGS))
    })


# ============================================
# Staff Feature Visibility Check
# ============================================

@router.get("/api/preferences")
async def staff_get_preferences(user_info: dict = Depends(require_staff)):
    """Get current user's UI preferences."""
    staff_email = user_info.get("email", "")
    return JSONResponse({
        "status": "ok",
        "preferences": user_preferences_service.get_preferences(staff_email),
    })


@router.put("/api/preferences")
async def staff_update_preferences(request: Request, user_info: dict = Depends(require_staff)):
    """Update current user's UI preferences."""
    staff_email = user_info.get("email", "")
    body = await request.json()
    patch = body.get("preferences", body) if isinstance(body, dict) else {}
    if not isinstance(patch, dict):
        return JSONResponse(
            {"status": "error", "error": "preferences must be an object"},
            status_code=400,
        )

    try:
        updated = user_preferences_service.set_preferences(
            email=staff_email,
            patch=patch,
            updated_by="self",
        )
        return JSONResponse({"status": "ok", "preferences": updated})
    except user_preferences_service.PreferenceValidationError as exc:
        return JSONResponse(
            {"status": "error", "error": "invalid preferences", "fields": exc.errors},
            status_code=400,
        )
    except Exception as exc:
        logging.getLogger(__name__).error("Failed to update staff preferences: %s", exc)
        return JSONResponse(
            {"status": "error", "error": "failed to persist preferences"},
            status_code=500,
        )

@router.get("/api/my-settings")
async def staff_get_my_settings(user_info: dict = Depends(require_staff)):
    """Get current staff member's permissions and role info."""
    staff_email = user_info.get("email", "")
    staff_mc_name = _get_hardcoded_staff_minecraft_name(staff_email)
    user_is_admin = is_admin(user_info)

    if user_is_admin:
        user_permissions = sorted(permissions_service.ALL_PERMISSIONS)
        visible_modules = sorted(set(
            m["module"] for m in permissions_service.PERMISSION_METADATA.values()
        ))
        role = "admin"
    else:
        user_permissions = sorted(permissions_service.get_effective_permissions(staff_email))
        visible_modules = permissions_service.get_user_visible_modules(staff_email)
        rbac = permissions_service.get_user_rbac(staff_email)
        role = rbac.role

    return JSONResponse({
        "status": "ok",
        "role": role,
        "permissions": user_permissions,
        "visible_modules": visible_modules,
        "preferences": user_preferences_service.get_preferences(staff_email),
        "hardcoded_minecraft_name": staff_mc_name,
        "has_hardcoded_minecraft_name": bool(staff_mc_name),
        # Backwards compatibility
        "hidden_features": staff_settings_service.get_staff_settings(staff_email).hidden_features,
    })
