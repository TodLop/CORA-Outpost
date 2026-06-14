"""
Authentication and authorization utilities for Project Octopus.
Handles admin role checking and route protection.
"""

from typing import Optional
from fastapi import Request, HTTPException

from app.core.deployment_identity import get_access_bootstrap_identity
from app.services import permissions as permissions_service

_ACCESS_BOOTSTRAP_IDENTITY = get_access_bootstrap_identity()

# Owner email whitelist (single owner for owner-only modules)
OWNER_EMAIL = _ACCESS_BOOTSTRAP_IDENTITY.owner_email
STAFF_EMAILS = _ACCESS_BOOTSTRAP_IDENTITY.staff_emails


LOCAL_DEBUG_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Admin email whitelist - single source of truth
ADMIN_EMAILS = frozenset(
    email.strip().lower() for email in [OWNER_EMAIL] if email.strip()
)
STAFF_EMAILS_NORMALIZED = frozenset(email.strip().lower() for email in STAFF_EMAILS)


def _normalize_email(user_info: Optional[dict]) -> str:
    if not user_info:
        return ""
    return str(user_info.get("email") or "").strip().lower()


def get_current_user(request: Request) -> Optional[dict]:
    """Extract user info from session."""
    return request.session.get("user_info")


def is_authenticated(request: Request) -> bool:
    """Check if user is logged in."""
    return request.session.get("user_info") is not None


def is_admin(user_info: Optional[dict]) -> bool:
    """Check if user has admin privileges."""
    return _normalize_email(user_info) in ADMIN_EMAILS


def is_owner(user_info: Optional[dict]) -> bool:
    """Check if user is the owner account."""
    return _normalize_email(user_info) == OWNER_EMAIL


def is_staff(user_info: Optional[dict]) -> bool:
    """Check if user is a staff member (limited permissions)."""
    normalized_staff_emails = frozenset(email.strip().lower() for email in STAFF_EMAILS)
    return _normalize_email(user_info) in normalized_staff_emails


def is_admin_or_staff(user_info: Optional[dict]) -> bool:
    """Check if user is admin OR staff."""
    return is_admin(user_info) or is_staff(user_info)


def is_admin_request(request: Request) -> bool:
    """Check if current request is from an admin user."""
    user_info = get_current_user(request)
    return is_admin(user_info)


async def require_auth(request: Request) -> dict:
    """
    FastAPI dependency: require authenticated user.
    Raises 401 if not authenticated.
    """
    user_info = get_current_user(request)
    if not user_info:
        raise HTTPException(
            status_code=401,
            detail="Authentication required"
        )
    return user_info


async def require_admin(request: Request) -> dict:
    """
    FastAPI dependency: require admin user.
    Raises 401 if not authenticated, 403 if not admin.
    """
    user_info = await require_auth(request)
    if not is_admin(user_info):
        raise HTTPException(
            status_code=403,
            detail="Admin access required"
        )
    return user_info


async def require_staff(request: Request) -> dict:
    """
    FastAPI dependency: require staff or admin user.
    Raises 401 if not authenticated, 403 if not staff/admin.
    """
    user_info = await require_auth(request)
    if not is_admin_or_staff(user_info):
        raise HTTPException(
            status_code=403,
            detail="Staff access required"
        )
    return user_info


async def require_owner(request: Request) -> dict:
    """
    FastAPI dependency: require owner user.
    Raises 401 if not authenticated, 403 if not owner.
    """
    user_info = await require_auth(request)
    if not is_owner(user_info):
        raise HTTPException(
            status_code=403,
            detail="Owner access required"
        )
    return user_info


def require_permission(permission: str):
    """
    FastAPI dependency factory: require a specific RBAC permission.
    Admins bypass permission checks entirely.

    Usage: user_info: dict = Depends(require_permission("moderation:kick"))
    """
    async def dependency(request: Request) -> dict:
        user_info = await require_staff(request)  # reuses existing staff/admin check
        if is_admin(user_info):
            return user_info  # admins bypass RBAC
        if not permissions_service.has_permission(user_info["email"], permission):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {permission}"
            )
        return user_info
    return dependency
