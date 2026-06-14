"""
Minecraft-local admin tier service.

This service intentionally does NOT change global auth behavior.
It provides owner-only governance for Minecraft module operations:
- owner / manager_admin / staff subject typing
- staff RBAC snapshot preservation on promotion
- snapshot restore on demotion
- owner-visible role event/audit log reads
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core import auth as core_auth
from app.core.config import DATA_DIR, STAFF_EMAILS
from app.services import permissions as permissions_service
from app.services import staff_settings as staff_settings_service
from app.services.audit_log import audit_event

TIER_STATE_FILE = DATA_DIR / "minecraft_admin_tiers.json"
MANAGER_ADMIN_CAPABILITY_PERMISSIONS = (
    "whitelist:audit",
    "whitelist:prune",
)

_state_lock = threading.Lock()

role_events_logger = logging.getLogger("minecraft_role_events")
role_events_logger.setLevel(logging.INFO)
if not role_events_logger.handlers:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    handler = logging.FileHandler(logs_dir / "minecraft_role_events.log")
    handler.setFormatter(logging.Formatter("%(message)s"))
    role_events_logger.addHandler(handler)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _staff_set() -> set[str]:
    return {normalize_email(email) for email in STAFF_EMAILS if normalize_email(email)}


def _global_admin_set() -> set[str]:
    return {normalize_email(email) for email in core_auth.ADMIN_EMAILS if normalize_email(email)}


def _global_admins_excluding_owner() -> list[str]:
    owner = normalize_email(core_auth.OWNER_EMAIL)
    return sorted(email for email in _global_admin_set() if email != owner)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _blank_state() -> dict[str, Any]:
    return {"version": 2, "manager_admins": {}}


def _normalize_entry(email: str, entry: dict[str, Any] | None) -> dict[str, Any]:
    entry = entry if isinstance(entry, dict) else {}
    restored = bool(entry.get("restored_after_demotion"))
    active = entry.get("active")
    # v1 migration: infer active when flag is absent.
    if active is None:
        active = not restored
    return {
        "email": email,
        "active": bool(active),
        "promoted_at": entry.get("promoted_at"),
        "promoted_by": normalize_email(entry.get("promoted_by", "")) or None,
        "snapshot": entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {},
        "restored_after_demotion": restored,
        "demoted_at": entry.get("demoted_at"),
        "demoted_by": normalize_email(entry.get("demoted_by", "")) or None,
        "last_seen_as_manager_admin_at": entry.get("last_seen_as_manager_admin_at")
        or entry.get("last_seen_as_admin_at"),
    }


def _load_state() -> dict[str, Any]:
    if not TIER_STATE_FILE.exists():
        return _blank_state()

    try:
        with open(TIER_STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            if not isinstance(raw, dict):
                return _blank_state()
    except Exception:
        return _blank_state()

    entries_in = raw.get("manager_admins", {})
    entries_out: dict[str, Any] = {}
    if isinstance(entries_in, dict):
        for email, entry in entries_in.items():
            email_n = normalize_email(email)
            if not email_n:
                continue
            entries_out[email_n] = _normalize_entry(email_n, entry if isinstance(entry, dict) else {})

    return {"version": 2, "manager_admins": entries_out}


def _save_state(data: dict[str, Any]) -> bool:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(TIER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def _snapshot_staff_state(email: str) -> dict[str, Any]:
    user = permissions_service.get_user_rbac(email)
    hidden_features = staff_settings_service.get_staff_settings(email).hidden_features
    return {
        "role": user.role,
        "grants": list(user.grants or []),
        "revokes": list(user.revokes or []),
        "hidden_features": list(hidden_features or []),
    }


def _apply_staff_snapshot(email: str, snapshot: dict[str, Any], actor: str) -> None:
    # Reset RBAC first, then replay snapshot exactly.
    permissions_service.reset_user(email, actor)
    role = snapshot.get("role")
    grants = list(snapshot.get("grants", []) or [])
    revokes = list(snapshot.get("revokes", []) or [])

    if role is not None:
        permissions_service.set_user_role(email, role, actor)

    for permission in grants:
        permissions_service.grant_permission(email, permission, actor)
    for permission in revokes:
        permissions_service.revoke_permission(email, permission, actor)

    hidden_features = list(snapshot.get("hidden_features", []) or [])
    if hidden_features:
        staff_settings_service.update_staff_settings(email, hidden_features, actor)
    else:
        staff_settings_service.delete_staff_settings(email)


def _log_role_event(*, actor: str, action: str, target: str, result: str, extra: dict[str, Any] | None = None) -> None:
    audit_event(
        logger=role_events_logger,
        actor=normalize_email(actor),
        action=action,
        target=normalize_email(target),
        result=result,
        extra=extra or {},
    )


def is_minecraft_owner(email: str) -> bool:
    return normalize_email(email) == normalize_email(core_auth.OWNER_EMAIL)


def get_current_manager_admins() -> list[str]:
    with _state_lock:
        state = _load_state()
        entries: dict[str, Any] = state.get("manager_admins", {})
        return sorted(
            email
            for email, entry in entries.items()
            if isinstance(entry, dict) and bool(entry.get("active"))
        )


def is_minecraft_manager_admin(email: str) -> bool:
    return normalize_email(email) in set(get_current_manager_admins())


def is_legacy_global_admin(email: str) -> bool:
    email_n = normalize_email(email)
    return bool(email_n) and email_n in set(_global_admins_excluding_owner())


def is_minecraft_admin(email: str) -> bool:
    email_n = normalize_email(email)
    if not email_n:
        return False
    if is_minecraft_owner(email_n):
        return True
    if is_minecraft_manager_admin(email_n):
        return True
    return is_legacy_global_admin(email_n)


def get_subject_type(email: str) -> str:
    email_n = normalize_email(email)
    if is_minecraft_owner(email_n):
        return "owner"
    if is_minecraft_manager_admin(email_n) or is_legacy_global_admin(email_n):
        return "manager_admin"
    if email_n in _staff_set():
        return "staff"
    return "external"


def reconcile_admin_tiers(actor: str = "system:reconcile") -> dict[str, int]:
    """
    Normalize local manager-admin tier state.

    This intentionally does not auto-promote or auto-demote from global ADMIN_EMAILS.
    """
    actor_n = normalize_email(actor) or "system:reconcile"
    normalized = 0
    captured = 0

    with _state_lock:
        state = _load_state()
        entries: dict[str, Any] = state.setdefault("manager_admins", {})
        staff_set = _staff_set()

        for email, entry in list(entries.items()):
            if not isinstance(entry, dict):
                entries[email] = _normalize_entry(email, {})
                normalized += 1
                continue

            normalized_entry = _normalize_entry(email, entry)
            if normalized_entry != entry:
                entries[email] = normalized_entry
                normalized += 1

            entry = entries[email]
            if bool(entry.get("active")):
                entry["last_seen_as_manager_admin_at"] = _now_iso()
                # Backfill snapshot when missing and user is staff.
                if email in staff_set and not isinstance(entry.get("snapshot"), dict):
                    entry["snapshot"] = _snapshot_staff_state(email)
                    captured += 1

        _save_state(state)

    return {"captured": captured, "restored": 0, "normalized": normalized}


def promote_staff_to_manager_admin(email: str, actor: str) -> dict[str, Any]:
    email_n = normalize_email(email)
    actor_n = normalize_email(actor) or "unknown"

    if not email_n:
        return {"success": False, "error": "Email is required"}
    if is_minecraft_owner(email_n):
        return {"success": False, "error": "Owner cannot be promoted"}
    if email_n not in _staff_set():
        return {"success": False, "error": "Only staff accounts can be promoted"}

    with _state_lock:
        state = _load_state()
        entries = state.setdefault("manager_admins", {})
        entry = _normalize_entry(email_n, entries.get(email_n, {}))

        if not isinstance(entry.get("snapshot"), dict) or not entry.get("snapshot"):
            entry["snapshot"] = _snapshot_staff_state(email_n)

        entry["email"] = email_n
        entry["active"] = True
        entry["promoted_at"] = _now_iso()
        entry["promoted_by"] = actor_n
        entry["restored_after_demotion"] = False
        entry["demoted_at"] = None
        entry["demoted_by"] = None
        entry["last_seen_as_manager_admin_at"] = _now_iso()
        entries[email_n] = entry
        _save_state(state)

    _log_role_event(
        actor=actor_n,
        action="promote_to_manager_admin",
        target=email_n,
        result="ok",
        extra={
            "minecraft_admin_active": True,
            "auth_admin_active": email_n in _global_admin_set(),
            "note": "Minecraft admin access now active for this account",
        },
    )
    return {
        "success": True,
        "email": email_n,
        "minecraft_admin_active": True,
        "auth_admin_active": email_n in _global_admin_set(),
    }


def demote_manager_admin_to_staff(email: str, actor: str) -> dict[str, Any]:
    email_n = normalize_email(email)
    actor_n = normalize_email(actor) or "unknown"

    if not email_n:
        return {"success": False, "error": "Email is required"}
    if is_minecraft_owner(email_n):
        return {"success": False, "error": "Owner cannot be demoted"}

    with _state_lock:
        state = _load_state()
        entries = state.setdefault("manager_admins", {})
        entry = _normalize_entry(email_n, entries.get(email_n))
        if not isinstance(entries.get(email_n), dict):
            return {"success": False, "error": "No manager admin history found"}

        snapshot = entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else None
        if not snapshot:
            return {"success": False, "error": "No staff snapshot available"}

        _apply_staff_snapshot(email_n, snapshot, actor_n)
        entry["active"] = False
        entry["restored_after_demotion"] = True
        entry["demoted_at"] = _now_iso()
        entry["demoted_by"] = actor_n
        entries[email_n] = entry
        _save_state(state)

    _log_role_event(
        actor=actor_n,
        action="demote_to_staff",
        target=email_n,
        result="ok",
        extra={
            "minecraft_admin_active": False,
            "auth_admin_active": email_n in _global_admin_set(),
        },
    )
    return {
        "success": True,
        "email": email_n,
        "minecraft_admin_active": False,
        "auth_admin_active": email_n in _global_admin_set(),
    }


def get_manager_admin_records() -> list[dict[str, Any]]:
    with _state_lock:
        state = _load_state()
        entries: dict[str, Any] = state.get("manager_admins", {})

    current_admins = _global_admin_set()
    records: list[dict[str, Any]] = []
    for email, raw in entries.items():
        entry = _normalize_entry(email, raw if isinstance(raw, dict) else {})
        user_rbac = permissions_service.get_user_rbac(email)
        grants = set(user_rbac.grants or [])
        revokes = set(user_rbac.revokes or [])
        effective = permissions_service.get_effective_permissions(email)
        records.append({
            "email": email,
            "minecraft_admin_active": bool(entry.get("active")),
            "promoted_at": entry.get("promoted_at"),
            "promoted_by": entry.get("promoted_by"),
            "demoted_at": entry.get("demoted_at"),
            "demoted_by": entry.get("demoted_by"),
            "restored_after_demotion": bool(entry.get("restored_after_demotion")),
            "auth_admin_active": email in current_admins,
            "capabilities": {
                permission: {
                    "enabled": permission in effective,
                    "granted": permission in grants,
                    "revoked": permission in revokes,
                }
                for permission in MANAGER_ADMIN_CAPABILITY_PERMISSIONS
            },
            "whitelist_audit_granted": "whitelist:audit" in effective,
            "whitelist_prune_granted": "whitelist:prune" in effective,
            "rbac_updated_at": user_rbac.updated_at,
            "rbac_updated_by": user_rbac.updated_by,
        })
    records.sort(key=lambda record: record.get("email", ""))
    return records


def get_owner_overview() -> dict[str, Any]:
    reconcile_admin_tiers()
    manager_admin_records = get_manager_admin_records()
    current_manager_admins = get_current_manager_admins()
    legacy_admins = _global_admins_excluding_owner()

    staff_users: list[dict[str, Any]] = []
    for email in sorted(_staff_set()):
        if get_subject_type(email) != "staff":
            continue
        rbac = permissions_service.get_user_rbac(email)
        effective = sorted(permissions_service.get_effective_permissions(email))
        staff_users.append({
            "email": email,
            "role": rbac.role,
            "effective_permissions_count": len(effective),
            "updated_at": rbac.updated_at,
            "updated_by": rbac.updated_by,
        })

    effective_manager_admins = sorted(set(current_manager_admins) | set(legacy_admins))

    return {
        "owner_email": normalize_email(core_auth.OWNER_EMAIL),
        "manager_admins_current": current_manager_admins,
        "legacy_admins_current": legacy_admins,
        "manager_admins_effective": effective_manager_admins,
        "manager_admin_records": manager_admin_records,
        "staff_users": staff_users,
    }


def _read_jsonl_tail(file_path: Path, limit: int) -> list[dict[str, Any]]:
    if not file_path.exists():
        return []
    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                items.append(payload)
        except Exception:
            items.append({"raw": line})
    return items[::-1]


def get_owner_audit_logs(limit: int = 100) -> dict[str, Any]:
    safe_limit = max(10, min(int(limit or 100), 500))
    logs_dir = Path("logs")
    return {
        "role_events": _read_jsonl_tail(logs_dir / "minecraft_role_events.log", safe_limit),
        "admin_audit": _read_jsonl_tail(logs_dir / "admin_audit.log", safe_limit),
        "rbac_audit": _read_jsonl_tail(logs_dir / "rbac_audit.log", safe_limit),
    }
