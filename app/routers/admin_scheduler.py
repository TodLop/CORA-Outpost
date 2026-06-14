# app/routers/admin_scheduler.py
"""
Admin Scheduler Routes (Reboot, CoreProtect, Backup)

Extracted from admin.py — scheduler-related endpoints.
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse

from app.core.minecraft_access import require_minecraft_admin
from app.services.reboot_scheduler import get_scheduler

router = APIRouter()


# =============================================================================
# Reboot Automation Endpoints
# =============================================================================


@router.get("/api/minecraft/reboot-scheduler/status")
async def get_reboot_scheduler_status(user_info: dict = Depends(require_minecraft_admin)):
    """Get current reboot scheduler status"""
    scheduler = get_scheduler()
    return JSONResponse({
        "status": "ok",
        "config": scheduler.get_config(),
        "scheduler_status": scheduler.get_status()
    })


@router.get("/api/minecraft/reboot-scheduler/logs")
async def get_reboot_scheduler_logs(limit: int = 50, user_info: dict = Depends(require_minecraft_admin)):
    """Get reboot scheduler action logs"""
    scheduler = get_scheduler()
    return JSONResponse({
        "status": "ok",
        "logs": scheduler.get_logs(limit=limit)
    })


@router.post("/api/minecraft/reboot-scheduler/config")
async def update_reboot_scheduler_config(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Update reboot scheduler configuration"""
    body = await request.json()
    scheduler = get_scheduler()

    # Validate and update config
    result = scheduler.update_config(**body)
    return JSONResponse(result)


@router.post("/api/minecraft/reboot-scheduler/trigger")
async def trigger_manual_restart(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Manually trigger a server restart with countdown"""
    body = await request.json()
    reason = body.get("reason", "manual")

    scheduler = get_scheduler()
    result = await scheduler.trigger_manual_restart(reason)

    return JSONResponse(result)


@router.post("/api/minecraft/reboot-scheduler/cancel")
async def cancel_restart_countdown(user_info: dict = Depends(require_minecraft_admin)):
    """Cancel an active restart countdown"""
    scheduler = get_scheduler()
    result = scheduler.cancel_countdown()
    return JSONResponse(result)


@router.post("/api/minecraft/reboot-scheduler/maintenance-stop/schedule")
async def schedule_maintenance_stop(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Schedule one-time maintenance stop at a specific datetime."""
    body = await request.json()
    scheduled_at = body.get("scheduled_at")

    scheduler = get_scheduler()
    result = scheduler.schedule_maintenance_stop(scheduled_at)
    status_code = 200 if result.get("success") else 400
    return JSONResponse(result, status_code=status_code)


@router.post("/api/minecraft/reboot-scheduler/maintenance-stop/cancel")
async def cancel_maintenance_stop(user_info: dict = Depends(require_minecraft_admin)):
    """Cancel one-time scheduled maintenance stop."""
    scheduler = get_scheduler()
    result = scheduler.cancel_maintenance_stop()
    status_code = 200 if result.get("success") else 400
    return JSONResponse(result, status_code=status_code)


# =============================================================================
# CoreProtect Maintenance Endpoints
# =============================================================================

@router.get("/api/minecraft/coreprotect/status")
async def get_coreprotect_status(user_info: dict = Depends(require_minecraft_admin)):
    """Get CoreProtect purge status"""
    scheduler = get_scheduler()
    return JSONResponse({
        "status": "ok",
        **scheduler.get_coreprotect_status()
    })


@router.post("/api/minecraft/coreprotect/config")
async def update_coreprotect_config(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Update CoreProtect purge configuration"""
    body = await request.json()
    scheduler = get_scheduler()

    # Only allow CoreProtect-related config updates
    allowed_keys = [
        "coreprotect_purge_enabled",
        "coreprotect_retention_days",
        "coreprotect_purge_hour"
    ]
    filtered = {k: v for k, v in body.items() if k in allowed_keys}

    if not filtered:
        return JSONResponse({"success": False, "error": "No valid config keys provided"}, status_code=400)

    result = scheduler.update_config(**filtered)
    return JSONResponse({
        **result,
        "coreprotect": scheduler.get_coreprotect_status()
    })


@router.post("/api/minecraft/coreprotect/purge")
async def trigger_coreprotect_purge(user_info: dict = Depends(require_minecraft_admin)):
    """Manually trigger CoreProtect log purge"""
    scheduler = get_scheduler()
    result = await scheduler.execute_coreprotect_purge(manual=True)
    return JSONResponse(result)


# =============================================================================
# Backup Scheduler Endpoints
# =============================================================================

@router.get("/api/minecraft/backup-scheduler/status")
async def get_backup_scheduler_status(user_info: dict = Depends(require_minecraft_admin)):
    """Get current backup scheduler status and config"""
    from app.services.backup_scheduler import get_backup_scheduler
    scheduler = get_backup_scheduler()
    return JSONResponse({
        "status": "ok",
        "config": scheduler.get_config(),
        "backup_status": scheduler.get_status(),
        "setup_status": scheduler.get_setup_status()
    })


@router.get("/api/minecraft/backup-scheduler/logs")
async def get_backup_scheduler_logs(limit: int = 50, user_info: dict = Depends(require_minecraft_admin)):
    """Get backup scheduler action logs"""
    from app.services.backup_scheduler import get_backup_scheduler
    scheduler = get_backup_scheduler()
    return JSONResponse({
        "status": "ok",
        "logs": scheduler.get_logs(limit=limit)
    })


@router.post("/api/minecraft/backup-scheduler/config")
async def update_backup_scheduler_config(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Update backup scheduler configuration"""
    body = await request.json()
    from app.services.backup_scheduler import get_backup_scheduler
    scheduler = get_backup_scheduler()

    allowed_keys = [
        "enabled", "backup_interval_days", "backup_hour", "backup_minute",
        "countdown_minutes", "drive_folder_id", "keep_drive_backups",
    ]
    filtered = {k: v for k, v in body.items() if k in allowed_keys}
    if not filtered:
        return JSONResponse({"success": False, "error": "No valid config keys provided"}, status_code=400)

    result = scheduler.update_config(**filtered)
    return JSONResponse(result)


@router.post("/api/minecraft/backup-scheduler/trigger")
async def trigger_manual_backup(user_info: dict = Depends(require_minecraft_admin)):
    """Manually trigger a server backup"""
    from app.services.backup_scheduler import get_backup_scheduler
    scheduler = get_backup_scheduler()
    result = await scheduler.trigger_manual_backup()
    return JSONResponse(result)


@router.post("/api/minecraft/backup-scheduler/cancel")
async def cancel_backup_countdown(user_info: dict = Depends(require_minecraft_admin)):
    """Cancel an active backup countdown"""
    from app.services.backup_scheduler import get_backup_scheduler
    scheduler = get_backup_scheduler()
    result = scheduler.cancel_countdown()
    return JSONResponse(result)


@router.post("/api/minecraft/backup-scheduler/test-connection")
async def test_backup_drive_connection(user_info: dict = Depends(require_minecraft_admin)):
    """Test Google Drive connectivity for backup service account"""
    from app.services.backup_scheduler import get_backup_scheduler
    scheduler = get_backup_scheduler()
    result = scheduler.test_drive_connection()
    return JSONResponse(result)
