# app/routers/admin_moderation.py
"""
Admin Moderation Routes

Extracted from admin.py — moderation, warnings, whitelist, CoreProtect,
watchlist, player notes, and investigation endpoints.
"""

import re
import time
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import JSONResponse

from app.core.config import PROTECTED_PLAYERS
from app.core.minecraft_access import require_minecraft_admin, require_minecraft_admin_permission
from app.services import minecraft_server
from app.services import whitelist_autocomplete_cache
from app.services.whitelist_audit import whitelist_audit_service

# Import shared Minecraft utilities
from app.services.minecraft_utils import (
    PLAYER_NAME_PATTERN, sanitize_reason,
    format_grimac_report,
)
from app.services.moderation_shared import (
    deny_if_protected,
    normalize_player,
    sanitize_moderation_reason,
    validate_player_name,
)

# Import warnings service
from app.services import warnings as warnings_service

# Import CoreProtect, watchlist, player notes, and investigation services
from app.services import coreprotect
from app.services import watchlist as watchlist_service
from app.services import player_notes as notes_service
from app.services import investigation as investigation_service

router = APIRouter()
_WHITELIST_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_.]{1,32}$")


# =============================================================================
# Admin Moderation Endpoints (Player Management)
# =============================================================================

# Rate limiting for broadcast (shared with staff but separate for admin)
_admin_broadcast_cooldowns: dict = {}
BROADCAST_COOLDOWN_SECONDS = 60


@router.post("/api/minecraft/kick")
async def admin_kick_player(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """
    Kick a player from the server (admin access - no protected player filtering).
    """
    body = await request.json()
    player = normalize_player(body.get("player", ""))
    reason = body.get("reason", "Kicked by admin").strip()

    ok, err = validate_player_name(player)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=400)

    reason = sanitize_moderation_reason(reason=reason, max_len=100, default="Kicked by admin")
    ok, err = deny_if_protected(player=player, allow_protected=True)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=403)
    command = f"kick {player} {reason}"
    result = await minecraft_server.send_command(command)

    if result.get("success"):
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


@router.post("/api/minecraft/tempban")
async def admin_tempban_player(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """
    Temporarily ban a player (admin access - no protected player filtering).
    """
    body = await request.json()
    player = normalize_player(body.get("player", ""))
    duration = body.get("duration", "").strip()
    reason = body.get("reason", "Admin action").strip()

    ok, err = validate_player_name(player)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=400)

    allowed_durations = ["1h", "6h", "24h", "7d"]
    if duration not in allowed_durations:
        return JSONResponse({
            "success": False,
            "error": f"Invalid duration. Allowed: {', '.join(allowed_durations)}"
        }, status_code=400)

    reason = sanitize_moderation_reason(reason=reason, max_len=100, default="Admin action")
    ok, err = deny_if_protected(player=player, allow_protected=True)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=403)
    command = f"tempban {player} {duration} {reason}"
    result = await minecraft_server.send_command(command)

    if result.get("success"):
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


@router.post("/api/minecraft/broadcast")
async def admin_broadcast_message(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """
    Send a server-wide broadcast message (admin access).
    """
    body = await request.json()
    message = body.get("message", "").strip()
    admin_email = user_info.get("email", "unknown")

    if not message:
        return JSONResponse({"success": False, "error": "Message is required"}, status_code=400)

    if len(message) > 200:
        return JSONResponse({
            "success": False,
            "error": "Message too long. Maximum 200 characters allowed."
        }, status_code=400)

    # Sanitize message
    message = message.replace('\n', ' ').replace('\r', ' ')
    message = re.sub(r'[/\\@]', '', message)
    message = ' '.join(message.split())

    if not message:
        return JSONResponse({"success": False, "error": "Message is empty after sanitization"}, status_code=400)

    # Rate limit check
    current_time = time.time()
    last_broadcast = _admin_broadcast_cooldowns.get(admin_email, 0)
    if current_time - last_broadcast < BROADCAST_COOLDOWN_SECONDS:
        remaining = int(BROADCAST_COOLDOWN_SECONDS - (current_time - last_broadcast))
        return JSONResponse({
            "success": False,
            "error": f"Please wait {remaining} seconds before sending another broadcast."
        }, status_code=429)

    command = f'say [ADMIN] {message}'
    result = await minecraft_server.send_command(command)

    if result.get("success"):
        _admin_broadcast_cooldowns[admin_email] = current_time
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


# =============================================================================
# Warnings
# =============================================================================

@router.post("/api/minecraft/warn")
async def admin_warn_player(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """
    Issue a warning to a player (admin access - no protected player filtering).
    """
    body = await request.json()
    player = normalize_player(body.get("player", ""))
    reason = body.get("reason", "").strip()
    notify = body.get("notify", True)
    admin_email = user_info.get("email", "unknown")

    ok, err = validate_player_name(player)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=400)

    if not reason:
        return JSONResponse({"success": False, "error": "Warning reason required"}, status_code=400)

    reason = sanitize_moderation_reason(reason=reason, max_len=200, default="Warning")
    ok, err = deny_if_protected(player=player, allow_protected=True)
    if not ok:
        return JSONResponse({"success": False, "error": err}, status_code=403)
    warning = warnings_service.issue_warning(player, reason, admin_email)
    warning_count = warnings_service.get_warning_count(player)
    escalation = warnings_service.get_escalation_recommendation(player)

    # Notify player in-game if requested
    notified = False
    if notify:
        notify_command = f'msg {player} [WARNING] You have been warned: {reason[:100]}'
        result = await minecraft_server.send_command(notify_command)
        if result.get("success"):
            warnings_service.mark_warning_notified(warning.id)
            notified = True

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


@router.get("/api/minecraft/warnings")
async def admin_get_all_warnings(
    limit: int = Query(default=50, le=100, ge=1),
    user_info: dict = Depends(require_minecraft_admin)
):
    """Get all recent warnings (admin access)."""
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


@router.get("/api/minecraft/warnings/{player}")
async def admin_get_player_warnings(player: str, user_info: dict = Depends(require_minecraft_admin)):
    """Get warning history for a specific player (admin access)."""
    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    warnings = warnings_service.get_player_warnings(player)
    escalation = warnings_service.get_escalation_recommendation(player)

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


@router.delete("/api/minecraft/warnings/{warning_id}")
async def admin_delete_warning(warning_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Delete a warning by ID (admin access - can delete any warning)."""
    admin_email = user_info.get("email", "unknown")

    warning = warnings_service.get_warning_by_id(warning_id)
    if not warning:
        return JSONResponse({
            "success": False,
            "error": "Warning not found"
        }, status_code=404)

    # Admin can delete any warning
    if warnings_service.delete_warning(warning_id, admin_email):
        return JSONResponse({
            "success": True,
            "message": f"Warning {warning_id} deleted"
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Failed to delete warning"
        }, status_code=500)


# =============================================================================
# Admin Server Logs (Less restrictive than staff)
# =============================================================================

@router.get("/api/minecraft/server-logs")
async def get_admin_server_logs(
    lines: int = Query(default=200, le=500, ge=10),
    search: Optional[str] = Query(default=None, max_length=50),
    user_info: dict = Depends(require_minecraft_admin)
):
    """
    Get server logs for admin (less restrictive filtering than staff).
    """
    all_logs = minecraft_server.get_recent_logs(lines=500, filtered=True)

    if not all_logs:
        all_logs = minecraft_server.read_latest_log(lines=500)

    # Admin logs: only mask IPs, don't filter protected players or admin commands
    filtered_logs = []
    for log in all_logs:
        message = log.get("message", "")
        # Mask IP addresses
        masked_message = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP]', message)
        filtered_logs.append({
            "time": log.get("time", ""),
            "message": masked_message
        })

    # Optional search filter
    if search:
        search_lower = search.lower()
        if re.match(r'^[a-zA-Z0-9_]{1,16}$', search):
            filtered_logs = [
                log for log in filtered_logs
                if search_lower in log.get("message", "").lower()
            ]

    result_logs = filtered_logs[-lines:]

    return JSONResponse({
        "status": "ok",
        "count": len(result_logs),
        "logs": result_logs
    })


# =============================================================================
# Admin Whitelist Management
# =============================================================================


def _invalidate_whitelist_cache() -> None:
    whitelist_autocomplete_cache.invalidate()


def _valid_whitelist_target(player: str) -> bool:
    return bool(_WHITELIST_TARGET_PATTERN.match(str(player or "").strip()))

@router.get("/api/minecraft/whitelist")
async def admin_get_whitelist(user_info: dict = Depends(require_minecraft_admin)):
    """Get current whitelist from whitelist.json with add-order preserved."""
    players = whitelist_audit_service.get_current_whitelist()

    return JSONResponse({
        "status": "ok",
        "players": players,
        "count": len(players),
    })


@router.post("/api/minecraft/whitelist/add")
async def admin_whitelist_add(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Add a player to the whitelist (admin access)."""
    body = await request.json()
    player = body.get("player", "").strip()

    if not player:
        return JSONResponse({"success": False, "error": "Player name required"}, status_code=400)

    if not _valid_whitelist_target(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name. Use letters, numbers, underscores, or dots only."
        }, status_code=400)

    result = await minecraft_server.send_command(f"whitelist add {player}")

    if result.get("success"):
        whitelist_audit_service.record_whitelist_add(
            player,
            actor_email=(user_info or {}).get("email"),
            actor_surface="admin_panel",
        )
        _invalidate_whitelist_cache()
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
async def admin_whitelist_remove(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Remove a player from the whitelist (admin access - no protected player check)."""
    body = await request.json()
    player = str(body.get("player", "")).strip()

    if not player:
        return JSONResponse({"success": False, "error": "Player name required"}, status_code=400)

    if not _valid_whitelist_target(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name. Use letters, numbers, underscores, or dots only."
        }, status_code=400)

    # Admin can remove any player (no protected player check)
    result = await minecraft_server.send_command(f"whitelist remove {player}")

    if result.get("success"):
        whitelist_audit_service.record_whitelist_remove(player)
        _invalidate_whitelist_cache()
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
async def admin_get_whitelist_audit(
    inactive_days: int = Query(default=30, ge=1, le=3650),
    refresh: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
    latest_shared: bool = Query(default=False),
    user_info: dict = Depends(require_minecraft_admin_permission("whitelist:audit")),
):
    """Return indexed whitelist activity for the current whitelist."""
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
async def admin_get_manual_prune_history(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default=""),
    status: str = Query(default="all"),
    sort: str = Query(default="removed_desc"),
    _user_info: dict = Depends(require_minecraft_admin_permission("whitelist:audit")),
):
    """Return a paginated archive of audit-based manual whitelist removals."""
    payload = whitelist_audit_service.get_manual_prune_history_page(
        limit=limit,
        offset=offset,
        q=q,
        status=status,
        sort=sort,
    )
    return JSONResponse(payload)


@router.get("/api/minecraft/whitelist/long-term-inactivity")
async def admin_get_long_term_inactivity(
    threshold_days: int = Query(default=100, ge=1, le=3650),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default=""),
    status: str = Query(default="all"),
    whitelist: str = Query(default="all"),
    sort: str = Query(default="review_desc"),
    _user_info: dict = Depends(require_minecraft_admin_permission("whitelist:audit")),
):
    """Return paginated long-term inactivity candidates for admin review."""
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
async def admin_prune_inactive_whitelist(
    request: Request,
    _audit_user_info: dict = Depends(require_minecraft_admin_permission("whitelist:audit")),
    user_info: dict = Depends(require_minecraft_admin_permission("whitelist:prune")),
):
    """
    Manually remove selected inactive players from the whitelist.

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
        if not _valid_whitelist_target(player_name):
            failed.append({"player": player_name, "error": "Invalid player name"})
            continue
        result = await minecraft_server.send_command(f"whitelist remove {player_name}")
        if result.get("success"):
            whitelist_audit_service.record_whitelist_remove(player_name)
            whitelist_audit_service.record_manual_prune(
                entry,
                actor_email=(user_info or {}).get("email"),
                actor_surface="admin_panel",
                inactive_days_threshold=inactive_days,
            )
            removed.append(
                {
                    "player": player_name,
                    "response": result.get("response", ""),
                }
            )
            continue

        failed.append(
            {
                "player": player_name,
                "error": result.get("error", "Failed to remove from whitelist"),
            }
        )

    if removed:
        _invalidate_whitelist_cache()

    partial_success = bool(removed and failed)
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


# =============================================================================
# Admin CoreProtect Lookup
# =============================================================================

@router.get("/api/minecraft/coreprotect/lookup")
async def admin_coreprotect_lookup(
    player: Optional[str] = Query(default=None, max_length=16),
    x: Optional[int] = Query(default=None),
    y: Optional[int] = Query(default=None),
    z: Optional[int] = Query(default=None),
    radius: int = Query(default=5, le=10, ge=1),
    limit: int = Query(default=50, le=100, ge=1),
    user_info: dict = Depends(require_minecraft_admin)
):
    """
    Query CoreProtect database for block changes (admin access).
    """
    if not coreprotect.is_database_available():
        return JSONResponse({
            "success": False,
            "error": "CoreProtect database not available"
        }, status_code=503)

    if not player and (x is None or y is None or z is None):
        return JSONResponse({
            "success": False,
            "error": "Provide either 'player' or 'x', 'y', 'z' coordinates"
        }, status_code=400)

    results = []

    if player:
        if not PLAYER_NAME_PATTERN.match(player):
            return JSONResponse({
                "success": False,
                "error": "Invalid player name format"
            }, status_code=400)
        results = coreprotect.lookup_by_player(player, limit=limit)
    elif x is not None and y is not None and z is not None:
        results = coreprotect.lookup_by_coordinates(x, y, z, radius=radius, limit=limit)

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
        "results": results_data
    })


# =============================================================================
# Watchlist Management (Admin Only)
# =============================================================================

@router.get("/api/watchlist/valid-tags")
async def admin_get_valid_tags(user_info: dict = Depends(require_minecraft_admin)):
    """Get list of valid watchlist tags."""
    return JSONResponse({
        "status": "ok",
        "tags": sorted(list(watchlist_service.VALID_TAGS))
    })


@router.get("/api/watchlist")
async def admin_get_watchlist(
    include_resolved: bool = Query(default=False),
    user_info: dict = Depends(require_minecraft_admin)
):
    """Get all watchlist entries (admin access)."""
    entries = watchlist_service.get_watchlist(include_resolved=include_resolved)
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
                "evidence_notes": e.evidence_notes,
                "added_by": e.added_by,
                "added_at": e.added_at,
                "status": e.status,
                "tags": e.tags,
                "updated_at": e.updated_at,
                "updated_by": e.updated_by,
                "resolved_at": e.resolved_at,
                "resolved_by": e.resolved_by,
                "resolution_notes": e.resolution_notes
            }
            for e in entries
        ]
    })


@router.post("/api/watchlist")
async def admin_add_to_watchlist(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Add a player to the watchlist (admin only)."""
    body = await request.json()
    player = body.get("player", "").strip()
    level = body.get("level", "suspicious")
    reason = body.get("reason", "").strip()
    evidence_notes = body.get("evidence_notes", "").strip()
    tags = body.get("tags", [])
    admin_email = user_info.get("email", "unknown")

    if not player:
        return JSONResponse({"success": False, "error": "Player name required"}, status_code=400)

    if not PLAYER_NAME_PATTERN.match(player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name. Use 3-16 alphanumeric characters or underscores."
        }, status_code=400)

    if not reason:
        return JSONResponse({"success": False, "error": "Reason required"}, status_code=400)

    if level not in watchlist_service.WATCHLIST_LEVELS:
        return JSONResponse({
            "success": False,
            "error": f"Invalid level. Allowed: {', '.join(watchlist_service.WATCHLIST_LEVELS)}"
        }, status_code=400)

    # Check if protected
    if watchlist_service.is_protected_player(player):
        return JSONResponse({
            "success": False,
            "error": "Cannot add protected player to watchlist"
        }, status_code=403)

    entry = watchlist_service.add_to_watchlist(
        player=player,
        level=level,
        reason=reason,
        evidence_notes=evidence_notes,
        admin_email=admin_email,
        tags=tags
    )

    if entry:
        return JSONResponse({
            "success": True,
            "message": f"Added {player} to watchlist",
            "entry": {
                "id": entry.id,
                "player": entry.player,
                "level": entry.level,
                "status": entry.status
            }
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Failed to add to watchlist. Player may already be on active watchlist."
        }, status_code=400)


@router.put("/api/watchlist/{entry_id}")
async def admin_update_watchlist_entry(
    entry_id: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_admin)
):
    """Update a watchlist entry (admin only)."""
    body = await request.json()
    admin_email = user_info.get("email", "unknown")

    entry = watchlist_service.update_watchlist_entry(
        entry_id=entry_id,
        admin_email=admin_email,
        level=body.get("level"),
        reason=body.get("reason"),
        evidence_notes=body.get("evidence_notes"),
        tags=body.get("tags")
    )

    if entry:
        return JSONResponse({
            "success": True,
            "message": "Watchlist entry updated",
            "entry": {
                "id": entry.id,
                "player": entry.player,
                "level": entry.level,
                "status": entry.status,
                "updated_at": entry.updated_at
            }
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Entry not found or invalid update data"
        }, status_code=404)


@router.delete("/api/watchlist/{entry_id}")
async def admin_delete_watchlist_entry(entry_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Delete a watchlist entry permanently (admin only)."""
    admin_email = user_info.get("email", "unknown")

    if watchlist_service.delete_watchlist_entry(entry_id, admin_email):
        return JSONResponse({
            "success": True,
            "message": f"Watchlist entry {entry_id} deleted"
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Entry not found"
        }, status_code=404)


@router.post("/api/watchlist/{entry_id}/resolve")
async def admin_resolve_watchlist_entry(
    entry_id: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_admin)
):
    """Resolve a watchlist entry (admin only)."""
    body = await request.json()
    resolution = body.get("resolution", "resolved")
    notes = body.get("notes", "")
    admin_email = user_info.get("email", "unknown")

    if resolution not in ["resolved", "false-positive"]:
        return JSONResponse({
            "success": False,
            "error": "Invalid resolution. Use 'resolved' or 'false-positive'"
        }, status_code=400)

    entry = watchlist_service.resolve_watchlist_entry(
        entry_id=entry_id,
        admin_email=admin_email,
        resolution=resolution,
        notes=notes
    )

    if entry:
        return JSONResponse({
            "success": True,
            "message": f"Entry marked as {resolution}",
            "entry": {
                "id": entry.id,
                "player": entry.player,
                "status": entry.status,
                "resolved_at": entry.resolved_at
            }
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Entry not found"
        }, status_code=404)


# =============================================================================
# Player Notes (Admin can view all and delete any)
# =============================================================================

@router.get("/api/notes/{player}")
async def admin_get_player_notes(player: str, user_info: dict = Depends(require_minecraft_admin)):
    """Get all notes for a player (admin access)."""
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
async def admin_add_note(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Add a note about a player (admin access)."""
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
async def admin_update_note(note_id: str, request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Update a note (admin can only edit own notes)."""
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
async def admin_delete_note(note_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Delete a note (admin can delete any note)."""
    author_email = user_info.get("email", "unknown")

    if notes_service.delete_note(note_id, author_email, is_admin=True):
        return JSONResponse({
            "success": True,
            "message": f"Note {note_id} deleted"
        })
    else:
        return JSONResponse({
            "success": False,
            "error": "Note not found"
        }, status_code=404)


# =============================================================================
# Investigation Commands (Admin - Direct Access)
# =============================================================================

@router.get("/api/investigation/grimac/{player}")
async def admin_run_grimac(player: str, user_info: dict = Depends(require_minecraft_admin)):
    """
    Get GrimAC violation history for a player from the database (admin access - no watchlist restriction).
    """
    from app.services import grimac as grimac_service

    admin_email = user_info.get("email", "unknown")

    # Validate player name
    if not re.match(r'^[a-zA-Z0-9_]{3,16}$', player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    # Query the GrimAC database directly - get more records
    result = grimac_service.get_player_violations(player, limit=100)

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
async def admin_run_mtrack(player: str, user_info: dict = Depends(require_minecraft_admin)):
    """
    Run mtrack check command for a player (admin access - no watchlist restriction).
    """
    admin_email = user_info.get("email", "unknown")

    # Validate player name
    if not re.match(r'^[a-zA-Z0-9_]{3,16}$', player):
        return JSONResponse({
            "success": False,
            "error": "Invalid player name format"
        }, status_code=400)

    # Run the command directly (admin has no restrictions)
    result = await minecraft_server.send_command(f"mtrack check {player}")

    return JSONResponse({
        "success": result.get("success", False),
        "response": result.get("response", ""),
        "error": result.get("error")
    })


# =============================================================================
# Whitelist Cache for Autocomplete
# =============================================================================

@router.get("/api/whitelist/autocomplete")
async def get_whitelist_autocomplete(user_info: dict = Depends(require_minecraft_admin)):
    """Get whitelist for autocomplete (cached)."""
    cached_players = whitelist_autocomplete_cache.get_fresh_players()
    if cached_players is not None:
        return JSONResponse({
            "status": "ok",
            "players": cached_players,
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

        cached_players = whitelist_autocomplete_cache.store_players(players)

        return JSONResponse({
            "status": "ok",
            "players": cached_players,
            "cached": False
        })

    return JSONResponse({
        "status": "ok",
        "players": whitelist_autocomplete_cache.get_stale_players(),
        "cached": True
    })
