# app/routers/admin_server.py
"""
Admin Server Control, Console, Log, and WebSocket Endpoints

Extracted from admin.py for modularity.
"""

import asyncio
import html
import json as json_module
import logging
import os
import re
import time
from base64 import b64decode
from urllib.parse import parse_qs
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import TimestampSigner

from app.core.config import TEMPLATES_DIR, APP_VERSION
from app.core.minecraft_access import (
    require_minecraft_admin,
    require_minecraft_owner_or_manager_admin,
    is_minecraft_admin_email,
)
from app.services import minecraft_updater
from app.services import minecraft_update_automation
from app.services import minecraft_server
from app.services import minecraft_settings
from app.services import minecraft_setup
from app.services import minecraft_setup_executor

# Audit logger for admin actions
admin_audit_logger = logging.getLogger("admin_audit")
admin_audit_logger.setLevel(logging.INFO)
if not admin_audit_logger.handlers:
    from pathlib import Path
    _logs_dir = Path("logs")
    _logs_dir.mkdir(exist_ok=True)
    _handler = logging.FileHandler(_logs_dir / "admin_audit.log")
    _handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    admin_audit_logger.addHandler(_handler)

# RCON command security
# NOTE: OP/DEOP are intentionally left commented for now (temporarily allowed for admin console operations).
DANGEROUS_COMMANDS = frozenset({
    "stop",
    # "op",
    # "deop",
    "ban-ip",
    "pardon-ip",
})
_command_rate_limits: dict = {}  # email -> list of timestamps
COMMAND_RATE_LIMIT = 10  # max commands per minute
LOG_WS_QUEUE_SIZE = 500


def _enqueue_latest(queue: asyncio.Queue, item: dict) -> None:
    """Bound slow log consumers without blocking the log producer callback."""
    try:
        queue.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass

    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass

    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass
COMMAND_RATE_WINDOW = 60  # seconds

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def _authorize_admin_websocket(websocket: WebSocket) -> bool:
    """Authorize admin WebSocket connections using signed session cookie."""
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        await websocket.close(code=4003, reason="Server config error")
        return False

    cookie_header = websocket.headers.get("cookie", "")
    session_cookie = None
    for cookie in cookie_header.split(";"):
        cookie = cookie.strip()
        if cookie.startswith("session="):
            session_cookie = cookie[len("session="):]
            break

    if not session_cookie:
        await websocket.close(code=4003, reason="No session cookie")
        return False

    try:
        signer = TimestampSigner(secret_key)
        data = signer.unsign(session_cookie.encode("utf-8"), max_age=14 * 24 * 60 * 60)
        session_data = json_module.loads(b64decode(data))
    except Exception:
        await websocket.close(code=4003, reason="Invalid session")
        return False

    user_info = session_data.get("user_info")
    if not user_info or not is_minecraft_admin_email(user_info.get("email", "")):
        await websocket.close(code=4003, reason="Forbidden")
        return False

    return True


@router.get("/api/minecraft/status")
async def get_minecraft_status(user_info: dict = Depends(require_minecraft_admin)):
    """Get Minecraft server status and plugin versions"""
    versions_data = minecraft_updater.load_versions()
    server_status = minecraft_updater.get_server_status()

    return JSONResponse({
        "status": "ok",
        "minecraft_version": versions_data.get("minecraft_version"),
        "last_check": versions_data.get("last_check"),
        "plugins": versions_data.get("plugins", {}),
        "server": server_status
    })


@router.get("/log", response_class=HTMLResponse)
async def minecraft_dev_log(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Developer-only raw log viewer page"""
    return templates.TemplateResponse("admin/log.html", {
        "request": request,
        "user_info": user_info,
        "is_admin": True
    })


@router.get("/api/minecraft/server/status")
async def get_server_status(user_info: dict = Depends(require_minecraft_admin)):
    """Get detailed server running status"""
    status = minecraft_server.get_server_status()
    rcon_config = minecraft_server.get_rcon_config()

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
            "players": status.players or [],
            "version": status.version,
            "stale": getattr(status, "stale", False),
            "last_updated": getattr(status, "last_updated", None),
        },
        "rcon": {
            "enabled": rcon_config.enabled,
            "port": rcon_config.port
        }
    })


@router.get("/api/minecraft/server-icon.png")
async def get_server_icon(user_info: dict = Depends(require_minecraft_admin)):
    """Serve the configured Minecraft server icon."""
    icon_path = minecraft_settings.get_server_icon_path()
    if icon_path is None:
        raise HTTPException(status_code=404, detail="Minecraft server icon not found")
    return FileResponse(
        icon_path,
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/api/minecraft/server-directory")
@router.get("/api/minecraft/server/directory")
async def get_server_directory_settings(user_info: dict = Depends(require_minecraft_admin)):
    """Get the configured Minecraft server directory."""
    return JSONResponse({
        "status": "ok",
        "settings": minecraft_settings.get_settings(),
    })


@router.put("/api/minecraft/server-directory")
@router.put("/api/minecraft/server/directory")
async def update_server_directory_settings(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Update the global Minecraft server directory after validation."""
    body = await request.json()
    raw_path = body.get("server_directory", body.get("serverDirectory", ""))

    try:
        proposed_path = minecraft_settings.validate_server_directory(raw_path)
    except minecraft_settings.PathValidationError as exc:
        return JSONResponse(
            {
                "status": "error",
                "error": "Invalid Minecraft server directory",
                "errors": exc.errors,
                "warnings": exc.warnings,
            },
            status_code=400,
        )

    current_path = minecraft_settings.get_server_directory()
    path_changed = proposed_path != current_path

    if path_changed and minecraft_server.is_server_running():
        return JSONResponse(
            {
                "status": "error",
                "error": "Stop the Minecraft server before changing the server directory.",
            },
            status_code=409,
        )

    try:
        from app.services.backup_scheduler import get_backup_scheduler
        backup_state = get_backup_scheduler().get_status().get("state")
    except Exception:
        backup_state = None

    if path_changed and backup_state in {"countdown", "stopping_server", "compressing", "uploading", "restarting"}:
        return JSONResponse(
            {
                "status": "error",
                "error": "Wait for the current backup operation to finish before changing the server directory.",
            },
            status_code=409,
        )

    settings = minecraft_settings.set_server_directory(
        proposed_path,
        updated_by=user_info.get("email", "unknown"),
    )
    minecraft_server.reset_path_dependent_runtime_state()

    admin_audit_logger.info(
        "server_directory_updated | admin=%s | path=%s",
        user_info.get("email", "unknown"),
        settings["server_directory"],
    )

    return JSONResponse({
        "status": "ok",
        "settings": settings,
        "changed": path_changed,
    })


def _profile_api_error(
    *,
    error_code: str,
    error: str,
    status_code: int,
    errors: dict | None = None,
    warnings: list[str] | None = None,
) -> JSONResponse:
    payload = {
        "status": "error",
        "success": False,
        "error_code": error_code,
        "error": error,
    }
    if errors is not None:
        payload["errors"] = errors
    if warnings is not None:
        payload["warnings"] = warnings
    return JSONResponse(payload, status_code=status_code)


def _profile_not_found_response(profile_id: str) -> JSONResponse:
    return _profile_api_error(
        error_code="profile_not_found",
        error=f"Minecraft server profile '{profile_id}' was not found.",
        status_code=404,
    )


def _coerce_optional_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise minecraft_settings.ProfileValidationError({"profile": "Boolean fields must be true or false."})


def _profile_payload_from_body(body: dict, *, partial: bool) -> dict:
    payload = {}
    if "id" in body or "profile_id" in body:
        payload["profile_id"] = body.get("id", body.get("profile_id"))
    if "name" in body:
        payload["name"] = body.get("name")
    if "kind" in body:
        payload["kind"] = body.get("kind")
    if "server_directory" in body or "serverDirectory" in body:
        payload["server_directory"] = body.get("server_directory", body.get("serverDirectory"))
    for field in ("operations_enabled", "rcon_enabled", "readonly"):
        camel_name = field.split("_")[0] + "".join(part.title() for part in field.split("_")[1:])
        if field in body or camel_name in body:
            payload[field] = _coerce_optional_bool(body.get(field, body.get(camel_name)))

    if not partial:
        if "name" not in payload:
            payload["name"] = body.get("name", "")
        if "server_directory" not in payload:
            payload["server_directory"] = body.get("server_directory", body.get("serverDirectory", ""))

    if payload.get("readonly") is True or str(payload.get("kind") or "").strip().lower() == "sample":
        payload["operations_enabled"] = False
        payload["rcon_enabled"] = False
    return payload


def _profile_has_pid_file(profile_id: str) -> bool:
    return (minecraft_settings.get_profile_state_dir(profile_id) / "server.pid").exists()


def _active_directory_change_block(proposed_path) -> JSONResponse | None:
    current_path = minecraft_settings.get_server_directory()
    if proposed_path == current_path:
        return None

    if minecraft_server.is_server_running():
        return _profile_api_error(
            error_code="server_directory_invalid",
            error="Stop the Minecraft server before changing the active profile server directory.",
            status_code=409,
        )

    try:
        from app.services.backup_scheduler import get_backup_scheduler
        backup_state = get_backup_scheduler().get_status().get("state")
    except Exception:
        backup_state = None

    if backup_state in {"countdown", "stopping_server", "compressing", "uploading", "restarting"}:
        return _profile_api_error(
            error_code="server_directory_invalid",
            error="Wait for the current backup operation to finish before changing the active profile server directory.",
            status_code=409,
        )
    return None


@router.get("/api/minecraft/server-profiles")
async def list_server_profiles(user_info: dict = Depends(require_minecraft_admin)):
    """List Minecraft server profiles for the admin settings UI."""
    settings = minecraft_settings.get_settings()
    return JSONResponse({
        "status": "ok",
        "active_profile_id": settings["active_profile_id"],
        "active_profile": settings["active_profile"],
        "profiles": settings["profiles"],
    })


@router.post("/api/minecraft/server-profiles")
async def create_server_profile(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Create a Minecraft server profile without activating or starting it."""
    body = await request.json()
    if not isinstance(body, dict):
        return _profile_api_error(
            error_code="profile_invalid",
            error="Profile request body must be an object.",
            status_code=400,
        )

    try:
        payload = _profile_payload_from_body(body, partial=False)
        profile = minecraft_settings.create_profile(
            profile_id=payload.get("profile_id"),
            name=payload["name"],
            kind=str(payload.get("kind") or "live").strip() or "live",
            server_directory=payload["server_directory"],
            operations_enabled=payload.get("operations_enabled", True),
            rcon_enabled=payload.get("rcon_enabled", True),
            readonly=payload.get("readonly", False),
            set_active=False,
        )
    except minecraft_settings.PathValidationError as exc:
        return _profile_api_error(
            error_code="server_directory_invalid",
            error="Invalid Minecraft server directory.",
            errors=exc.errors,
            warnings=exc.warnings,
            status_code=400,
        )
    except minecraft_settings.ProfileValidationError as exc:
        return _profile_api_error(
            error_code="profile_invalid",
            error="Invalid Minecraft server profile.",
            errors=exc.errors,
            status_code=400,
        )

    admin_audit_logger.info(
        "server_profile_created | admin=%s | profile=%s",
        user_info.get("email", "unknown"),
        profile["id"],
    )
    return JSONResponse({"status": "ok", "profile": profile}, status_code=201)


@router.put("/api/minecraft/server-profiles/{profile_id}")
async def update_server_profile(
    profile_id: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_admin),
):
    """Update mutable Minecraft server profile metadata."""
    body = await request.json()
    if not isinstance(body, dict):
        return _profile_api_error(
            error_code="profile_invalid",
            error="Profile request body must be an object.",
            status_code=400,
        )

    try:
        current_profile = minecraft_settings.get_profile(profile_id)
    except KeyError:
        return _profile_not_found_response(profile_id)

    proposed_path = None
    try:
        payload = _profile_payload_from_body(body, partial=True)
        if "server_directory" in payload:
            proposed_path = minecraft_settings.validate_server_directory(payload["server_directory"])
            if current_profile["id"] == minecraft_settings.get_settings()["active_profile_id"]:
                block = _active_directory_change_block(proposed_path)
                if block is not None:
                    return block

        profile = minecraft_settings.update_profile(
            profile_id,
            name=payload.get("name"),
            server_directory=proposed_path,
            operations_enabled=payload.get("operations_enabled"),
            rcon_enabled=payload.get("rcon_enabled"),
            readonly=payload.get("readonly"),
        )
    except minecraft_settings.PathValidationError as exc:
        return _profile_api_error(
            error_code="server_directory_invalid",
            error="Invalid Minecraft server directory.",
            errors=exc.errors,
            warnings=exc.warnings,
            status_code=400,
        )
    except minecraft_settings.ProfileValidationError as exc:
        return _profile_api_error(
            error_code="profile_invalid",
            error="Invalid Minecraft server profile.",
            errors=exc.errors,
            status_code=400,
        )

    if proposed_path is not None and current_profile["id"] == minecraft_settings.get_settings()["active_profile_id"]:
        minecraft_server.reset_path_dependent_runtime_state()

    admin_audit_logger.info(
        "server_profile_updated | admin=%s | profile=%s",
        user_info.get("email", "unknown"),
        profile["id"],
    )
    return JSONResponse({"status": "ok", "profile": profile})


@router.post("/api/minecraft/server-profiles/{profile_id}/activate")
async def activate_server_profile(profile_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Switch the active profile pointer without touching server processes."""
    try:
        settings = minecraft_settings.set_active_profile(profile_id)
    except KeyError:
        return _profile_not_found_response(profile_id)

    minecraft_server.reset_path_dependent_runtime_state()
    admin_audit_logger.info(
        "server_profile_activated | admin=%s | profile=%s",
        user_info.get("email", "unknown"),
        settings["active_profile_id"],
    )
    return JSONResponse({
        "status": "ok",
        "active_profile_id": settings["active_profile_id"],
        "active_profile": settings["active_profile"],
        "profiles": settings["profiles"],
    })


@router.delete("/api/minecraft/server-profiles/{profile_id}")
async def delete_server_profile(profile_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Delete profile metadata only when no active/profile-state safety block exists."""
    try:
        profile = minecraft_settings.get_profile(profile_id)
        settings = minecraft_settings.get_settings()
    except KeyError:
        return _profile_not_found_response(profile_id)

    if profile["id"] == settings["active_profile_id"]:
        return _profile_api_error(
            error_code="profile_delete_active",
            error="Cannot delete the active Minecraft server profile.",
            status_code=409,
        )

    if profile["id"] == minecraft_settings.SAMPLE_PROFILE_ID:
        return _profile_api_error(
            error_code="profile_delete_sample",
            error="Cannot delete the default Sample Server profile.",
            status_code=409,
        )

    if _profile_has_pid_file(profile["id"]):
        return _profile_api_error(
            error_code="profile_delete_has_pid",
            error="Cannot delete a profile while its profile state contains server.pid.",
            status_code=409,
        )

    try:
        settings = minecraft_settings.delete_profile(profile["id"])
    except minecraft_settings.ProfileValidationError as exc:
        return _profile_api_error(
            error_code="profile_invalid",
            error="Invalid Minecraft server profile.",
            errors=exc.errors,
            status_code=400,
        )

    admin_audit_logger.info(
        "server_profile_deleted | admin=%s | profile=%s",
        user_info.get("email", "unknown"),
        profile["id"],
    )
    return JSONResponse({
        "status": "ok",
        "active_profile_id": settings["active_profile_id"],
        "profiles": settings["profiles"],
    })


@router.get("/api/minecraft/players")
async def get_admin_online_players(user_info: dict = Depends(require_minecraft_admin)):
    """Get list of online players for admin panel"""
    snapshot = minecraft_server.get_online_players_snapshot()
    if not snapshot.get("running"):
        return JSONResponse({"status": "ok", "players": [], "message": "Server offline"})

    if snapshot.get("error") and not snapshot.get("players"):
        return JSONResponse({"status": "error", "error": snapshot["error"]}, status_code=500)

    players = snapshot.get("players", [])
    return JSONResponse({
        "status": "ok",
        "players": players,
        "count": len(players),
        "stale": bool(snapshot.get("stale")),
        "last_updated": snapshot.get("last_updated"),
    })


@router.post("/api/minecraft/server/start")
async def start_server(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Start the Minecraft server"""
    from app.services.operations import execute_operation
    result = await execute_operation(
        key="server:start",
        user_info=user_info,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(result)


@router.post("/api/minecraft/server/stop")
async def stop_server(request: Request, force: bool = False, user_info: dict = Depends(require_minecraft_admin)):
    """Stop the Minecraft server"""
    from app.services.operations import execute_operation
    result = await execute_operation(
        key="server:stop",
        user_info=user_info,
        params={"force": force},
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(result)


@router.post("/api/minecraft/server/restart")
async def restart_server(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Restart the Minecraft server"""
    from app.services.operations import execute_operation
    result = await execute_operation(
        key="server:restart",
        user_info=user_info,
        params={"source": "admin_ui"},
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(result)


@router.post("/api/minecraft/server/recover")
async def recover_server(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Emergency recovery when UI/server state diverges."""
    from app.services.operations import execute_operation
    result = await execute_operation(
        key="server:recover",
        user_info=user_info,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(result)


@router.post("/api/minecraft/server/command")
async def send_server_command(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Arbitrary RCON command execution is disabled in the public extraction."""
    admin_email = user_info.get("email", "unknown")
    from app.services.audit_log import audit_event

    audit_event(
        logger=admin_audit_logger,
        actor=admin_email,
        action="rcon_command_disabled",
        target="public_extract",
        result="denied",
    )
    return JSONResponse(
        {
            "success": False,
            "error": "Arbitrary RCON command execution is disabled in this public extraction.",
        },
        status_code=403,
    )


@router.get("/api/minecraft/server/logs")
async def get_server_logs(lines: int = 100, offset: int = 0, user_info: dict = Depends(require_minecraft_admin)):
    """Get server console logs with pagination support"""
    lines = minecraft_server.clamp_log_line_limit(lines, default=100)
    # Try to get live logs first, fall back to file
    logs = minecraft_server.get_recent_logs(lines, offset=offset)

    if not logs and offset == 0:
        # Fall back to latest.log file only for initial load
        logs = minecraft_server.read_latest_log(lines)

    return JSONResponse({
        "status": "ok",
        "count": len(logs),
        "line_limit": lines,
        "logs": logs,
        "has_more": len(logs) == lines  # If we got full page, there might be more
    })


@router.get("/api/minecraft/server/full-log")
async def get_full_server_log(lines: int = 1000, user_info: dict = Depends(require_minecraft_admin)):
    """Get FULL server log from latest.log file (for developer debugging)"""
    lines = minecraft_server.clamp_log_line_limit(lines, default=1000)
    result = minecraft_server.read_log_file_tail(minecraft_server.get_latest_log_path(), lines=lines)
    if result.get("error"):
        return JSONResponse({"status": "error", **result}, status_code=413)
    return JSONResponse({
        "status": "ok",
        "count": result["count"],
        "line_limit": result["line_limit"],
        "truncated": result["truncated"],
        "logs": result["logs"]
    })


@router.get("/api/minecraft/server/log-files")
async def list_log_files(user_info: dict = Depends(require_minecraft_admin)):
    """List all available log files (latest.log and archived .gz files)"""
    logs_dir = minecraft_server.get_logs_dir()
    log_files = []

    if logs_dir.exists():
        # Add latest.log first
        latest = logs_dir / "latest.log"
        if latest.exists():
            stat = latest.stat()
            log_files.append({
                "name": "latest.log",
                "size": stat.st_size,
                "modified": stat.st_mtime
            })

        # Add archived .gz files sorted by date (newest first)
        gz_files = sorted(logs_dir.glob("*.log.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in gz_files:
            stat = f.stat()
            log_files.append({
                "name": f.name,
                "size": stat.st_size,
                "modified": stat.st_mtime
            })

    return JSONResponse({"status": "ok", "files": log_files})


@router.get("/api/minecraft/server/log-file/{filename:path}")
async def get_log_file(filename: str, lines: int = 1000, user_info: dict = Depends(require_minecraft_admin)):
    """Load a specific log file by name (supports .gz files)"""
    logs_dir = minecraft_server.get_logs_dir()
    log_path = logs_dir / filename

    # Security check: ensure path is within logs directory
    try:
        log_path.resolve().relative_to(logs_dir.resolve())
    except ValueError:
        return JSONResponse({"status": "error", "message": "Invalid file path"}, status_code=400)

    if not log_path.exists():
        return JSONResponse({"status": "error", "message": "File not found"}, status_code=404)

    try:
        result = minecraft_server.read_log_file_tail(log_path, lines=lines)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    if result.get("error"):
        return JSONResponse({
            "status": "error",
            "filename": filename,
            **result,
        }, status_code=413)

    return JSONResponse({
        "status": "ok",
        "filename": filename,
        "count": result["count"],
        "line_limit": result["line_limit"],
        "truncated": result["truncated"],
        "source_lines": result["source_lines"],
        "logs": result["logs"]
    })


@router.websocket("/ws/minecraft/logs")
async def websocket_logs(websocket: WebSocket):
    """WebSocket endpoint for real-time log streaming"""
    if not await _authorize_admin_websocket(websocket):
        return
    await websocket.accept()

    log_queue = asyncio.Queue(maxsize=LOG_WS_QUEUE_SIZE)
    sent_logs = set()  # Track sent log hashes to prevent duplicates

    async def log_callback(log_entry):
        # Create a hash of the log entry for deduplication
        log_hash = f"{log_entry.get('time', '')}:{log_entry.get('message', '')}"
        if log_hash not in sent_logs:
            _enqueue_latest(log_queue, log_entry)

    # Subscribe FIRST to not miss any logs during initial send
    minecraft_server.subscribe_to_logs(log_callback)

    try:
        # Send recent logs and track what we sent
        recent = minecraft_server.get_recent_logs(50)
        for log in recent:
            log_hash = f"{log.get('time', '')}:{log.get('message', '')}"
            sent_logs.add(log_hash)
            await websocket.send_json(log)

        # Stream new logs (only those not already sent)
        while True:
            try:
                log_entry = await asyncio.wait_for(log_queue.get(), timeout=30.0)
                log_hash = f"{log_entry.get('time', '')}:{log_entry.get('message', '')}"
                if log_hash not in sent_logs:
                    sent_logs.add(log_hash)
                    await websocket.send_json(log_entry)
                    # Limit size of tracking set
                    if len(sent_logs) > 1000:
                        sent_logs.clear()
            except asyncio.TimeoutError:
                # Send heartbeat
                await websocket.send_json({"type": "heartbeat"})

    except WebSocketDisconnect:
        pass
    finally:
        minecraft_server.unsubscribe_from_logs(log_callback)


@router.websocket("/ws/minecraft/raw-logs")
async def websocket_raw_logs(websocket: WebSocket):
    """WebSocket endpoint for RAW log streaming (no filtering, for dev log viewer)"""
    if not await _authorize_admin_websocket(websocket):
        return
    await websocket.accept()

    log_queue = asyncio.Queue(maxsize=LOG_WS_QUEUE_SIZE)
    sent_logs = set()

    async def log_callback(log_entry):
        log_hash = f"{log_entry.get('time', '')}:{log_entry.get('message', '')}"
        if log_hash not in sent_logs:
            _enqueue_latest(log_queue, log_entry)

    minecraft_server.subscribe_to_logs(log_callback)

    try:
        # Send ALL recent logs (unfiltered, more lines for debugging)
        recent = minecraft_server.get_recent_logs(200, filtered=False)
        for log in recent:
            log_hash = f"{log.get('time', '')}:{log.get('message', '')}"
            sent_logs.add(log_hash)
            await websocket.send_json(log)

        # Stream new logs without filtering
        while True:
            try:
                log_entry = await asyncio.wait_for(log_queue.get(), timeout=30.0)
                log_hash = f"{log_entry.get('time', '')}:{log_entry.get('message', '')}"
                if log_hash not in sent_logs:
                    sent_logs.add(log_hash)
                    await websocket.send_json(log_entry)
                    if len(sent_logs) > 2000:
                        sent_logs.clear()
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})

    except WebSocketDisconnect:
        pass
    finally:
        minecraft_server.unsubscribe_from_logs(log_callback)


# =============================================================================
# Full Update Flow (with server control)
# =============================================================================

@router.post("/api/minecraft/update/{plugin_id}")
async def apply_plugin_update(plugin_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """
    Apply update for a specific plugin

    Requires prior update check to populate pending update info.
    """
    try:
        # First check for the latest version
        versions_data = minecraft_updater.load_versions()
        plugin_config = versions_data.get("plugins", {}).get(plugin_id)

        if not plugin_config:
            raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found in tracking")

        minecraft_version = versions_data.get("minecraft_version", "1.21.1")

        # Check for update
        update_check = await minecraft_updater.check_plugin_update(
            plugin_id, plugin_config, minecraft_version
        )

        if not update_check.has_update:
            return JSONResponse({
                "status": "no_update",
                "message": f"{plugin_id} is already up to date (v{update_check.current_version})"
            })

        # Apply the update
        log = await minecraft_updater.apply_update(plugin_id, update_check)

        return JSONResponse({
            "status": log.status,
            "plugin_id": plugin_id,
            "from_version": log.from_version,
            "to_version": log.to_version,
            "steps": log.steps,
            "error": log.error
        })

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e)
        }, status_code=500)


@router.post("/api/minecraft/update-with-restart/{plugin_id}")
async def apply_update_with_restart(plugin_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """
    Full update flow: stop server -> apply update -> start server
    """
    steps = []
    server_was_running = minecraft_server.is_server_running()

    try:
        # Step 1: Stop server if running
        if server_was_running:
            steps.append({"step": "stop_server", "status": "started"})
            stop_result = await minecraft_server.stop_server()
            if not stop_result["success"]:
                steps.append({"step": "stop_server", "status": "failed", "error": stop_result.get("error")})
                return JSONResponse({
                    "status": "failed",
                    "error": f"Failed to stop server: {stop_result.get('error')}",
                    "steps": steps
                }, status_code=500)
            steps.append({"step": "stop_server", "status": "completed"})
            await asyncio.sleep(3)

        # Step 2: Apply update
        steps.append({"step": "apply_update", "status": "started"})

        versions_data = minecraft_updater.load_versions()
        plugin_config = versions_data.get("plugins", {}).get(plugin_id)

        if not plugin_config:
            raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")

        minecraft_version = versions_data.get("minecraft_version", "1.21.11")
        update_check = await minecraft_updater.check_plugin_update(
            plugin_id, plugin_config, minecraft_version
        )

        if not update_check.has_update:
            steps.append({"step": "apply_update", "status": "skipped", "reason": "already up to date"})
        else:
            update_log = await minecraft_updater.apply_update(plugin_id, update_check)
            if update_log.status != "success":
                steps.append({"step": "apply_update", "status": "failed", "error": update_log.error})
                return JSONResponse({
                    "status": "failed",
                    "error": f"Update failed: {update_log.error}",
                    "steps": steps
                }, status_code=500)
            steps.append({
                "step": "apply_update",
                "status": "completed",
                "from_version": update_log.from_version,
                "to_version": update_log.to_version
            })

        # Step 3: Restart server if it was running
        if server_was_running:
            steps.append({"step": "start_server", "status": "started"})
            await asyncio.sleep(2)

            start_result = await minecraft_server.start_server()
            if not start_result["success"]:
                steps.append({"step": "start_server", "status": "failed", "error": start_result.get("error")})
                return JSONResponse({
                    "status": "partial",
                    "message": "Update applied but server failed to start",
                    "error": start_result.get("error"),
                    "steps": steps
                }, status_code=500)
            steps.append({"step": "start_server", "status": "completed", "pid": start_result.get("pid")})

        return JSONResponse({
            "status": "success",
            "message": f"Update completed" + (" and server restarted" if server_was_running else ""),
            "steps": steps
        })

    except HTTPException:
        raise
    except Exception as e:
        steps.append({"step": "error", "error": str(e)})
        return JSONResponse({
            "status": "failed",
            "error": str(e),
            "steps": steps
        }, status_code=500)


@router.post("/api/minecraft/check-updates")
async def trigger_update_check(user_info: dict = Depends(require_minecraft_admin)):
    """Manually trigger update check for all tracked plugins"""
    try:
        automation_config = minecraft_update_automation.get_config()
        results = await minecraft_updater.check_all_updates(
            excluded_plugins=automation_config.get("excluded_plugins", [])
        )

        # Convert to serializable format
        updates = []
        for result in results:
            updates.append({
                "plugin_id": result.plugin_id,
                "source": result.source,
                "current_version": result.current_version,
                "latest_version": result.latest_version,
                "has_update": result.has_update,
                "download_url": result.download_url,
                "filename": result.filename,
                "changelog": result.changelog[:500] if result.changelog else None,
                "current_full_version": result.current_full_version,
                "latest_full_version": result.latest_full_version
            })

        return JSONResponse({
            "status": "ok",
            "checked_at": minecraft_updater.load_versions().get("last_check"),
            "updates": updates,
            "updates_available": sum(1 for u in updates if u["has_update"]),
            "excluded_plugins": automation_config.get("excluded_plugins", []),
        })

    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e)
        }, status_code=500)


@router.get("/api/minecraft/update-automation/config")
async def get_update_automation_config(user_info: dict = Depends(require_minecraft_owner_or_manager_admin)):
    """Get plugin update automation settings."""
    return JSONResponse({
        "status": "ok",
        "config": minecraft_update_automation.get_config(),
    })


@router.post("/api/minecraft/update-automation/config")
async def update_update_automation_config(request: Request, user_info: dict = Depends(require_minecraft_owner_or_manager_admin)):
    """Update plugin update automation settings."""
    body = await request.json()
    allowed = {
        "enabled",
        "check_interval_hours",
        "check_hour",
        "check_minute",
        "auto_apply",
        "restart_after_apply",
        "restart_message_label",
        "excluded_plugins",
    }
    patch = {key: body[key] for key in allowed if key in body}
    if not patch:
        return JSONResponse({"success": False, "error": "No valid config keys provided"}, status_code=400)
    result = minecraft_update_automation.update_config(**patch)
    return JSONResponse(result)


@router.get("/api/minecraft/update-automation/logs")
async def get_update_automation_logs(limit: int = 50, user_info: dict = Depends(require_minecraft_owner_or_manager_admin)):
    """Get plugin update automation logs."""
    return JSONResponse({
        "status": "ok",
        "logs": minecraft_update_automation.get_logs(limit=limit),
    })


@router.post("/api/minecraft/update-automation/run")
async def run_update_automation_now(user_info: dict = Depends(require_minecraft_owner_or_manager_admin)):
    """Run plugin update automation immediately."""
    actor = user_info.get("email") or user_info.get("name") or "admin"
    result = await minecraft_update_automation.run_once(manual=True, actor=actor)
    status_code = 200 if result.get("status") != "failed" else 500
    return JSONResponse(result, status_code=status_code)


@router.get("/api/minecraft/setup/defaults")
async def get_minecraft_setup_defaults(user_info: dict = Depends(require_minecraft_admin)):
    """Return read-only defaults for the Minecraft setup preview."""
    return JSONResponse({
        "status": "ok",
        **minecraft_setup.build_setup_defaults(),
    })


@router.post("/api/minecraft/setup/choose-folder")
async def choose_minecraft_setup_folder(user_info: dict = Depends(require_minecraft_owner_or_manager_admin)):
    """Open a native folder picker for setup preview without persisting anything."""
    try:
        selected_path = await asyncio.to_thread(minecraft_setup.choose_setup_server_directory)
    except minecraft_setup.SetupFolderPickerCancelled as exc:
        return JSONResponse({
            "status": "cancelled",
            "error": str(exc),
        }, status_code=400)
    except minecraft_setup.SetupFolderPickerUnavailable as exc:
        return JSONResponse({
            "status": "error",
            "error": str(exc),
        }, status_code=500)
    except Exception as exc:
        return JSONResponse({
            "status": "error",
            "error": str(exc),
        }, status_code=500)

    return JSONResponse({
        "status": "ok",
        "path": selected_path,
    })


@router.post("/api/minecraft/setup/preview")
async def preview_minecraft_setup(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Render a read-only Minecraft setup preview without creating anything."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({
            "status": "error",
            "error_code": "setup_preview_invalid",
            "error": "Request body must be valid JSON.",
        }, status_code=400)

    try:
        preview = minecraft_setup.build_setup_preview(payload)
    except minecraft_setup.SetupValidationError as exc:
        return JSONResponse({
            "status": "error",
            "success": False,
            "error_code": "setup_preview_invalid",
            "error": "Invalid Minecraft setup preview payload.",
            "errors": exc.errors,
            "warnings": exc.warnings,
        }, status_code=400)

    return JSONResponse({
        "status": "ok",
        "preview": preview,
    })


@router.post("/api/minecraft/setup/create-server")
async def create_minecraft_setup_server(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Run the create-server preflight contract without writing server files."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({
            "status": "error",
            "success": False,
            "error_code": "setup_create_invalid",
            "error": "Request body must be valid JSON.",
        }, status_code=400)

    try:
        result = minecraft_setup.build_create_server_preflight(payload)
    except minecraft_setup.SetupCreationNotReady as exc:
        error = "Minecraft setup target is not ready for creation."
        if "eula_accepted" in exc.errors:
            error = "Minecraft setup policy is not ready for execution."
        return JSONResponse({
            "status": "error",
            "success": False,
            "error_code": "setup_create_not_ready",
            "error": error,
            "errors": exc.errors,
            "warnings": exc.warnings,
            "preflight": exc.preflight,
            "preview": exc.preview,
        }, status_code=409)
    except minecraft_setup.SetupValidationError as exc:
        return JSONResponse({
            "status": "error",
            "success": False,
            "error_code": "setup_create_invalid",
            "error": "Invalid Minecraft setup create-server payload.",
            "errors": exc.errors,
            "warnings": exc.warnings,
        }, status_code=400)

    return JSONResponse({
        "status": "ok",
        **result,
    })


@router.post("/api/minecraft/setup/create-server/execute")
async def execute_minecraft_setup_server(
    request: Request,
    user_info: dict = Depends(require_minecraft_owner_or_manager_admin),
):
    """Execute the guarded setup create flow without starting server processes."""
    if request.headers.get("x-cora-setup-intent") != "create-server":
        return JSONResponse({
            "status": "error",
            "success": False,
            "error_code": "setup_create_intent_required",
            "error": "Setup create execution requires an explicit intent header.",
        }, status_code=400)

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return JSONResponse({
            "status": "error",
            "success": False,
            "error_code": "setup_create_json_required",
            "error": "Setup create execution requires an application/json request body.",
        }, status_code=415)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({
            "status": "error",
            "success": False,
            "error_code": "setup_create_invalid",
            "error": "Request body must be valid JSON.",
        }, status_code=400)

    try:
        result = await minecraft_setup_executor.execute_create_server(
            payload,
            actor=user_info.get("email", "unknown"),
        )
    except minecraft_setup_executor.SetupExecutionError as exc:
        admin_audit_logger.info(
            "setup_create_execute_failed | admin=%s | error_code=%s",
            user_info.get("email", "unknown"),
            exc.error_code,
        )
        return JSONResponse(exc.to_response(), status_code=exc.status_code)

    profile = result.get("result", {}).get("profile", {})
    admin_audit_logger.info(
        "setup_create_execute_completed | admin=%s | profile=%s",
        user_info.get("email", "unknown"),
        profile.get("id", "unknown"),
    )
    return JSONResponse(result)


@router.get("/api/minecraft/paper-targets")
async def get_paper_upgrade_targets(user_info: dict = Depends(require_minecraft_admin)):
    """Get recent stable Paper targets for version upgrade preflight."""
    try:
        targets = await minecraft_updater.get_paper_upgrade_targets()
        return JSONResponse({
            "status": "ok",
            **targets,
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=500)


@router.post("/api/minecraft/upgrade-preflight")
async def run_upgrade_preflight(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Run a read-only Paper/Modrinth target compatibility preflight."""
    try:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse({
                "status": "error",
                "error": "Request body must be a JSON object",
            }, status_code=400)

        target_version = str(payload.get("target_version") or "").strip() or None
        result = await minecraft_updater.create_upgrade_manifest(target_version)
        return JSONResponse({
            "status": "ok",
            **result,
        })
    except ValueError as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=400)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=500)


@router.get("/api/minecraft/upgrade-manifests/{manifest_id}")
async def get_upgrade_manifest(manifest_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Get a persisted version upgrade manifest with current gate status."""
    try:
        manifest = minecraft_updater.load_upgrade_manifest(manifest_id)
        return JSONResponse({
            "status": "ok",
            **manifest,
        })
    except ValueError as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=404)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=500)


@router.post("/api/minecraft/upgrade-manifests/{manifest_id}/manual-review")
async def resolve_upgrade_manifest_manual_review(
    manifest_id: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_admin),
):
    """Mark a manual plugin in an upgrade manifest as reviewed/resolved."""
    try:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse({
                "status": "error",
                "error": "Request body must be a JSON object",
            }, status_code=400)

        plugin_id = str(payload.get("plugin_id") or "").strip()
        review_status = str(payload.get("status") or "").strip()
        note = str(payload.get("note") or "")
        if review_status != "resolved":
            return JSONResponse({
                "status": "error",
                "error": "Only resolved manual-review status is supported",
            }, status_code=400)

        reviewed_by = str(user_info.get("email") or "")
        manifest = minecraft_updater.resolve_upgrade_manifest_manual_review(
            manifest_id,
            plugin_id,
            note=note,
            reviewed_by=reviewed_by,
        )
        return JSONResponse({
            "status": "ok",
            **manifest,
        })
    except ValueError as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=400)
    except (FileNotFoundError, KeyError) as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=404)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=500)


@router.post("/api/minecraft/upgrade-manifests/{manifest_id}/execute")
async def execute_upgrade_manifest(
    manifest_id: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_admin),
):
    """Execute a ready Paper/Minecraft upgrade manifest."""
    from app.services.operations import execute_operation

    result = await execute_operation(
        key="server:upgrade",
        user_info=user_info,
        params={"manifest_id": manifest_id},
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(result)


@router.get("/api/minecraft/upgrade-executions/{execution_id}")
async def get_upgrade_execution(execution_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Get a persisted Paper/Minecraft upgrade execution record."""
    try:
        execution = minecraft_updater.load_upgrade_execution(execution_id)
        return JSONResponse({
            "status": "ok",
            "execution": execution,
        })
    except ValueError as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=404)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=500)


@router.get("/api/minecraft/logs")
async def get_update_logs(limit: int = 20, user_info: dict = Depends(require_minecraft_admin)):
    """Get recent update operation logs"""
    logs = minecraft_updater.get_update_logs(limit=limit)
    return JSONResponse({
        "status": "ok",
        "count": len(logs),
        "logs": logs
    })


@router.get("/api/minecraft/update-logs")
async def get_update_logs_api(limit: int = 10, user_info: dict = Depends(require_minecraft_admin)):
    """Get update logs via API for Alpine.js"""
    logs = minecraft_updater.get_update_logs(limit=limit)
    return JSONResponse({
        "status": "ok",
        "logs": logs
    })


@router.get("/api/minecraft/plugin-registry/candidates")
async def get_plugin_registry_candidates(
    include_hashes: bool = Query(True),
    user_info: dict = Depends(require_minecraft_admin),
):
    """List tracked plugins and untracked local plugin JARs."""
    try:
        return JSONResponse({
            "status": "ok",
            **minecraft_updater.scan_plugin_registry_candidates(include_hashes=include_hashes),
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=500)


@router.get("/api/minecraft/plugin-registry/modrinth-search")
async def search_modrinth_plugins(
    q: str = Query(..., min_length=1),
    user_info: dict = Depends(require_minecraft_admin),
):
    """Search Modrinth plugin projects for explicit operator matching."""
    try:
        versions_data = minecraft_updater.load_versions()
        results = await minecraft_updater.search_modrinth_plugin_projects(
            q,
            versions_data.get("minecraft_version", "1.21.11"),
            limit=10,
        )
        return JSONResponse({
            "status": "ok",
            "results": results,
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=500)


@router.get("/api/minecraft/plugin-registry/voxelshop-search")
async def search_voxelshop_plugins(
    q: str = Query(..., min_length=1),
    user_info: dict = Depends(require_minecraft_admin),
):
    """Search Voxel.shop resources for explicit operator matching."""
    try:
        versions_data = minecraft_updater.load_versions()
        results = await minecraft_updater.search_voxelshop_plugin_projects(
            q,
            versions_data.get("minecraft_version", "1.21.11"),
            limit=10,
        )
        return JSONResponse({
            "status": "ok",
            "results": results,
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
        }, status_code=500)


@router.get("/api/minecraft/plugin-registry/voxelshop-credentials")
async def get_voxelshop_credentials(user_info: dict = Depends(require_minecraft_admin)):
    """Return masked Voxel.shop token connection status."""
    return JSONResponse({
        "status": "ok",
        "credentials": minecraft_updater.get_voxelshop_credentials_status(),
    })


@router.get("/api/minecraft/plugin-registry/voxelshop-browser-download/config")
async def get_voxelshop_browser_download_config(user_info: dict = Depends(require_minecraft_admin)):
    """Return Voxel.shop browser-assisted download folder status."""
    return JSONResponse({
        "status": "ok",
        "config": minecraft_updater.get_voxelshop_browser_download_config(),
    })


@router.post("/api/minecraft/plugin-registry/voxelshop-browser-download/config")
async def save_voxelshop_browser_download_config(
    request: Request,
    user_info: dict = Depends(require_minecraft_owner_or_manager_admin),
):
    """Set the folder CORA watches for browser-downloaded Voxel.shop JARs."""
    try:
        body = await request.json()
        config = minecraft_updater.set_voxelshop_browser_download_directory(
            body.get("download_directory", ""),
            actor=user_info.get("email", ""),
        )
        return JSONResponse({"status": "ok", "config": config})
    except FileNotFoundError as e:
        return JSONResponse({"status": "folder_required", "error": str(e)}, status_code=400)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/api/minecraft/plugin-registry/voxelshop-browser-download/choose-folder")
async def choose_voxelshop_browser_download_folder(
    user_info: dict = Depends(require_minecraft_owner_or_manager_admin),
):
    """Open a native server-side folder picker for the Voxel.shop download folder."""
    try:
        config = await asyncio.to_thread(
            minecraft_updater.choose_voxelshop_browser_download_directory,
            actor=user_info.get("email", ""),
        )
        return JSONResponse({"status": "ok", "config": config})
    except FileNotFoundError as e:
        return JSONResponse({"status": "folder_required", "error": str(e)}, status_code=400)
    except ValueError as e:
        return JSONResponse({"status": "cancelled", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/api/minecraft/plugin-registry/voxelshop-browser-download/start/{plugin_id}")
async def start_voxelshop_browser_download(
    plugin_id: str,
    user_info: dict = Depends(require_minecraft_owner_or_manager_admin),
):
    """Create a browser-assisted Voxel.shop download session for one plugin."""
    try:
        result = await minecraft_updater.start_voxelshop_browser_download_session(
            plugin_id,
            actor=user_info.get("email", ""),
        )
        status_code = 409 if result.get("status") == "folder_required" else 200
        return JSONResponse(result, status_code=status_code)
    except KeyError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/api/minecraft/plugin-registry/voxelshop-browser-download/session/{session_id}")
async def get_voxelshop_browser_download_session(
    session_id: str,
    user_info: dict = Depends(require_minecraft_admin),
):
    """Poll a browser-assisted Voxel.shop download session for matching JARs."""
    try:
        return JSONResponse(minecraft_updater.get_voxelshop_browser_download_session(session_id))
    except KeyError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=404)
    except FileNotFoundError as e:
        return JSONResponse({"status": "folder_required", "error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/api/minecraft/plugin-registry/voxelshop-browser-download/session/{session_id}/apply")
async def apply_voxelshop_browser_download_session(
    session_id: str,
    user_info: dict = Depends(require_minecraft_owner_or_manager_admin),
):
    """Apply the matching browser-downloaded JAR for a Voxel.shop session."""
    try:
        log = await minecraft_updater.apply_voxelshop_browser_download_session(session_id)
        return JSONResponse({
            "status": log.status,
            "plugin_id": log.plugin,
            "from_version": log.from_version,
            "to_version": log.to_version,
            "steps": log.steps,
            "error": log.error,
        }, status_code=200 if log.status == "success" else 500)
    except KeyError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/api/minecraft/plugin-registry/voxelshop-auth/start")
async def start_voxelshop_authorization(
    request: Request,
    user_info: dict = Depends(require_minecraft_owner_or_manager_admin),
):
    """Create a Polymart/Voxel.shop account-link URL for paid downloads."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        public_base_url = body.get("public_base_url") or str(request.base_url).rstrip("/")
        authorization = await minecraft_updater.create_voxelshop_authorization_url(
            public_base_url=public_base_url,
            actor=user_info.get("email", ""),
        )
        return JSONResponse({"status": "ok", "authorization": authorization})
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.api_route("/api/minecraft/plugin-registry/voxelshop-auth/callback", methods=["GET", "POST"], response_class=HTMLResponse)
async def complete_voxelshop_authorization(request: Request):
    """Receive Polymart/Voxel.shop authorization callback and store the token."""
    values = dict(request.query_params)
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        raw_body = await request.body()
        if raw_body and "application/x-www-form-urlencoded" in content_type:
            posted = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
            values.update({key: items[-1] if items else "" for key, items in posted.items()})

    try:
        credentials = await minecraft_updater.complete_voxelshop_authorization(
            success=values.get("success", "1"),
            token=values.get("token", ""),
            state=values.get("state", ""),
        )
        connected = "true" if credentials.get("connected") else "false"
        title = "Voxel.shop connected"
        message = "Voxel.shop account connected. You can close this window."
        status = "ok"
    except Exception as e:
        connected = "false"
        title = "Voxel.shop connection failed"
        message = str(e)
        status = "error"

    safe_title = html.escape(title)
    safe_message = html.escape(message)

    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e2e8f0; }}
    main {{ width: min(520px, calc(100vw - 32px)); border: 1px solid rgba(148, 163, 184, .25); border-radius: 10px; padding: 24px; background: rgba(15, 23, 42, .92); }}
    h1 {{ margin: 0 0 8px; font-size: 20px; }}
    p {{ margin: 0; color: #94a3b8; line-height: 1.5; }}
    button {{ margin-top: 18px; border: 1px solid rgba(148, 163, 184, .35); border-radius: 8px; background: #1e293b; color: #e2e8f0; padding: 8px 12px; cursor: pointer; }}
  </style>
</head>
<body>
  <main>
    <h1>{safe_title}</h1>
    <p>{safe_message}</p>
    <button onclick="window.close()">Close</button>
  </main>
  <script>
    if (window.opener) {{
      window.opener.postMessage({{ type: 'cora-voxelshop-auth', status: '{status}', connected: {connected}, message: {json_module.dumps(message)} }}, window.location.origin);
      setTimeout(() => window.close(), 900);
    }}
  </script>
</body>
</html>""")


@router.post("/api/minecraft/plugin-registry/voxelshop-credentials")
async def connect_voxelshop_credentials(
    request: Request,
    user_info: dict = Depends(require_minecraft_owner_or_manager_admin),
):
    """Advanced/manual token entry for tokens returned by Voxel.shop authorization."""
    try:
        body = await request.json()
        credentials = await minecraft_updater.connect_voxelshop_token(
            body.get("token", ""),
            actor=user_info.get("email", ""),
        )
        return JSONResponse({"status": "ok", "credentials": credentials})
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.delete("/api/minecraft/plugin-registry/voxelshop-credentials")
async def disconnect_voxelshop_credentials(
    user_info: dict = Depends(require_minecraft_owner_or_manager_admin),
):
    """Remove the locally stored Voxel.shop token."""
    try:
        credentials = await minecraft_updater.disconnect_voxelshop_token()
        return JSONResponse({"status": "ok", "credentials": credentials})
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/api/minecraft/plugin-registry/tracking/{plugin_id}/connection")
async def get_plugin_tracking_connection(
    plugin_id: str,
    user_info: dict = Depends(require_minecraft_admin),
):
    """Return provider connection and latest-update status for a tracked plugin."""
    try:
        connection = await minecraft_updater.get_plugin_connection_status(plugin_id)
        return JSONResponse({"status": "ok", "connection": connection})
    except KeyError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/api/minecraft/plugin-registry/track-local")
async def track_local_plugin_jar(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Track an existing untracked local plugin JAR without replacing files."""
    try:
        body = await request.json()
        result = minecraft_updater.add_tracked_plugin_from_local_jar(
            filename=body.get("filename", ""),
            source=body.get("source", "manual"),
            plugin_id=body.get("plugin_id"),
            project_id=body.get("project_id"),
            loader=body.get("loader", "paper"),
            actor=user_info.get("email", ""),
        )
        return JSONResponse({"status": "ok", **result})
    except FileNotFoundError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/api/minecraft/plugin-registry/install-modrinth")
async def install_modrinth_plugin(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Install a selected Modrinth plugin and add it to tracking."""
    try:
        body = await request.json()
        result = await minecraft_updater.install_modrinth_plugin_project(
            project_id=body.get("project_id", ""),
            plugin_id=body.get("plugin_id"),
            loader=body.get("loader", "paper"),
            actor=user_info.get("email", ""),
        )
        return JSONResponse({"status": "ok", **result})
    except FileExistsError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.patch("/api/minecraft/plugin-registry/tracking/{plugin_id}")
async def update_plugin_tracking(
    plugin_id: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_admin),
):
    """Update editable tracking metadata for an already tracked plugin."""
    try:
        body = await request.json()
        result = minecraft_updater.update_tracked_plugin_settings(
            plugin_id,
            source=body.get("source"),
            project_id=body.get("project_id"),
            loader=body.get("loader"),
            auto_update=body.get("auto_update"),
            channel=body.get("channel"),
            actor=user_info.get("email", ""),
        )
        return JSONResponse({"status": "ok", **result})
    except KeyError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/api/minecraft/changelog/{plugin_id}")
async def get_plugin_changelog(plugin_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Get changelog for a specific plugin's latest version"""
    try:
        versions_data = minecraft_updater.load_versions()
        plugin_config = versions_data.get("plugins", {}).get(plugin_id)

        if not plugin_config:
            raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")

        minecraft_version = versions_data.get("minecraft_version", "1.21.1")

        # Get latest version info
        source = plugin_config.get("source")
        project_id = plugin_config.get("project_id", plugin_id)

        if source == "papermc":
            version_info = await minecraft_updater.get_papermc_latest(minecraft_version)
        elif source == "modrinth":
            version_info = await minecraft_updater.get_modrinth_latest(project_id, minecraft_version)
        elif source == "voxelshop":
            version_info = await minecraft_updater.get_voxelshop_latest(project_id, minecraft_version)
        elif source == "essentialsx":
            version_info = await minecraft_updater.get_essentialsx_latest(
                minecraft_version,
                channel=plugin_config.get("channel", "auto"),
                module=minecraft_updater._essentialsx_module(plugin_id, plugin_config),
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

        return JSONResponse({
            "status": "ok",
            "plugin_id": plugin_id,
            "version": version_info.version,
            "changelog": version_info.changelog,
            "game_versions": version_info.game_versions
        })

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e)
        }, status_code=500)


@router.post("/api/minecraft/server/enable-rcon")
async def enable_rcon_endpoint(user_info: dict = Depends(require_minecraft_admin)):
    """RCON password generation is disabled in the public extraction."""
    from app.services.audit_log import audit_event

    audit_event(
        logger=admin_audit_logger,
        actor=user_info.get("email", "unknown"),
        action="enable_rcon_disabled",
        target="public_extract",
        result="denied",
    )
    return JSONResponse(
        {
            "success": False,
            "error": "RCON password generation is disabled in this public extraction.",
        },
        status_code=403,
    )
