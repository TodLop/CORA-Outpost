"""
Minecraft-local access dependencies.

This module adds Minecraft-only admin gating without changing global auth.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request

from app.core import auth as core_auth
from app.services import permissions as permissions_service
from app.services import minecraft_admin_tiers as admin_tiers


def _normalize_email(email: str) -> str:
    return admin_tiers.normalize_email(email)


def _admin_email_set() -> set[str]:
    return {
        _normalize_email(email)
        for email in core_auth.ADMIN_EMAILS
        if _normalize_email(email)
    }


def is_minecraft_admin_email(email: str) -> bool:
    email_n = _normalize_email(email)
    if not email_n:
        return False
    if admin_tiers.is_minecraft_owner(email_n):
        return True
    if email_n in _admin_email_set():
        return True
    return admin_tiers.is_minecraft_manager_admin(email_n)


def is_minecraft_admin_user(user_info: Optional[dict]) -> bool:
    if not user_info:
        return False
    if core_auth.is_admin(user_info):
        return True
    email = user_info.get("email", "")
    return is_minecraft_admin_email(email)


def is_minecraft_rbac_manager_user(user_info: Optional[dict]) -> bool:
    """
    Users allowed to manage staff RBAC inside Minecraft module.
    Includes owner and manager-admin (including legacy global admins).
    """
    if not user_info:
        return False
    email = user_info.get("email", "")
    return admin_tiers.get_subject_type(email) in {"owner", "manager_admin"}


def is_minecraft_owner_or_manager_admin_user(user_info: Optional[dict]) -> bool:
    """Strict owner/current manager-admin check, excluding legacy global admins."""
    if not user_info:
        return False
    email = user_info.get("email", "")
    return admin_tiers.is_minecraft_owner(email) or admin_tiers.is_minecraft_manager_admin(email)


async def require_minecraft_admin(request: Request) -> dict:
    user_info = await core_auth.require_auth(request)
    if not is_minecraft_admin_user(user_info):
        raise HTTPException(status_code=403, detail="Minecraft admin access required")
    return user_info


async def require_minecraft_rbac_manager(request: Request) -> dict:
    user_info = await require_minecraft_admin(request)
    if not is_minecraft_rbac_manager_user(user_info):
        raise HTTPException(status_code=403, detail="Minecraft RBAC manager access required")
    return user_info


async def require_minecraft_owner_or_manager_admin(request: Request) -> dict:
    user_info = await require_minecraft_admin(request)
    if not is_minecraft_owner_or_manager_admin_user(user_info):
        raise HTTPException(status_code=403, detail="Minecraft owner or manager admin access required")
    return user_info


def require_minecraft_admin_permission(permission: str):
    async def dependency(request: Request) -> dict:
        user_info = await require_minecraft_admin(request)
        email = _normalize_email(user_info.get("email", ""))
        subject_type = admin_tiers.get_subject_type(email)

        if subject_type == "owner":
            return user_info
        if subject_type != "manager_admin":
            raise HTTPException(status_code=403, detail="Minecraft admin manager access required")
        if not permissions_service.has_permission(email, permission):
            raise HTTPException(
                status_code=403,
                detail=f"Minecraft admin permission denied: {permission}",
            )
        return user_info

    return dependency


async def require_minecraft_owner(request: Request) -> dict:
    user_info = await require_minecraft_admin(request)
    email = user_info.get("email", "")
    if not admin_tiers.is_minecraft_owner(email):
        raise HTTPException(status_code=403, detail="Minecraft owner access required")
    return user_info
