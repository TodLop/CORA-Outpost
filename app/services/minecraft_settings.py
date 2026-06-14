"""
Minecraft server profile settings.

The admin UI historically stored one managed server directory here.  The
profile model keeps that compatibility surface while allowing the active
managed directory to become a pointer to one selected server profile.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core import config

SETTINGS_FILE = config.DATA_DIR / "minecraft_server_settings.json"
PROFILE_STATE_ROOT = config.DATA_DIR / "minecraft_server_profiles"
SAMPLE_PROFILE_ID = "sample"
SAMPLE_SERVER_DIRECTORY = "examples/minecraft_sample_server"
DEFAULT_LIVE_PROFILE_ID = "main"

_file_lock = threading.Lock()


class PathValidationError(ValueError):
    """Raised when a proposed Minecraft server directory is not usable."""

    def __init__(self, errors: dict[str, str], *, warnings: list[str] | None = None):
        super().__init__("Invalid Minecraft server directory")
        self.errors = errors
        self.warnings = warnings or []


class ProfileValidationError(ValueError):
    """Raised when a server profile mutation is invalid."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("Invalid Minecraft server profile")
        self.errors = errors


def get_default_server_directory() -> Path:
    """Return the code-configured default server directory."""
    return Path(config.MINECRAFT_SERVER_PATH).expanduser().resolve()


def _normalize_path(raw_path: str | os.PathLike[str]) -> Path:
    expanded = Path(raw_path).expanduser()
    if not expanded.is_absolute():
        expanded = config.ROOT_DIR / expanded
    return expanded.resolve(strict=False)


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _default_sample_profile() -> dict[str, Any]:
    timestamp = _now_iso()
    return {
        "id": SAMPLE_PROFILE_ID,
        "name": "Sample Server",
        "kind": "sample",
        "server_directory": SAMPLE_SERVER_DIRECTORY,
        "operations_enabled": False,
        "rcon_enabled": False,
        "readonly": True,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _profile_payload(
    *,
    profile_id: str,
    name: str,
    kind: str,
    server_directory: str | os.PathLike[str],
    operations_enabled: bool,
    rcon_enabled: bool,
    readonly: bool,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = _now_iso()
    return {
        "id": profile_id,
        "name": name,
        "kind": kind,
        "server_directory": str(server_directory),
        "operations_enabled": bool(operations_enabled),
        "rcon_enabled": bool(rcon_enabled),
        "readonly": bool(readonly),
        "created_at": created_at or timestamp,
        "updated_at": updated_at or timestamp,
    }


def _empty_payload() -> dict[str, Any]:
    sample = _default_sample_profile()
    return {
        "active_profile_id": sample["id"],
        "profiles": [sample],
        "updated_at": None,
        "updated_by": None,
    }


def _legacy_payload_to_profiles(payload: dict[str, Any]) -> dict[str, Any]:
    sample = _default_sample_profile()
    raw_path = str(payload.get("server_directory") or "").strip()
    if not raw_path:
        return _empty_payload()

    server_directory = _normalize_path(raw_path)
    timestamp = str(payload.get("updated_at") or "") or _now_iso()
    live_profile = _profile_payload(
        profile_id=DEFAULT_LIVE_PROFILE_ID,
        name="Main Server",
        kind="live",
        server_directory=str(server_directory),
        operations_enabled=True,
        rcon_enabled=True,
        readonly=False,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return {
        "active_profile_id": live_profile["id"],
        "profiles": [sample, live_profile],
        "updated_at": payload.get("updated_at"),
        "updated_by": payload.get("updated_by"),
    }


def _normalize_profile(raw_profile: Any) -> dict[str, Any] | None:
    if not isinstance(raw_profile, dict):
        return None

    profile_id = str(raw_profile.get("id") or "").strip()
    name = str(raw_profile.get("name") or "").strip()
    server_directory = str(raw_profile.get("server_directory") or "").strip()
    if not profile_id or not server_directory:
        return None

    kind = str(raw_profile.get("kind") or "live").strip() or "live"
    return _profile_payload(
        profile_id=profile_id,
        name=name or profile_id,
        kind=kind,
        server_directory=server_directory,
        operations_enabled=bool(raw_profile.get("operations_enabled", kind != "sample")),
        rcon_enabled=bool(raw_profile.get("rcon_enabled", kind != "sample")),
        readonly=bool(raw_profile.get("readonly", kind == "sample")),
        created_at=str(raw_profile.get("created_at") or "") or None,
        updated_at=str(raw_profile.get("updated_at") or "") or None,
    )


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list):
        return _legacy_payload_to_profiles(payload)

    profiles_by_id: dict[str, dict[str, Any]] = {}
    for raw_profile in raw_profiles:
        profile = _normalize_profile(raw_profile)
        if profile is not None:
            profiles_by_id[profile["id"]] = profile

    if SAMPLE_PROFILE_ID not in profiles_by_id:
        profiles_by_id = {SAMPLE_PROFILE_ID: _default_sample_profile(), **profiles_by_id}

    profiles = list(profiles_by_id.values())
    active_profile_id = str(payload.get("active_profile_id") or "").strip()
    if active_profile_id not in profiles_by_id:
        active_profile_id = SAMPLE_PROFILE_ID if SAMPLE_PROFILE_ID in profiles_by_id else profiles[0]["id"]

    return {
        "active_profile_id": active_profile_id,
        "profiles": profiles,
        "updated_at": payload.get("updated_at"),
        "updated_by": payload.get("updated_by"),
    }


def _load_payload() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return _empty_payload()

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _empty_payload()

    if not isinstance(payload, dict):
        return _empty_payload()

    normalized = _normalize_payload(payload)
    if normalized != payload:
        _save_payload(normalized)
    return normalized


def _save_payload(payload: dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = SETTINGS_FILE.with_suffix(f"{SETTINGS_FILE.suffix}.tmp")
    with open(temp_file, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    temp_file.replace(SETTINGS_FILE)


def inspect_server_directory(raw_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return validation details for a Minecraft server directory."""
    errors: dict[str, str] = {}
    warnings: list[str] = []

    raw_text = str(raw_path or "").strip()
    if not raw_text:
        errors["server_directory"] = "Server directory is required."
        resolved = get_default_server_directory()
    else:
        expanded = Path(raw_text).expanduser()
        if not expanded.is_absolute():
            errors["server_directory"] = "Use an absolute path."
            resolved = expanded
        else:
            resolved = expanded.resolve(strict=False)

    if not errors:
        if not resolved.exists():
            errors["server_directory"] = "Directory does not exist."
        elif not resolved.is_dir():
            errors["server_directory"] = "Path is not a directory."
        else:
            if not os.access(resolved, os.R_OK | os.X_OK):
                errors["permissions"] = "Directory is not readable."
            if not os.access(resolved, os.W_OK):
                errors["writable"] = "Directory must be writable."

            start_script = resolved / "start.sh"
            if not start_script.is_file():
                errors["start_script"] = "start.sh was not found in this directory."

            server_properties = resolved / "server.properties"
            if not server_properties.is_file():
                errors["server_properties"] = "server.properties was not found in this directory."

            if not (resolved / "plugins").exists():
                warnings.append("plugins/ was not found; plugin features may appear empty.")
            if not (resolved / "logs").exists():
                warnings.append("logs/ was not found; log viewers will start empty.")

    return {
        "valid": not errors,
        "path": str(resolved),
        "errors": errors,
        "warnings": warnings,
    }


def validate_server_directory(raw_path: str | os.PathLike[str]) -> Path:
    validation = inspect_server_directory(raw_path)
    if not validation["valid"]:
        raise PathValidationError(validation["errors"], warnings=validation["warnings"])
    return Path(validation["path"])


def _profile_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {profile["id"]: profile for profile in payload.get("profiles", [])}


def _copy_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return dict(profile)


def list_profiles() -> list[dict[str, Any]]:
    """Return all configured server profiles."""
    return [_copy_profile(profile) for profile in _load_payload()["profiles"]]


def get_profile(profile_id: str) -> dict[str, Any]:
    """Return one server profile by id."""
    profiles = _profile_map(_load_payload())
    profile = profiles.get(str(profile_id or "").strip())
    if profile is None:
        raise KeyError(f"Unknown Minecraft server profile: {profile_id}")
    return _copy_profile(profile)


def get_active_profile() -> dict[str, Any]:
    """Return the currently selected server profile."""
    payload = _load_payload()
    return _copy_profile(_profile_map(payload)[payload["active_profile_id"]])


def get_active_profile_operation_block(action: str, *, requires_rcon: bool = False) -> dict[str, Any] | None:
    """Return a failure result when the active profile disallows an operation."""
    profile = get_active_profile()
    profile_name = str(profile.get("name") or profile.get("id") or "active profile")
    action_label = str(action or "operation").strip() or "operation"
    base_result = {
        "success": False,
        "profile_id": profile["id"],
        "profile_name": profile_name,
    }

    if profile.get("readonly"):
        return {
            **base_result,
            "error_code": "profile_readonly",
            "error": f"Profile '{profile_name}' is readonly; {action_label} is not allowed.",
        }

    if not profile.get("operations_enabled", True):
        return {
            **base_result,
            "error_code": "profile_operations_disabled",
            "error": f"Profile '{profile_name}' has operations disabled; {action_label} is not allowed.",
        }

    if requires_rcon and not profile.get("rcon_enabled", True):
        return {
            **base_result,
            "error_code": "profile_rcon_disabled",
            "error": f"Profile '{profile_name}' has RCON disabled; {action_label} is not allowed.",
        }

    return None


def _active_server_directory(payload: dict[str, Any]) -> Path:
    active_profile = _profile_map(payload)[payload["active_profile_id"]]
    return _normalize_path(active_profile["server_directory"])


def get_server_directory() -> Path:
    """Return the active profile server directory."""
    payload = _load_payload()
    return _active_server_directory(payload)


def get_settings() -> dict[str, Any]:
    payload = _load_payload()
    active_profile = get_active_profile()
    server_directory = _active_server_directory(payload)
    validation = inspect_server_directory(server_directory)
    return {
        "server_directory": str(server_directory),
        "default_server_directory": str(get_default_server_directory()),
        "active_profile_id": payload["active_profile_id"],
        "active_profile": active_profile,
        "profiles": list_profiles(),
        "validation": validation,
        "updated_at": payload.get("updated_at"),
        "updated_by": payload.get("updated_by"),
    }


def _validate_profile_id(profile_id: str) -> str:
    normalized = str(profile_id or "").strip().lower()
    if not normalized:
        raise ProfileValidationError({"id": "Profile id is required."})
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", normalized):
        raise ProfileValidationError({
            "id": "Use 1-64 lowercase letters, numbers, dashes, or underscores; start with a letter or number."
        })
    return normalized


def _slugify_profile_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-_")
    return slug[:64] or DEFAULT_LIVE_PROFILE_ID


def _unique_profile_id(base: str, existing_ids: set[str]) -> str:
    candidate = _validate_profile_id(_slugify_profile_id(base))
    if candidate not in existing_ids:
        return candidate
    suffix = 2
    while True:
        trimmed = candidate[: max(1, 64 - len(str(suffix)) - 1)]
        next_candidate = f"{trimmed}-{suffix}"
        if next_candidate not in existing_ids:
            return next_candidate
        suffix += 1


def set_active_profile(profile_id: str) -> dict[str, Any]:
    """Switch the active profile pointer without touching any server process."""
    normalized_id = str(profile_id or "").strip()
    with _file_lock:
        payload = _load_payload()
        if normalized_id not in _profile_map(payload):
            raise KeyError(f"Unknown Minecraft server profile: {profile_id}")
        payload["active_profile_id"] = normalized_id
        _save_payload(payload)
    return get_settings()


def create_profile(
    *,
    name: str,
    server_directory: str | os.PathLike[str],
    profile_id: str | None = None,
    kind: str = "live",
    operations_enabled: bool = True,
    rcon_enabled: bool = True,
    readonly: bool = False,
    set_active: bool = False,
) -> dict[str, Any]:
    """Create a live server profile after validating its server directory."""
    server_path = validate_server_directory(server_directory)
    display_name = str(name or "").strip()
    if not display_name:
        raise ProfileValidationError({"name": "Profile name is required."})

    with _file_lock:
        payload = _load_payload()
        existing_ids = set(_profile_map(payload))
        new_profile_id = (
            _validate_profile_id(profile_id)
            if profile_id is not None
            else _unique_profile_id(display_name, existing_ids)
        )
        if new_profile_id in existing_ids:
            raise ProfileValidationError({"id": "Profile id already exists."})

        profile = _profile_payload(
            profile_id=new_profile_id,
            name=display_name,
            kind=str(kind or "live").strip() or "live",
            server_directory=str(server_path),
            operations_enabled=operations_enabled,
            rcon_enabled=rcon_enabled,
            readonly=readonly,
        )
        payload["profiles"].append(profile)
        if set_active:
            payload["active_profile_id"] = profile["id"]
        _save_payload(payload)
    return get_profile(new_profile_id)


def update_profile(
    profile_id: str,
    *,
    name: str | None = None,
    server_directory: str | os.PathLike[str] | None = None,
    operations_enabled: bool | None = None,
    rcon_enabled: bool | None = None,
    readonly: bool | None = None,
) -> dict[str, Any]:
    """Update mutable profile settings for non-readonly profiles."""
    normalized_id = str(profile_id or "").strip()
    with _file_lock:
        payload = _load_payload()
        profiles = _profile_map(payload)
        profile = profiles.get(normalized_id)
        if profile is None:
            raise KeyError(f"Unknown Minecraft server profile: {profile_id}")
        if profile.get("readonly"):
            raise ProfileValidationError({"profile": "Readonly profiles cannot be modified."})

        if name is not None:
            display_name = str(name or "").strip()
            if not display_name:
                raise ProfileValidationError({"name": "Profile name is required."})
            profile["name"] = display_name
        if server_directory is not None:
            profile["server_directory"] = str(validate_server_directory(server_directory))
        if operations_enabled is not None:
            profile["operations_enabled"] = bool(operations_enabled)
        if rcon_enabled is not None:
            profile["rcon_enabled"] = bool(rcon_enabled)
        if readonly is not None:
            profile["readonly"] = bool(readonly)
        profile["updated_at"] = _now_iso()

        payload["profiles"] = list(profiles.values())
        _save_payload(payload)
    return get_profile(normalized_id)


def delete_profile(profile_id: str) -> dict[str, Any]:
    """Delete a non-active, non-readonly profile."""
    normalized_id = str(profile_id or "").strip()
    with _file_lock:
        payload = _load_payload()
        profiles = _profile_map(payload)
        profile = profiles.get(normalized_id)
        if profile is None:
            raise KeyError(f"Unknown Minecraft server profile: {profile_id}")
        if normalized_id == payload["active_profile_id"]:
            raise ProfileValidationError({"profile": "Cannot delete the active Minecraft server profile."})
        if profile.get("readonly"):
            raise ProfileValidationError({"profile": "Readonly profiles cannot be deleted."})

        payload["profiles"] = [
            existing for existing in payload["profiles"] if existing["id"] != normalized_id
        ]
        _save_payload(payload)
    return get_settings()


def set_server_directory(raw_path: str | os.PathLike[str], *, updated_by: str = "") -> dict[str, Any]:
    """Validate and persist the active live Minecraft server directory."""
    server_directory = validate_server_directory(raw_path)
    with _file_lock:
        payload = _load_payload()
        profiles = _profile_map(payload)
        active_profile = profiles[payload["active_profile_id"]]
        if active_profile.get("readonly"):
            live_profile = profiles.get(DEFAULT_LIVE_PROFILE_ID)
            if live_profile is None or live_profile.get("readonly"):
                live_profile = _profile_payload(
                    profile_id=DEFAULT_LIVE_PROFILE_ID,
                    name="Main Server",
                    kind="live",
                    server_directory=str(server_directory),
                    operations_enabled=True,
                    rcon_enabled=True,
                    readonly=False,
                )
                profiles[live_profile["id"]] = live_profile
            else:
                live_profile["server_directory"] = str(server_directory)
                live_profile["updated_at"] = _now_iso()
            payload["active_profile_id"] = live_profile["id"]
        else:
            active_profile["server_directory"] = str(server_directory)
            active_profile["updated_at"] = _now_iso()

        payload["profiles"] = list(profiles.values())
        payload["updated_at"] = _now_iso()
        payload["updated_by"] = updated_by or None
        _save_payload(payload)
    return get_settings()


def get_profile_state_dir(profile_id: str) -> Path:
    """Return the profile-scoped runtime state directory path."""
    normalized_id = str(profile_id or "").strip()
    if normalized_id not in _profile_map(_load_payload()):
        raise KeyError(f"Unknown Minecraft server profile: {profile_id}")
    return PROFILE_STATE_ROOT / normalized_id


def get_active_profile_state_dir() -> Path:
    """Return the active profile-scoped runtime state directory path."""
    return get_profile_state_dir(_load_payload()["active_profile_id"])


def get_server_properties_path() -> Path:
    return get_server_directory() / "server.properties"


def get_start_script_path() -> Path:
    return get_server_directory() / "start.sh"


def get_logs_dir() -> Path:
    return get_server_directory() / "logs"


def get_latest_log_path() -> Path:
    return get_logs_dir() / "latest.log"


def get_console_history_file() -> Path:
    return get_logs_dir() / "cora_console_history.jsonl"


def get_pid_file() -> Path:
    return get_active_profile_state_dir() / "server.pid"


def get_plugins_dir() -> Path:
    return get_server_directory() / "plugins"


def get_backups_dir() -> Path:
    return get_server_directory() / "backups"


def get_update_logs_dir() -> Path:
    return get_server_directory() / "update_logs"


def get_versions_file() -> Path:
    return get_server_directory() / "versions.json"


def get_whitelist_path() -> Path:
    return get_server_directory() / "whitelist.json"


def get_usercache_path() -> Path:
    return get_server_directory() / "usercache.json"


def get_server_icon_path() -> Path | None:
    """Return the configured server icon path when present."""
    server_directory = get_server_directory()
    for filename in ("server-icon.png", "sever-icon.png"):
        candidate = server_directory / filename
        if candidate.is_file():
            return candidate
    return None


def get_coreprotect_db_path() -> Path:
    return get_plugins_dir() / "CoreProtect" / "database.db"


def get_grimac_db_path() -> Path:
    return get_plugins_dir() / "GrimAC" / "violations.sqlite"


def get_essentials_userdata_path() -> Path:
    return get_plugins_dir() / "Essentials" / "userdata"


def get_player_auctions_db_path() -> Path:
    return get_plugins_dir() / "PlayerAuctions" / "data" / "database.db"
