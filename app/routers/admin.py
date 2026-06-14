# app/routers/admin.py
"""
Admin Panel Routes — Aggregator

Access restricted to Minecraft admin users (owner/manager_admin/global admin).
Routes are split by concern into sub-routers; this file aggregates them
and keeps cross-cutting endpoints (overview, dashboard).
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.core.config import TEMPLATES_DIR, APP_VERSION
from app.core.minecraft_access import require_minecraft_admin
from app.services import minecraft_updater
from app.services import minecraft_server
from app.services import minecraft_settings
from app.services import user_preferences as user_preferences_service
from app.services import permissions as permissions_service
from app.services.modrinth_api import batch_get_icons
from app.services import minecraft_admin_tiers

# Sub-routers (each has router = APIRouter() with no prefix)
from app.routers.admin_server import router as server_router
from app.routers.admin_scheduler import router as scheduler_router
from app.routers.admin_moderation import router as moderation_router
from app.routers.admin_rbac import router as rbac_router
from app.routers.admin_analytics import router as analytics_router

router = APIRouter(prefix="/minecraft/admin", tags=["Admin"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Include sub-routers (they inherit our prefix and tags)
router.include_router(server_router)
router.include_router(scheduler_router)
router.include_router(moderation_router)
router.include_router(rbac_router)
router.include_router(analytics_router)


# =============================================================================
# Cross-cutting endpoints (aggregate data from multiple concerns)
# =============================================================================

@router.get("/api/overview")
async def get_admin_overview(user_info: dict = Depends(require_minecraft_admin)):
    """Get aggregated overview data for admin dashboard"""
    from app.services.reboot_scheduler import get_scheduler

    # Get server status
    server_status = minecraft_server.get_server_status()

    # Get versions data for last check time
    versions_data = minecraft_updater.load_versions()

    # Get scheduler status for uptime
    scheduler = get_scheduler()
    scheduler_status = scheduler.get_status()

    # Count active services
    active_services = 1  # Web app is always running
    if server_status.running:
        active_services += 1  # Minecraft server

    # Format uptime
    uptime_str = scheduler_status.get("uptime_formatted", "--")
    if not server_status.running:
        uptime_str = "Offline"

    return JSONResponse({
        "status": "ok",
        "active_services": active_services,
        "pending_updates": versions_data.get("pending_updates", 0),
        "uptime": uptime_str,
        "last_check": versions_data.get("last_check"),
        "server_running": server_status.running,
        "players_online": server_status.players_online or 0,
        "app_version": APP_VERSION
    })


@router.get("/api/preferences")
async def admin_get_preferences(user_info: dict = Depends(require_minecraft_admin)):
    """Get current admin user's UI preferences."""
    email = user_info.get("email", "")
    return JSONResponse({
        "status": "ok",
        "preferences": user_preferences_service.get_preferences(email),
    })


@router.put("/api/preferences")
async def admin_update_preferences(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Update current admin user's UI preferences."""
    email = user_info.get("email", "")
    body = await request.json()
    patch = body.get("preferences", body) if isinstance(body, dict) else {}
    if not isinstance(patch, dict):
        return JSONResponse(
            {"status": "error", "error": "preferences must be an object"},
            status_code=400,
        )

    try:
        updated = user_preferences_service.set_preferences(
            email=email,
            patch=patch,
            updated_by="self",
        )
        return JSONResponse({"status": "ok", "preferences": updated})
    except user_preferences_service.PreferenceValidationError as exc:
        return JSONResponse(
            {"status": "error", "error": "invalid preferences", "fields": exc.errors},
            status_code=400,
        )
    except Exception:
        return JSONResponse(
            {"status": "error", "error": "failed to persist preferences"},
            status_code=500,
        )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def minecraft_dashboard(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Minecraft server management dashboard"""
    # Get current version data
    versions_data = minecraft_updater.load_versions()
    file_status = minecraft_updater.get_server_status()  # File-based status
    server_status = minecraft_server.get_server_status()  # Process-based status
    update_logs = minecraft_updater.get_update_logs(limit=10)
    server_icon_path = minecraft_settings.get_server_icon_path()
    server_icon_url = None
    if server_icon_path is not None:
        server_icon_url = f"/minecraft/admin/api/minecraft/server-icon.png?v={int(server_icon_path.stat().st_mtime)}"

    # Get Modrinth icons for plugins
    tracked_plugins = versions_data.get("plugins", {})
    modrinth_ids = []
    for plugin_id, plugin_config in tracked_plugins.items():
        if plugin_config.get("source") == "modrinth" and plugin_config.get("project_id"):
            modrinth_ids.append(plugin_config["project_id"])

    plugin_icons = {}
    if modrinth_ids:
        plugin_icons = await batch_get_icons(modrinth_ids)

    user_email = (user_info or {}).get("email", "")
    subject_type = minecraft_admin_tiers.get_subject_type(user_email)
    can_manage_staff_rbac = subject_type in {"owner", "manager_admin"}
    is_owner = minecraft_admin_tiers.is_minecraft_owner(user_email)
    can_whitelist_audit = is_owner or (
        subject_type == "manager_admin"
        and permissions_service.has_permission(user_email, "whitelist:audit")
    )
    can_whitelist_prune = is_owner or (
        subject_type == "manager_admin"
        and permissions_service.has_permission(user_email, "whitelist:prune")
    )
    can_view_map = False

    return templates.TemplateResponse("admin/minecraft.html", {
        "request": request,
        "user_info": user_info,
        "is_admin": True,
        "is_minecraft_owner": is_owner,
        "is_minecraft_manager_admin": minecraft_admin_tiers.is_minecraft_manager_admin(user_email),
        "can_manage_staff_rbac": can_manage_staff_rbac,
        "can_whitelist_audit": can_whitelist_audit,
        "can_whitelist_prune": can_whitelist_prune,
        "can_view_map": can_view_map,
        "versions_data": versions_data,
        "server_status": {
            **file_status,
            "running": server_status.running,
            "process_running": server_status.process_running,
            "healthy": server_status.healthy,
            "state_reason": server_status.state_reason,
            "pid": server_status.pid,
            "game_port_listening": server_status.game_port_listening,
            "rcon_port_listening": server_status.rcon_port_listening,
            "players_online": server_status.players_online,
            "max_players": server_status.max_players
        },
        "update_logs": update_logs,
        "plugin_icons": plugin_icons,
        "server_icon_url": server_icon_url,
    })
