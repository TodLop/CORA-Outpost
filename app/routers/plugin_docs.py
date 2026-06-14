# app/routers/plugin_docs.py
"""
Plugin Documentation Router

Provides API endpoints and page routes for viewing and managing
plugin documentation for both admin and staff users.
"""

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.core.config import TEMPLATES_DIR
from app.core.auth import require_auth, is_staff
from app.core.minecraft_access import is_minecraft_admin_user, require_minecraft_admin
from app.services import plugin_docs, plugin_notifications, permissions as permissions_service
from app.services.minecraft_updater import load_versions
from app.services.modrinth_api import batch_get_icons, get_plugin_icon

router = APIRouter(prefix="/minecraft/plugins", tags=["PluginDocs"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def require_plugins_view_access(request: Request) -> dict:
    """
    Plugin docs view access:
    - Minecraft admins: always allowed
    - Staff: requires plugins:view permission
    """
    user_info = await require_auth(request)
    if is_minecraft_admin_user(user_info):
        return user_info

    if not is_staff(user_info):
        raise HTTPException(status_code=403, detail="Staff access required")

    email = user_info.get("email", "")
    if not permissions_service.has_permission(email, "plugins:view"):
        raise HTTPException(status_code=403, detail="Permission denied: plugins:view")
    return user_info


# ==================== Page Routes ====================

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def plugins_overview(request: Request, user_info=Depends(require_plugins_view_access)):
    """Plugin documentation overview page"""
    # Get plugin version info
    versions_data = load_versions()
    tracked_plugins = versions_data.get("plugins", {})

    # Get documentation for all plugins
    docs = plugin_docs.get_all_plugins()

    # Get unread notification count
    unread_count = plugin_notifications.get_unread_count(user_info.get("email", ""))

    # Merge version info with docs
    plugins_list = []
    modrinth_ids = []
    for plugin_id, version_info in tracked_plugins.items():
        doc = docs.get(plugin_id, {})
        plugin_data = {
            "id": plugin_id,
            "name": plugin_id.title(),
            "version": version_info.get("full_version") or version_info.get("current_version", "Unknown"),
            "source": version_info.get("source", "unknown"),
            "summary": doc.get("summary", ""),
            "has_docs": bool(doc.get("summary") or doc.get("description")),
            "commands_count": len(doc.get("commands", [])),
            "comments_count": len(doc.get("comments", []))
        }
        plugins_list.append(plugin_data)

        # Collect Modrinth project IDs for icon fetching
        if version_info.get("source") == "modrinth" and version_info.get("project_id"):
            modrinth_ids.append(version_info["project_id"])

    # Fetch Modrinth icons
    icons_map = {}
    if modrinth_ids:
        icons_map = await batch_get_icons(modrinth_ids)

    # Add icon URLs to plugins
    for plugin in plugins_list:
        version_info = tracked_plugins.get(plugin["id"], {})
        if version_info.get("source") == "modrinth":
            project_id = version_info.get("project_id")
            plugin["icon_url"] = icons_map.get(project_id)
        else:
            plugin["icon_url"] = None

    # Sort by name
    plugins_list.sort(key=lambda x: x["name"].lower())

    user_is_minecraft_admin = is_minecraft_admin_user(user_info)
    return templates.TemplateResponse("plugins/index.html", {
        "request": request,
        "user_info": user_info,
        "is_admin": user_is_minecraft_admin,
        "is_minecraft_admin": user_is_minecraft_admin,
        "plugins": plugins_list,
        "unread_count": unread_count,
    })


@router.get("/{plugin_id}", response_class=HTMLResponse)
async def plugin_detail(request: Request, plugin_id: str, user_info=Depends(require_plugins_view_access)):
    """Plugin detail page with tabs"""
    # Get version info
    versions_data = load_versions()
    tracked_plugins = versions_data.get("plugins", {})

    if plugin_id not in tracked_plugins:
        raise HTTPException(status_code=404, detail="Plugin not found")

    version_info = tracked_plugins[plugin_id]

    # Get documentation
    doc = plugin_docs.get_plugin(plugin_id) or {
        "summary": "",
        "description": "",
        "commands": [],
        "key_settings": [],
        "comments": []
    }

    # Get config files list
    config_files = plugin_docs.list_config_files(plugin_id)

    # Get Modrinth icon if available
    icon_url = None
    if version_info.get("source") == "modrinth":
        project_id = version_info.get("project_id")
        if project_id:
            icon_url = await get_plugin_icon(project_id)

    # Mark notifications for this plugin as read
    plugin_notifications.mark_plugin_notifications_read(user_info.get("email", ""), plugin_id)

    # Get unread notification count (after marking)
    unread_count = plugin_notifications.get_unread_count(user_info.get("email", ""))

    user_is_minecraft_admin = is_minecraft_admin_user(user_info)
    return templates.TemplateResponse("plugins/detail.html", {
        "request": request,
        "user_info": user_info,
        "is_admin": user_is_minecraft_admin,
        "is_minecraft_admin": user_is_minecraft_admin,
        "plugin_id": plugin_id,
        "plugin_name": plugin_id.title(),
        "version": version_info.get("full_version") or version_info.get("current_version", "Unknown"),
        "source": version_info.get("source", "unknown"),
        "doc": doc,
        "config_files": config_files,
        "unread_count": unread_count,
        "icon_url": icon_url
    })


# ==================== API Endpoints: Documentation ====================

@router.get("/api/docs")
async def get_all_docs(user_info: dict = Depends(require_plugins_view_access)):
    """Get all plugin documentation"""
    docs = plugin_docs.get_all_plugins()
    return JSONResponse({"status": "ok", "plugins": docs})


@router.get("/api/docs/{plugin_id}")
async def get_plugin_doc(plugin_id: str, user_info: dict = Depends(require_plugins_view_access)):
    """Get documentation for a specific plugin"""
    doc = plugin_docs.get_plugin(plugin_id)
    if not doc:
        return JSONResponse({"status": "ok", "doc": None})
    return JSONResponse({"status": "ok", "doc": doc})


@router.put("/api/docs/{plugin_id}")
async def update_plugin_doc(request: Request, plugin_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Update plugin summary and description (Admin only)"""
    body = await request.json()

    summary = body.get("summary")
    description = body.get("description")

    if summary is None and description is None:
        return JSONResponse(
            {"status": "error", "error": "No update data provided"},
            status_code=400
        )

    doc = plugin_docs.update_plugin_doc(
        plugin_id=plugin_id,
        summary=summary,
        description=description,
        updated_by=user_info.get("email", ""),
        updated_by_name=user_info.get("name", "Admin")
    )

    # Create notification
    plugin_notifications.create_notification(
        notification_type="doc_update",
        plugin_id=plugin_id,
        plugin_name=plugin_id.title(),
        actor=user_info.get("email", ""),
        actor_name=user_info.get("name", "Admin"),
        message=f"Updated documentation for {plugin_id.title()}"
    )

    return JSONResponse({"status": "ok", "doc": doc})


# ==================== API Endpoints: Commands ====================

@router.post("/api/{plugin_id}/commands")
async def add_command(request: Request, plugin_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Add a command to plugin documentation (Admin only)"""
    body = await request.json()

    command = body.get("command", "").strip()
    description = body.get("description", "").strip()

    if not command:
        return JSONResponse(
            {"status": "error", "error": "Command is required"},
            status_code=400
        )

    cmd = plugin_docs.add_command(
        plugin_id=plugin_id,
        command=command,
        description=description,
        permission=body.get("permission", ""),
        usage=body.get("usage", ""),
        added_by=user_info.get("email", "")
    )

    # Create notification
    plugin_notifications.create_notification(
        notification_type="command_added",
        plugin_id=plugin_id,
        plugin_name=plugin_id.title(),
        actor=user_info.get("email", ""),
        actor_name=user_info.get("name", "Admin"),
        message=f"Added command {command} to {plugin_id.title()}"
    )

    return JSONResponse({"status": "ok", "command": cmd})


@router.put("/api/{plugin_id}/commands/{command_id}")
async def update_command(
    request: Request,
    plugin_id: str,
    command_id: str,
    user_info: dict = Depends(require_minecraft_admin)
):
    """Update a command (Admin only)"""
    body = await request.json()

    cmd = plugin_docs.update_command(
        plugin_id=plugin_id,
        command_id=command_id,
        command=body.get("command"),
        description=body.get("description"),
        permission=body.get("permission"),
        usage=body.get("usage")
    )

    if not cmd:
        return JSONResponse(
            {"status": "error", "error": "Command not found"},
            status_code=404
        )

    return JSONResponse({"status": "ok", "command": cmd})


@router.delete("/api/{plugin_id}/commands/{command_id}")
async def delete_command(plugin_id: str, command_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Delete a command (Admin only)"""
    success = plugin_docs.delete_command(plugin_id, command_id)

    if not success:
        return JSONResponse(
            {"status": "error", "error": "Command not found"},
            status_code=404
        )

    return JSONResponse({"status": "ok"})


# ==================== API Endpoints: Key Settings ====================

@router.post("/api/{plugin_id}/settings")
async def add_key_setting(request: Request, plugin_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Add a key setting highlight (Admin only)"""
    body = await request.json()

    path = body.get("path", "").strip()
    description = body.get("description", "").strip()

    if not path:
        return JSONResponse(
            {"status": "error", "error": "Setting path is required"},
            status_code=400
        )

    setting = plugin_docs.add_key_setting(
        plugin_id=plugin_id,
        path=path,
        description=description,
        current_value=body.get("current_value", ""),
        added_by=user_info.get("email", "")
    )

    # Create notification
    plugin_notifications.create_notification(
        notification_type="setting_added",
        plugin_id=plugin_id,
        plugin_name=plugin_id.title(),
        actor=user_info.get("email", ""),
        actor_name=user_info.get("name", "Admin"),
        message=f"Added key setting {path} to {plugin_id.title()}"
    )

    return JSONResponse({"status": "ok", "setting": setting})


@router.delete("/api/{plugin_id}/settings/{setting_id}")
async def delete_key_setting(plugin_id: str, setting_id: str, user_info: dict = Depends(require_minecraft_admin)):
    """Delete a key setting (Admin only)"""
    success = plugin_docs.delete_key_setting(plugin_id, setting_id)

    if not success:
        return JSONResponse(
            {"status": "error", "error": "Setting not found"},
            status_code=404
        )

    return JSONResponse({"status": "ok"})


# ==================== API Endpoints: Comments ====================

@router.post("/api/{plugin_id}/comments")
async def add_comment(request: Request, plugin_id: str, user_info: dict = Depends(require_plugins_view_access)):
    """Add a comment (Staff + Admin)"""
    body = await request.json()

    text = body.get("text", "").strip()

    if not text:
        return JSONResponse(
            {"status": "error", "error": "Comment text is required"},
            status_code=400
        )

    if len(text) > 2000:
        return JSONResponse(
            {"status": "error", "error": "Comment too long (max 2000 chars)"},
            status_code=400
        )

    comment = plugin_docs.add_comment(
        plugin_id=plugin_id,
        author=user_info.get("email", ""),
        author_name=user_info.get("name", "User"),
        text=text
    )

    # Create notification
    plugin_notifications.create_notification(
        notification_type="comment_added",
        plugin_id=plugin_id,
        plugin_name=plugin_id.title(),
        actor=user_info.get("email", ""),
        actor_name=user_info.get("name", "User"),
        message=f"New comment on {plugin_id.title()}"
    )

    return JSONResponse({"status": "ok", "comment": comment})


@router.delete("/api/{plugin_id}/comments/{comment_id}")
async def delete_comment(plugin_id: str, comment_id: str, user_info: dict = Depends(require_plugins_view_access)):
    """Delete a comment (Admin can delete any, Staff can delete own)"""
    user_is_minecraft_admin = is_minecraft_admin_user(user_info)
    success = plugin_docs.delete_comment(
        plugin_id=plugin_id,
        comment_id=comment_id,
        user_email=user_info.get("email", ""),
        is_admin=user_is_minecraft_admin,
    )

    if not success:
        return JSONResponse(
            {"status": "error", "error": "Comment not found or not authorized"},
            status_code=404
        )

    return JSONResponse({"status": "ok"})


# ==================== API Endpoints: Config Files ====================

@router.get("/api/{plugin_id}/config")
async def get_config_file(
    plugin_id: str,
    filename: str = "config.yml",
    user_info: dict = Depends(require_plugins_view_access),
):
    """Read a config file for a plugin (read-only)"""
    result = plugin_docs.read_config_file(plugin_id, filename)

    if not result:
        return JSONResponse(
            {"status": "error", "error": "Config file not found"},
            status_code=404
        )

    if "error" in result:
        return JSONResponse(
            {"status": "error", **result},
            status_code=400
        )

    return JSONResponse({"status": "ok", **result})


@router.get("/api/{plugin_id}/config/files")
async def list_config_files(plugin_id: str, user_info: dict = Depends(require_plugins_view_access)):
    """List available config files for a plugin"""
    files = plugin_docs.list_config_files(plugin_id)
    return JSONResponse({"status": "ok", "files": files})


# ==================== API Endpoints: Notifications ====================

@router.get("/api/notifications")
async def get_notifications(
    limit: int = 50,
    unread_only: bool = False,
    user_info: dict = Depends(require_plugins_view_access),
):
    """Get notifications for the current user"""
    notifications = plugin_notifications.get_notifications(
        user_email=user_info.get("email", ""),
        limit=limit,
        unread_only=unread_only
    )
    return JSONResponse({"status": "ok", "notifications": notifications})


@router.get("/api/notifications/unread")
async def get_unread_count(user_info: dict = Depends(require_plugins_view_access)):
    """Get unread notification count"""
    count = plugin_notifications.get_unread_count(user_info.get("email", ""))
    return JSONResponse({"status": "ok", "count": count})


@router.post("/api/notifications/mark-read")
async def mark_notifications_read(request: Request, user_info: dict = Depends(require_plugins_view_access)):
    """Mark notifications as read"""
    body = await request.json()
    notification_ids = body.get("ids")  # None means mark all

    count = plugin_notifications.mark_as_read(
        user_email=user_info.get("email", ""),
        notification_ids=notification_ids
    )

    return JSONResponse({"status": "ok", "marked": count})


# ==================== Initialization Endpoint ====================

@router.post("/api/initialize")
async def initialize_docs(user_info: dict = Depends(require_minecraft_admin)):
    """Initialize plugin documentation with default data (Admin only)"""
    count = plugin_docs.initialize_plugin_docs()
    return JSONResponse({"status": "ok", "plugins_initialized": count})
