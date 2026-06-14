# app/routers/admin_rbac.py
"""
RBAC and staff governance endpoints for Minecraft module.

Owner-only scope:
- Manager-admin promotion/demotion history
- Owner audit log reads

Manager-admin + owner scope:
- Staff RBAC management (staff subjects only)
"""

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import JSONResponse

from app.core.minecraft_access import require_minecraft_owner, require_minecraft_rbac_manager
from app.core.config import STAFF_EMAILS
from app.services import permissions as permissions_service
from app.services import minecraft_admin_tiers as admin_tiers

router = APIRouter()
_MANAGER_ADMIN_CAPABILITY_PERMISSIONS = frozenset(admin_tiers.MANAGER_ADMIN_CAPABILITY_PERMISSIONS)


def _subject_type(email: str) -> str:
    return admin_tiers.get_subject_type(email)


def _is_staff_subject(email: str) -> bool:
    return _subject_type(email) == "staff"


def _staff_target_blocked_response() -> JSONResponse:
    return JSONResponse(
        {
            "success": False,
            "error": "Target must be a staff account (owner/manager_admin cannot be modified)",
        },
        status_code=403,
    )


def _is_active_manager_admin_subject(email: str) -> bool:
    return admin_tiers.is_minecraft_manager_admin(email)


def _manager_admin_target_blocked_response() -> JSONResponse:
    return JSONResponse(
        {
            "success": False,
            "error": "Target must be an active tracked manager_admin account",
        },
        status_code=400,
    )


# =============================================================================
# RBAC Permission Management (Manager Admin + Owner)
# =============================================================================


@router.get("/api/rbac/roles")
async def get_rbac_roles(user_info: dict = Depends(require_minecraft_rbac_manager)):
    """List all role presets with descriptions and permissions."""
    roles = {}
    for role_name, role_data in permissions_service.ROLE_PRESETS.items():
        roles[role_name] = {
            "description": role_data["description"],
            "permissions": sorted(role_data["permissions"]),
        }
    return JSONResponse({"status": "ok", "roles": roles})


@router.get("/api/rbac/permissions")
async def get_rbac_permissions(user_info: dict = Depends(require_minecraft_rbac_manager)):
    """Get all permissions with metadata (description, module)."""
    permissions = {}
    for perm in sorted(permissions_service.ALL_PERMISSIONS):
        meta = permissions_service.PERMISSION_METADATA.get(perm, {})
        permissions[perm] = {
            "module": meta.get("module", "unknown"),
            "description": meta.get("description", perm),
        }
    return JSONResponse({"status": "ok", "permissions": permissions})


@router.get("/api/rbac/users")
async def get_rbac_users(user_info: dict = Depends(require_minecraft_rbac_manager)):
    """Get staff-only RBAC users (manager-admin/owner only)."""
    actor_email = user_info.get("email", "unknown")
    admin_tiers.reconcile_admin_tiers(actor=actor_email)

    rbac_users = permissions_service.get_all_users()
    rbac_emails = {u.email for u in rbac_users}

    users = []
    for user in rbac_users:
        subject_type = _subject_type(user.email)
        if subject_type != "staff":
            continue
        effective = sorted(permissions_service.get_effective_permissions(user.email))
        users.append({
            "email": user.email,
            "role": user.role,
            "grants": user.grants,
            "revokes": user.revokes,
            "effective_permissions": effective,
            "visible_modules": permissions_service.get_user_visible_modules(user.email),
            "updated_at": user.updated_at,
            "updated_by": user.updated_by,
            "subject_type": subject_type,
        })

    for email in STAFF_EMAILS:
        email_l = email.lower()
        if _subject_type(email_l) != "staff":
            continue
        if email_l not in rbac_emails:
            users.append({
                "email": email_l,
                "role": None,
                "grants": [],
                "revokes": [],
                "effective_permissions": [],
                "visible_modules": [],
                "updated_at": None,
                "updated_by": None,
                "subject_type": "staff",
            })

    return JSONResponse({"status": "ok", "users": users})


@router.put("/api/rbac/users/{email}/role")
async def set_rbac_user_role(
    email: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_rbac_manager),
):
    """Assign a role to a staff member (manager-admin/owner only)."""
    if _subject_type(email) != "staff":
        return _staff_target_blocked_response()

    body = await request.json()
    role = body.get("role")
    admin_email = user_info.get("email", "unknown")

    result = permissions_service.set_user_role(email, role, admin_email)
    if result:
        effective = sorted(permissions_service.get_effective_permissions(email))
        return JSONResponse({
            "success": True,
            "message": f"Role {'assigned' if role else 'removed'} for {email}",
            "user": {
                "email": result.email,
                "role": result.role,
                "effective_permissions": effective,
                "visible_modules": permissions_service.get_user_visible_modules(email),
                "subject_type": _subject_type(result.email),
            },
        })

    return JSONResponse({"success": False, "error": "Invalid role or failed to update"}, status_code=400)


@router.post("/api/rbac/users/{email}/grant")
async def grant_rbac_permission(
    email: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_rbac_manager),
):
    """Grant an extra permission to a staff member (manager-admin/owner only)."""
    if _subject_type(email) != "staff":
        return _staff_target_blocked_response()

    body = await request.json()
    permission = body.get("permission", "")
    admin_email = user_info.get("email", "unknown")

    result = permissions_service.grant_permission(email, permission, admin_email)
    if result:
        return JSONResponse({
            "success": True,
            "message": f"Granted {permission} to {email}",
            "user": {
                "email": result.email,
                "grants": result.grants,
                "revokes": result.revokes,
                "effective_permissions": sorted(permissions_service.get_effective_permissions(email)),
                "subject_type": _subject_type(result.email),
            },
        })

    return JSONResponse({"success": False, "error": "Invalid permission or failed to update"}, status_code=400)


@router.post("/api/rbac/users/{email}/revoke")
async def revoke_rbac_permission(
    email: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_rbac_manager),
):
    """Revoke a permission from a staff member (manager-admin/owner only)."""
    if _subject_type(email) != "staff":
        return _staff_target_blocked_response()

    body = await request.json()
    permission = body.get("permission", "")
    admin_email = user_info.get("email", "unknown")

    result = permissions_service.revoke_permission(email, permission, admin_email)
    if result:
        return JSONResponse({
            "success": True,
            "message": f"Revoked {permission} from {email}",
            "user": {
                "email": result.email,
                "grants": result.grants,
                "revokes": result.revokes,
                "effective_permissions": sorted(permissions_service.get_effective_permissions(email)),
                "subject_type": _subject_type(result.email),
            },
        })

    return JSONResponse({"success": False, "error": "Invalid permission or failed to update"}, status_code=400)


@router.delete("/api/rbac/users/{email}")
async def reset_rbac_user(email: str, user_info: dict = Depends(require_minecraft_rbac_manager)):
    """Reset a staff member to no-role (manager-admin/owner only)."""
    if _subject_type(email) != "staff":
        return _staff_target_blocked_response()

    admin_email = user_info.get("email", "unknown")
    if permissions_service.reset_user(email, admin_email):
        return JSONResponse({"success": True, "message": f"RBAC settings reset for {email}"})
    return JSONResponse({"success": False, "error": "No RBAC settings found for this user"}, status_code=404)


# =============================================================================
# Manager Admin Governance (Owner Only)
# =============================================================================


@router.get("/api/minecraft/admin-tiers/overview")
async def get_admin_tiers_overview(user_info: dict = Depends(require_minecraft_owner)):
    """Owner overview for manager admin + staff governance."""
    owner_email = user_info.get("email", "unknown")
    admin_tiers.reconcile_admin_tiers(actor=owner_email)
    overview = admin_tiers.get_owner_overview()
    return JSONResponse({"status": "ok", **overview})


@router.post("/api/minecraft/admin-tiers/promote/{email}")
async def promote_to_manager_admin(email: str, user_info: dict = Depends(require_minecraft_owner)):
    """Promote a staff subject to manager admin tracking (owner only)."""
    owner_email = user_info.get("email", "unknown")
    result = admin_tiers.promote_staff_to_manager_admin(email, owner_email)
    status_code = 200 if result.get("success") else 400
    return JSONResponse(result, status_code=status_code)


@router.post("/api/minecraft/admin-tiers/demote/{email}")
async def demote_to_staff(email: str, user_info: dict = Depends(require_minecraft_owner)):
    """Demote a tracked manager admin and restore previous staff settings."""
    owner_email = user_info.get("email", "unknown")
    result = admin_tiers.demote_manager_admin_to_staff(email, owner_email)
    status_code = 200 if result.get("success") else 400
    return JSONResponse(result, status_code=status_code)


@router.put("/api/minecraft/admin-tiers/manager-capabilities/{email}")
async def update_manager_admin_capabilities(
    email: str,
    request: Request,
    user_info: dict = Depends(require_minecraft_owner),
):
    """Owner-only capability grants for active tracked manager admins."""
    if not _is_active_manager_admin_subject(email):
        return _manager_admin_target_blocked_response()

    body = await request.json()
    capabilities = None
    if isinstance(body, dict):
        if "capabilities" in body:
            capabilities = body.get("capabilities")
        elif "capability" in body and "enabled" in body:
            capabilities = {body.get("capability"): body.get("enabled")}
        else:
            capabilities = body
    if not isinstance(capabilities, dict) or not capabilities:
        return JSONResponse(
            {"success": False, "error": "capabilities must be a non-empty object"},
            status_code=400,
        )

    unknown_permissions = sorted(set(capabilities) - _MANAGER_ADMIN_CAPABILITY_PERMISSIONS)
    if unknown_permissions:
        return JSONResponse(
            {
                "success": False,
                "error": f"Unsupported capability permissions: {', '.join(unknown_permissions)}",
            },
            status_code=400,
        )

    owner_email = user_info.get("email", "unknown")
    target_email = email.lower()
    for permission, enabled in capabilities.items():
        if not isinstance(enabled, bool):
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Capability '{permission}' must be true or false",
                },
                status_code=400,
            )
        if enabled:
            permissions_service.grant_permission(target_email, permission, owner_email)
        else:
            permissions_service.revoke_permission(target_email, permission, owner_email)

    admin_tiers.reconcile_admin_tiers(actor=owner_email)
    updated_record = next(
        (
            record
            for record in admin_tiers.get_manager_admin_records()
            if record.get("email") == target_email
        ),
        None,
    )
    if updated_record is None:
        return _manager_admin_target_blocked_response()

    return JSONResponse(
        {
            "success": True,
            "message": f"Updated manager admin whitelist capabilities for {target_email}",
            "manager_admin": updated_record,
        }
    )


@router.get("/api/minecraft/admin-audit/logs")
async def get_owner_audit_logs(
    limit: int = Query(default=100, ge=10, le=500),
    user_info: dict = Depends(require_minecraft_owner),
):
    """Owner-only audit log bundle for manager/admin actions."""
    owner_email = user_info.get("email", "unknown")
    admin_tiers.reconcile_admin_tiers(actor=owner_email)
    logs = admin_tiers.get_owner_audit_logs(limit=limit)
    return JSONResponse({"status": "ok", **logs})
