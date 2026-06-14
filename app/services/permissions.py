# app/services/permissions.py
"""
RBAC Permission Service

Role-based access control for Minecraft staff dashboard.
Manages role assignments, per-user permission grants/revokes, and module visibility.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
import threading

from app.core.config import DATA_DIR

logger = logging.getLogger(__name__)

# ============================================
# Audit Logger (same pattern as staff_audit.log)
# ============================================

rbac_logger = logging.getLogger("rbac_audit")
rbac_logger.setLevel(logging.INFO)

if not rbac_logger.handlers:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    handler = logging.FileHandler(logs_dir / "rbac_audit.log")
    handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    rbac_logger.addHandler(handler)

# ============================================
# Permission Map (모든 권한 정의)
# ============================================

ALL_PERMISSIONS = frozenset([
    "status:view", "players:view", "logs:view",
    "whitelist:view", "whitelist:add", "whitelist:remove", "whitelist:audit", "whitelist:prune",
    "lookup:coreprotect",
    "moderation:kick", "moderation:tempban", "moderation:broadcast",
    "warnings:view", "warnings:issue", "warnings:delete",
    "watchlist:view",
    "notes:view", "notes:manage",
    "investigation:view", "investigation:manage", "investigation:grimac", "investigation:mtrack",
    "spectator:quick_toggle",
    "map:view",
    "server:start", "server:restart",
    "plugins:view",
    "ops:backend_docs:view",
])

# Permission metadata: description and module grouping
PERMISSION_METADATA: Dict[str, dict] = {
    "status:view":              {"module": "status",        "description": "View server status"},
    "players:view":             {"module": "players",       "description": "View online players"},
    "logs:view":                {"module": "logs",          "description": "View server logs"},
    "whitelist:view":           {"module": "whitelist",     "description": "View whitelist"},
    "whitelist:add":            {"module": "whitelist",     "description": "Add players to whitelist"},
    "whitelist:remove":         {"module": "whitelist",     "description": "Remove players from whitelist"},
    "whitelist:audit":          {"module": "whitelist",     "description": "View whitelist activity audit"},
    "whitelist:prune":          {"module": "whitelist",     "description": "Manually prune inactive whitelist entries"},
    "lookup:coreprotect":       {"module": "lookup",        "description": "CoreProtect block lookup"},
    "moderation:kick":          {"module": "moderation",    "description": "Kick players"},
    "moderation:tempban":       {"module": "moderation",    "description": "Temporarily ban players"},
    "moderation:broadcast":     {"module": "moderation",    "description": "Send server broadcasts"},
    "warnings:view":            {"module": "warnings",      "description": "View player warnings"},
    "warnings:issue":           {"module": "warnings",      "description": "Issue warnings to players"},
    "warnings:delete":          {"module": "warnings",      "description": "Delete warnings"},
    "watchlist:view":           {"module": "watchlist",     "description": "View watchlist"},
    "notes:view":               {"module": "notes",         "description": "View player notes"},
    "notes:manage":             {"module": "notes",         "description": "Add/edit/delete notes"},
    "investigation:view":       {"module": "investigation", "description": "View investigation sessions"},
    "investigation:manage":     {"module": "investigation", "description": "Start/end investigations"},
    "investigation:grimac":     {"module": "investigation", "description": "GrimAC violation lookup"},
    "investigation:mtrack":     {"module": "investigation", "description": "mTrack player check"},
    "spectator:quick_toggle":   {"module": "gamemode_toggle", "description": "Quick toggle own gamemode (spectator/survival)"},
    "map:view":                 {"module": "map",             "description": "View the live BlueMap server map"},
    "server:start":             {"module": "server",        "description": "Start the server"},
    "server:restart":           {"module": "server",        "description": "Restart the server"},
    "plugins:view":             {"module": "plugins",       "description": "View plugin documentation"},
    "ops:backend_docs:view":    {"module": "operations_docs", "description": "View backend operations documentation"},
}

# ============================================
# Role Presets (정적 정의, 버전 관리)
# ============================================

# Base viewer permissions — all roles include these
_VIEWER_PERMS = frozenset([
    "status:view", "players:view", "logs:view",
    "whitelist:view", "lookup:coreprotect",
    "warnings:view", "watchlist:view", "notes:view",
    "investigation:view",
])

ROLE_PRESETS: Dict[str, dict] = {
    "viewer": {
        "description": "Probationary staff — view-only access with CoreProtect lookup",
        "permissions": _VIEWER_PERMS,
    },
    "moderator": {
        "description": "Active moderator — can kick, ban, warn, and investigate",
        "permissions": _VIEWER_PERMS | frozenset([
            "moderation:kick", "moderation:tempban",
            "warnings:issue",
            "notes:manage",
            "investigation:manage", "investigation:grimac", "investigation:mtrack",
            "plugins:view",
        ]),
    },
    "senior_moderator": {
        "description": "Trusted senior — moderator perms plus whitelist remove, warning delete, broadcast, restart",
        "permissions": _VIEWER_PERMS | frozenset([
            "moderation:kick", "moderation:tempban", "moderation:broadcast",
            "whitelist:remove",
            "warnings:issue", "warnings:delete",
            "notes:manage",
            "investigation:manage", "investigation:grimac", "investigation:mtrack",
            "server:start", "server:restart",
            "plugins:view",
        ]),
    },
    "community_manager": {
        "description": "Community-facing — whitelist add, broadcast, warn, notes management",
        "permissions": _VIEWER_PERMS | frozenset([
            "whitelist:add",
            "moderation:broadcast",
            "warnings:issue",
            "notes:manage",
        ]),
    },
}

# ============================================
# Data Model
# ============================================

RBAC_SETTINGS_FILE = DATA_DIR / "rbac_settings.json"
_file_lock = threading.Lock()


@dataclass
class UserRBAC:
    """Represents RBAC settings for a single staff member"""
    email: str
    role: Optional[str] = None
    grants: List[str] = field(default_factory=list)
    revokes: List[str] = field(default_factory=list)
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


# ============================================
# File I/O
# ============================================

def _load_settings() -> dict:
    """Load RBAC settings from JSON file"""
    if not RBAC_SETTINGS_FILE.exists():
        return {"version": 2, "users": {}}

    try:
        with open(RBAC_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Error loading settings file: %s", e)
        return {"version": 2, "users": {}}


def _save_settings(data: dict) -> bool:
    """Save RBAC settings to JSON file"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(RBAC_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        logger.error("Error saving settings file: %s", e)
        return False


# ============================================
# Core Functions
# ============================================

def get_effective_permissions(email: str) -> Set[str]:
    """
    Calculate effective permissions for a staff member.
    Formula: (role_perms | grants) - revokes
    Returns empty set if no role assigned (deny-all default).
    """
    with _file_lock:
        data = _load_settings()

    user_data = data.get("users", {}).get(email.lower())
    if not user_data:
        return set()

    role_name = user_data.get("role")
    if not role_name or role_name not in ROLE_PRESETS:
        # No valid role = only explicit grants (minus revokes)
        grants = set(p for p in user_data.get("grants", []) if p in ALL_PERMISSIONS)
        revokes = set(user_data.get("revokes", []))
        return grants - revokes

    role_perms = set(ROLE_PRESETS[role_name]["permissions"])
    grants = set(p for p in user_data.get("grants", []) if p in ALL_PERMISSIONS)
    revokes = set(user_data.get("revokes", []))

    return (role_perms | grants) - revokes


def has_permission(email: str, permission: str) -> bool:
    """Check if a staff member has a specific permission."""
    return permission in get_effective_permissions(email)


def get_user_rbac(email: str) -> UserRBAC:
    """Get RBAC data for a specific user."""
    with _file_lock:
        data = _load_settings()

    user_data = data.get("users", {}).get(email.lower())
    if user_data:
        return UserRBAC(
            email=user_data.get("email", email.lower()),
            role=user_data.get("role"),
            grants=user_data.get("grants", []),
            revokes=user_data.get("revokes", []),
            updated_at=user_data.get("updated_at"),
            updated_by=user_data.get("updated_by"),
        )

    return UserRBAC(email=email.lower())


def get_all_users() -> List[UserRBAC]:
    """Get RBAC data for all configured users."""
    with _file_lock:
        data = _load_settings()

    users = []
    for email, user_data in data.get("users", {}).items():
        users.append(UserRBAC(
            email=user_data.get("email", email),
            role=user_data.get("role"),
            grants=user_data.get("grants", []),
            revokes=user_data.get("revokes", []),
            updated_at=user_data.get("updated_at"),
            updated_by=user_data.get("updated_by"),
        ))
    return users


def set_user_role(email: str, role: Optional[str], admin_email: str) -> Optional[UserRBAC]:
    """
    Assign a role to a staff member. Pass None to unassign.
    """
    if role is not None and role not in ROLE_PRESETS:
        return None

    with _file_lock:
        data = _load_settings()

        if "users" not in data:
            data["users"] = {}

        email_lower = email.lower()
        old_role = data.get("users", {}).get(email_lower, {}).get("role")

        if email_lower not in data["users"]:
            data["users"][email_lower] = {
                "email": email_lower,
                "role": role,
                "grants": [],
                "revokes": [],
                "updated_at": datetime.now().isoformat(),
                "updated_by": admin_email,
            }
        else:
            data["users"][email_lower]["role"] = role
            data["users"][email_lower]["updated_at"] = datetime.now().isoformat()
            data["users"][email_lower]["updated_by"] = admin_email

        if _save_settings(data):
            rbac_logger.info(
                f"ROLE_CHANGE | admin={admin_email} | target={email_lower} | "
                f"old_role={old_role} | new_role={role}"
            )
            return UserRBAC(**data["users"][email_lower])

    return None


def grant_permission(email: str, permission: str, admin_email: str) -> Optional[UserRBAC]:
    """Grant an extra permission to a staff member."""
    if permission not in ALL_PERMISSIONS:
        return None

    with _file_lock:
        data = _load_settings()

        if "users" not in data:
            data["users"] = {}

        email_lower = email.lower()
        if email_lower not in data["users"]:
            data["users"][email_lower] = {
                "email": email_lower,
                "role": None,
                "grants": [],
                "revokes": [],
                "updated_at": datetime.now().isoformat(),
                "updated_by": admin_email,
            }

        user = data["users"][email_lower]
        if permission not in user["grants"]:
            user["grants"].append(permission)
        # Remove from revokes if it was revoked
        if permission in user.get("revokes", []):
            user["revokes"].remove(permission)
        user["updated_at"] = datetime.now().isoformat()
        user["updated_by"] = admin_email

        if _save_settings(data):
            rbac_logger.info(
                f"GRANT | admin={admin_email} | target={email_lower} | permission={permission}"
            )
            return UserRBAC(**data["users"][email_lower])

    return None


def revoke_permission(email: str, permission: str, admin_email: str) -> Optional[UserRBAC]:
    """Revoke a permission from a staff member (overrides role)."""
    if permission not in ALL_PERMISSIONS:
        return None

    with _file_lock:
        data = _load_settings()

        if "users" not in data:
            data["users"] = {}

        email_lower = email.lower()
        if email_lower not in data["users"]:
            data["users"][email_lower] = {
                "email": email_lower,
                "role": None,
                "grants": [],
                "revokes": [],
                "updated_at": datetime.now().isoformat(),
                "updated_by": admin_email,
            }

        user = data["users"][email_lower]
        if permission not in user.get("revokes", []):
            user["revokes"].append(permission)
        # Remove from grants if it was granted
        if permission in user.get("grants", []):
            user["grants"].remove(permission)
        user["updated_at"] = datetime.now().isoformat()
        user["updated_by"] = admin_email

        if _save_settings(data):
            rbac_logger.info(
                f"REVOKE | admin={admin_email} | target={email_lower} | permission={permission}"
            )
            return UserRBAC(**data["users"][email_lower])

    return None


def reset_user(email: str, admin_email: str) -> bool:
    """Remove all RBAC settings for a user (reset to no-role)."""
    with _file_lock:
        data = _load_settings()

        email_lower = email.lower()
        if email_lower in data.get("users", {}):
            old_data = data["users"][email_lower]
            del data["users"][email_lower]
            if _save_settings(data):
                rbac_logger.info(
                    f"RESET | admin={admin_email} | target={email_lower} | "
                    f"old_role={old_data.get('role')}"
                )
                return True

    return False


def get_user_visible_modules(email: str) -> List[str]:
    """
    Get list of module names the user can see (has ANY permission in that module).
    Used for UI tab filtering.
    """
    perms = get_effective_permissions(email)
    modules = set()
    for perm in perms:
        meta = PERMISSION_METADATA.get(perm)
        if meta:
            modules.add(meta["module"])
    return sorted(modules)


# ============================================
# Migration from v1 (staff_settings.json)
# ============================================

# Mapping from old hidden_features to new permissions to revoke
_V1_FEATURE_TO_REVOKES = {
    "plugin_installation": ["plugins:view"],
    "server_restart": ["server:restart"],
    "whitelist_remove": ["whitelist:remove"],
}


def migrate_from_v1() -> bool:
    """
    Migrate from staff_settings.json (v1) to rbac_settings.json (v2).
    Only runs once — checks for 'migrated_from_v1' flag.
    """
    with _file_lock:
        data = _load_settings()

        # Already migrated?
        if data.get("migrated_from_v1"):
            return False

        # Load v1 data
        v1_file = DATA_DIR / "staff_settings.json"
        if not v1_file.exists():
            data["migrated_from_v1"] = True
            _save_settings(data)
            return False

        try:
            with open(v1_file, 'r', encoding='utf-8') as f:
                v1_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            data["migrated_from_v1"] = True
            _save_settings(data)
            return False

        if "users" not in data:
            data["users"] = {}

        migrated_count = 0
        for email, staff_data in v1_data.get("staff", {}).items():
            email_lower = email.lower()
            hidden_features = staff_data.get("hidden_features", [])

            # Collect revokes from old hidden features
            revokes = []
            for feature in hidden_features:
                revokes.extend(_V1_FEATURE_TO_REVOKES.get(feature, []))

            # Only create entry if there are revokes to carry over
            if revokes and email_lower not in data["users"]:
                data["users"][email_lower] = {
                    "email": email_lower,
                    "role": None,  # Admin assigns roles after deployment
                    "grants": [],
                    "revokes": revokes,
                    "updated_at": datetime.now().isoformat(),
                    "updated_by": "system:migration",
                }
                migrated_count += 1

        data["version"] = 2
        data["migrated_from_v1"] = True

        if _save_settings(data):
            rbac_logger.info(
                f"MIGRATION | v1→v2 | migrated_users={migrated_count} | "
                f"source=staff_settings.json"
            )
            logger.info("RBAC migration: %d users migrated from v1", migrated_count)
            return True

    return False
