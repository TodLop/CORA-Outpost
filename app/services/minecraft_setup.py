"""Read-only helpers for the Minecraft first-run setup preview."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psutil

from app.services import native_folder_picker


MIB = 1024 * 1024
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
GAMEMODES = ("survival", "creative", "adventure", "spectator")
DEFAULT_PROFILE_NAME = "CORA-Outpost Server"
DEFAULT_MOTD = "CORA-Outpost"
DEFAULT_PAPER_FILENAME = "paper.jar"
SAFE_JAR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.jar$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
SLUG_RE = re.compile(r"[^a-z0-9]+")

AIKAR_FLAGS = (
    "-XX:+UseG1GC",
    "-XX:+ParallelRefProcEnabled",
    "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions",
    "-XX:+DisableExplicitGC",
    "-XX:+AlwaysPreTouch",
    "-XX:G1NewSizePercent=30",
    "-XX:G1MaxNewSizePercent=40",
    "-XX:G1HeapRegionSize=8M",
    "-XX:G1ReservePercent=20",
    "-XX:G1HeapWastePercent=5",
    "-XX:G1MixedGCCountTarget=4",
    "-XX:InitiatingHeapOccupancyPercent=15",
    "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:G1RSetUpdatingPauseTimePercent=5",
    "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem",
    "-XX:MaxTenuringThreshold=1",
)


class SetupValidationError(ValueError):
    """Raised when a setup preview payload cannot be rendered safely."""

    def __init__(self, errors: dict[str, str], warnings: list[str] | None = None):
        super().__init__("Invalid Minecraft setup preview payload.")
        self.errors = errors
        self.warnings = warnings or []


class SetupCreationNotReady(SetupValidationError):
    """Raised when a valid setup payload is not safe to create from yet."""

    def __init__(
        self,
        errors: dict[str, str],
        warnings: list[str] | None = None,
        *,
        preflight: dict[str, Any],
        preview: dict[str, Any],
    ):
        super().__init__(errors, warnings)
        self.preflight = preflight
        self.preview = preview


class SetupFolderPickerCancelled(ValueError):
    """Raised when the setup folder picker is cancelled."""


class SetupFolderPickerUnavailable(RuntimeError):
    """Raised when the setup folder picker cannot be opened."""


def choose_setup_server_directory() -> str:
    """Return a selected folder path for setup preview without persisting it."""
    try:
        return native_folder_picker.choose_directory_with_native_dialog(
            "Choose a Minecraft server folder for setup preview"
        )
    except native_folder_picker.FolderPickerCancelled as exc:
        raise SetupFolderPickerCancelled(str(exc)) from exc
    except native_folder_picker.FolderPickerUnavailable as exc:
        raise SetupFolderPickerUnavailable(str(exc)) from exc


def get_host_memory_summary(expected_players: int = 10) -> dict[str, int]:
    """Return host memory details for safe setup defaults."""
    memory = psutil.virtual_memory()
    total_mb = max(0, int(memory.total // MIB))
    available_mb = max(0, int(getattr(memory, "available", memory.total) // MIB))
    reserved_mb = max(1024, min(total_mb, max(2048, int(total_mb * 0.25))))
    safe_max_mb = max(1024, min(max(1024, total_mb - reserved_mb), max(1024, int(total_mb * 0.5))))
    recommended_max_mb = recommend_memory_max_mb(expected_players, safe_max_mb)
    return {
        "total_mb": total_mb,
        "available_mb": available_mb,
        "reserved_mb": reserved_mb,
        "recommended_max_mb": recommended_max_mb,
        "safe_max_mb": safe_max_mb,
    }


def recommend_memory_max_mb(expected_players: int, safe_max_mb: int) -> int:
    players = _clamp_int(expected_players, 1, 100)
    base_mb = 2048 if players <= 4 else 2048 + ((players - 4) * 320)
    rounded_mb = int(math.ceil(base_mb / 512) * 512)
    return _clamp_int(rounded_mb, 1024, max(1024, int(safe_max_mb)))


def build_setup_defaults() -> dict[str, Any]:
    host_memory = get_host_memory_summary(expected_players=10)
    defaults = {
        "profile_name": DEFAULT_PROFILE_NAME,
        "server_directory": "",
        "expected_players": 10,
        "memory_max_mb": host_memory["recommended_max_mb"],
        "use_aikar_flags": True,
        "eula_accepted": False,
        "minecraft_version": "",
        "paper_version": "",
        "paper_filename": DEFAULT_PAPER_FILENAME,
        "server_properties": _default_server_properties(),
    }
    return {
        "host_memory": host_memory,
        "defaults": defaults,
        "options": {
            "difficulties": list(DIFFICULTIES),
            "gamemodes": list(GAMEMODES),
        },
    }


def build_setup_preview(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise SetupValidationError({"payload": "Request body must be a JSON object."})

    defaults = build_setup_defaults()["defaults"]
    warnings: list[str] = []
    errors: dict[str, str] = {}

    profile_name = str(payload.get("profile_name", defaults["profile_name"]) or "").strip()
    if not profile_name:
        errors["profile_name"] = "Profile name is required."
    elif len(profile_name) > 64:
        errors["profile_name"] = "Profile name must be 64 characters or fewer."

    folder = _inspect_requested_folder(payload.get("server_directory", defaults["server_directory"]))
    errors.update(folder.pop("errors"))
    warnings.extend(folder.pop("warnings"))

    expected_players = _int_field(
        payload.get("expected_players", defaults["expected_players"]),
        field="expected_players",
        errors=errors,
    )
    expected_players = _clamp_with_warning(
        expected_players,
        1,
        100,
        "expected_players",
        warnings,
    )

    host_memory = get_host_memory_summary(expected_players=expected_players)
    memory_max_mb = _optional_int_field(payload.get("memory_max_mb"), field="memory_max_mb", errors=errors)
    if memory_max_mb is None:
        memory_max_mb = host_memory["recommended_max_mb"]
    else:
        memory_max_mb = _clamp_with_warning(
            memory_max_mb,
            1024,
            host_memory["safe_max_mb"],
            "memory_max_mb",
            warnings,
        )

    use_aikar_flags = _bool_field(
        payload.get("use_aikar_flags", defaults["use_aikar_flags"]),
        field="use_aikar_flags",
        errors=errors,
    )
    eula_accepted = _explicit_bool_field(
        payload.get("eula_accepted", defaults["eula_accepted"]),
        field="eula_accepted",
        errors=errors,
    )
    paper_filename = _safe_paper_filename(
        payload.get("paper_filename", defaults["paper_filename"]),
        errors,
    )
    properties = _normalize_server_properties(
        payload.get("server_properties", defaults["server_properties"]),
        warnings=warnings,
        errors=errors,
    )

    if errors:
        raise SetupValidationError(errors, warnings)

    start_script = build_start_script_preview(
        paper_filename=paper_filename,
        memory_min_mb=memory_max_mb,
        memory_max_mb=memory_max_mb,
        use_aikar_flags=use_aikar_flags,
    )
    properties_text, properties_entries = build_server_properties_preview(properties)
    install_plan = build_install_plan_preview(
        profile_name=profile_name,
        folder=folder,
        paper_filename=paper_filename,
        minecraft_version=str(payload.get("minecraft_version") or "").strip(),
        paper_version=str(payload.get("paper_version") or "").strip(),
        memory_max_mb=memory_max_mb,
        memory_min_mb=memory_max_mb,
        use_aikar_flags=use_aikar_flags,
    )
    creation_policy = build_creation_policy_preview(
        install_plan=install_plan,
        eula_accepted=eula_accepted,
    )

    return {
        "valid": True,
        "errors": {},
        "warnings": warnings,
        "profile": {
            "name": profile_name,
            "suggested_id": suggest_profile_id(profile_name),
            "server_directory": folder["path"],
        },
        "folder": folder,
        "memory": {
            "expected_players": expected_players,
            "recommended_max_mb": host_memory["recommended_max_mb"],
            "memory_max_mb": memory_max_mb,
            "memory_min_mb": memory_max_mb,
            "safe_max_mb": host_memory["safe_max_mb"],
            "use_aikar_flags": use_aikar_flags,
        },
        "paper": {
            "minecraft_version": str(payload.get("minecraft_version") or "").strip(),
            "paper_version": str(payload.get("paper_version") or "").strip(),
            "filename": paper_filename,
        },
        "creation_policy": creation_policy,
        "start_script": start_script,
        "server_properties": properties_text,
        "server_properties_entries": properties_entries,
        "install_plan": install_plan,
    }


def build_create_server_preflight(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return the future create-server contract without writing anything."""
    preview = build_setup_preview(payload)
    install_plan = preview["install_plan"]
    creation_policy = preview["creation_policy"]
    warnings = _combined_warnings(preview.get("warnings", []), install_plan.get("warnings", []))
    preflight = {
        "mode": "preflight_only",
        "version": 1,
        "ready_for_creation": bool(install_plan.get("ready_for_creation")),
        "ready_for_execution": bool(creation_policy.get("ready_for_execution")),
        "profile": install_plan["profile"],
        "target": install_plan["target"],
        "paper": install_plan["paper"],
        "memory": install_plan["memory"],
        "creation_policy": creation_policy,
        "planned_artifacts": install_plan["planned_artifacts"],
        "non_actions": install_plan["non_actions"],
        "warnings": warnings,
    }

    if not preflight["ready_for_creation"]:
        target = preflight["target"]
        if target.get("folder_exists") is True and target.get("target_writable") is False:
            raise SetupValidationError({"server_directory_permissions": "Target directory is not writable."}, warnings)
        raise SetupCreationNotReady(
            {"server_directory": _create_not_ready_reason(preview, warnings)},
            warnings,
            preflight=preflight,
            preview=preview,
        )

    if not preflight["ready_for_execution"]:
        raise SetupCreationNotReady(
            {"eula_accepted": "Accept the Minecraft EULA before creating a new server."},
            warnings,
            preflight=preflight,
            preview=preview,
        )

    return {
        "preflight": preflight,
        "preview": preview,
    }


def suggest_profile_id(profile_name: str) -> str:
    slug = SLUG_RE.sub("-", profile_name.strip().lower()).strip("-")
    return (slug or "server")[:48].strip("-") or "server"


def build_start_script_preview(
    *,
    paper_filename: str,
    memory_min_mb: int,
    memory_max_mb: int,
    use_aikar_flags: bool,
) -> str:
    flags = " ".join(AIKAR_FLAGS) if use_aikar_flags else ""
    parts = [
        "exec java",
        f"-Xms{int(memory_min_mb)}M",
        f"-Xmx{int(memory_max_mb)}M",
    ]
    if flags:
        parts.append(flags)
    parts.extend(["-jar", paper_filename, "--nogui"])
    return "#!/bin/sh\n" + " ".join(parts) + "\n"


def build_server_properties_preview(properties: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    entries = {
        "motd": properties["motd"],
        "max-players": properties["max_players"],
        "white-list": properties["white_list"],
        "enforce-whitelist": properties["white_list"],
        "difficulty": properties["difficulty"],
        "gamemode": properties["gamemode"],
        "view-distance": properties["view_distance"],
        "simulation-distance": properties["simulation_distance"],
        "online-mode": properties["online_mode"],
        "server-port": properties["server_port"],
        "enable-rcon": False,
    }
    lines = [f"{key}={_properties_value(value)}" for key, value in entries.items()]
    return "\n".join(lines) + "\n", entries


def build_install_plan_preview(
    *,
    profile_name: str,
    folder: Mapping[str, object],
    paper_filename: str,
    minecraft_version: str,
    paper_version: str,
    memory_min_mb: int,
    memory_max_mb: int,
    use_aikar_flags: bool,
) -> dict[str, object]:
    target_path = str(folder.get("path") or "")
    folder_exists = bool(folder.get("exists"))
    folder_empty = folder.get("empty")
    target_writable = folder.get("target_writable")
    profile_registration_ready = bool(folder.get("profile_registration_ready"))
    ready_for_creation = bool(
        target_path
        and bool(folder.get("parent_exists"))
        and (
            (not folder_exists and bool(folder.get("parent_writable")))
            or (folder_exists and folder_empty is True and target_writable is True)
        )
    )
    warnings: list[str] = []
    if profile_registration_ready:
        warnings.append("Existing server files were detected; choose a missing or empty folder for setup creation.")
    elif folder_exists and folder_empty is False:
        warnings.append("Choose a missing or empty folder for setup creation.")
    elif folder_exists and target_writable is False:
        warnings.append("Target directory is not writable.")

    paper_label = paper_filename
    if minecraft_version or paper_version:
        version_bits = " / ".join(bit for bit in (minecraft_version, paper_version) if bit)
        paper_label = f"{version_bits} / {paper_filename}"

    return {
        "mode": "plan_only",
        "version": 1,
        "ready_for_creation": ready_for_creation,
        "warnings": warnings,
        "profile": {
            "name": profile_name,
            "suggested_id": suggest_profile_id(profile_name),
        },
        "target": {
            "server_directory": target_path,
            "folder_exists": folder_exists,
            "folder_empty": folder_empty,
            "parent_writable": bool(folder.get("parent_writable")),
            "target_writable": target_writable,
        },
        "paper": {
            "label": paper_label,
            "minecraft_version": minecraft_version,
            "paper_version": paper_version,
            "filename": paper_filename,
        },
        "memory": {
            "memory_min_mb": int(memory_min_mb),
            "memory_max_mb": int(memory_max_mb),
            "use_aikar_flags": use_aikar_flags,
        },
        "planned_artifacts": [
            _planned_artifact(target_path, paper_filename, "Paper server jar", "selected Paper target"),
            _planned_artifact(target_path, "start.sh", "Start script", "start.sh preview"),
            _planned_artifact(target_path, "server.properties", "Server properties", "server.properties preview"),
        ],
        "non_actions": [
            "No folders are created.",
            "No files are written.",
            "No Paper jars are downloaded.",
            "No server profile is created.",
            "No profile is activated.",
            "No server process, backup, update, RCON, or folder removal command runs.",
        ],
    }


def build_creation_policy_preview(
    *,
    install_plan: Mapping[str, object],
    eula_accepted: bool,
) -> dict[str, object]:
    ready_for_creation = bool(install_plan.get("ready_for_creation"))
    return {
        "mode": "execution_contract",
        "version": 1,
        "ready_for_execution": bool(ready_for_creation and eula_accepted),
        "requires_eula_acceptance": True,
        "eula_accepted": bool(eula_accepted),
        "future_eula_txt": "eula=true" if eula_accepted else None,
        "write_scope": "target_directory_only",
        "allowed_artifacts": [
            "paper_jar",
            "start.sh",
            "server.properties",
            "eula.txt",
            "server_profile_metadata",
        ],
        "preserve_existing_files": True,
        "profile_metadata": {
            "create": True,
            "set_active": False,
            "operations_enabled": True,
            "rcon_enabled": False,
            "readonly": False,
        },
        "failure_cleanup": {
            "paper_download": "remove_only_files_created_by_this_attempt",
            "never_delete_existing_target_files": True,
        },
        "server_actions": {
            "start": False,
            "stop": False,
            "restart": False,
            "backup": False,
            "update": False,
            "rcon": False,
        },
    }


def _planned_artifact(target_path: str, filename: str, label: str, source: str) -> dict[str, str]:
    path = str(Path(target_path) / filename) if target_path else filename
    return {
        "label": label,
        "path": path,
        "source": source,
    }


def _combined_warnings(*groups: object) -> list[str]:
    warnings: list[str] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for warning in group:
            text = str(warning or "").strip()
            if text and text not in warnings:
                warnings.append(text)
    return warnings


def _create_not_ready_reason(preview: Mapping[str, Any], warnings: list[str]) -> str:
    folder = preview.get("folder")
    if isinstance(folder, Mapping) and bool(folder.get("profile_registration_ready")):
        return "Existing server files were detected; choose a missing or empty folder for setup creation."
    if warnings:
        return warnings[0]
    return "Choose a missing or empty server folder before creating a new server."


def _default_server_properties() -> dict[str, Any]:
    return {
        "motd": DEFAULT_MOTD,
        "max_players": 20,
        "white_list": True,
        "difficulty": "normal",
        "gamemode": "survival",
        "view_distance": 10,
        "simulation_distance": 8,
        "online_mode": True,
        "server_port": 25565,
    }


def _inspect_requested_folder(raw_path: Any) -> dict[str, Any]:
    errors: dict[str, str] = {}
    warnings: list[str] = []
    raw_text = str(raw_path or "").strip()
    if not raw_text:
        return {
            "path": "",
            "exists": False,
            "parent_exists": False,
            "parent_writable": False,
            "target_writable": None,
            "empty": None,
            "start_script_exists": False,
            "server_properties_exists": False,
            "profile_registration_ready": False,
            "errors": {"server_directory": "Server directory is required."},
            "warnings": warnings,
        }

    expanded = Path(raw_text).expanduser()
    if not expanded.is_absolute():
        return {
            "path": str(expanded),
            "exists": False,
            "parent_exists": False,
            "parent_writable": False,
            "target_writable": None,
            "empty": None,
            "start_script_exists": False,
            "server_properties_exists": False,
            "profile_registration_ready": False,
            "errors": {"server_directory": "Use an absolute path."},
            "warnings": warnings,
        }

    resolved = expanded.resolve(strict=False)
    exists = resolved.exists()
    is_dir = exists and resolved.is_dir()
    parent = resolved.parent
    parent_exists = parent.exists() and parent.is_dir()
    parent_writable = bool(parent_exists and os.access(parent, os.W_OK | os.X_OK))
    target_writable = None
    empty = None
    start_script_exists = False
    server_properties_exists = False

    if exists and not is_dir:
        errors["server_directory"] = "Path exists but is not a directory."
    if not parent_exists:
        errors["server_directory_parent"] = "Parent directory does not exist."
    elif not exists and not parent_writable:
        errors["server_directory_parent"] = "Parent directory is not writable."

    if is_dir:
        target_writable = bool(os.access(resolved, os.W_OK | os.X_OK))
        if not target_writable:
            warnings.append("Target directory is not writable.")
        try:
            empty = not any(resolved.iterdir())
        except OSError:
            empty = None
            warnings.append("Could not inspect whether the target directory is empty.")
        if empty is False:
            warnings.append("Target directory already contains files; preview will not modify them.")
        start_script_exists = (resolved / "start.sh").is_file()
        server_properties_exists = (resolved / "server.properties").is_file()

    return {
        "path": str(resolved),
        "exists": exists,
        "parent_exists": parent_exists,
        "parent_writable": parent_writable,
        "target_writable": target_writable,
        "empty": empty,
        "start_script_exists": start_script_exists,
        "server_properties_exists": server_properties_exists,
        "profile_registration_ready": bool(is_dir and start_script_exists and server_properties_exists),
        "errors": errors,
        "warnings": warnings,
    }


def _normalize_server_properties(
    value: Any,
    *,
    warnings: list[str],
    errors: dict[str, str],
) -> dict[str, Any]:
    defaults = _default_server_properties()
    raw = value if isinstance(value, dict) else {}
    properties = {
        "motd": _sanitize_text(str(raw.get("motd", defaults["motd"]) or ""), limit=80),
        "max_players": _clamp_with_warning(
            _int_field(raw.get("max_players", defaults["max_players"]), field="server_properties.max_players", errors=errors),
            1,
            200,
            "server_properties.max_players",
            warnings,
        ),
        "white_list": _bool_field(raw.get("white_list", defaults["white_list"]), field="server_properties.white_list", errors=errors),
        "difficulty": str(raw.get("difficulty", defaults["difficulty"]) or "").strip().lower(),
        "gamemode": str(raw.get("gamemode", defaults["gamemode"]) or "").strip().lower(),
        "view_distance": _clamp_with_warning(
            _int_field(raw.get("view_distance", defaults["view_distance"]), field="server_properties.view_distance", errors=errors),
            2,
            32,
            "server_properties.view_distance",
            warnings,
        ),
        "simulation_distance": _clamp_with_warning(
            _int_field(raw.get("simulation_distance", defaults["simulation_distance"]), field="server_properties.simulation_distance", errors=errors),
            2,
            32,
            "server_properties.simulation_distance",
            warnings,
        ),
        "online_mode": _bool_field(raw.get("online_mode", defaults["online_mode"]), field="server_properties.online_mode", errors=errors),
        "server_port": _clamp_with_warning(
            _int_field(raw.get("server_port", defaults["server_port"]), field="server_properties.server_port", errors=errors),
            1,
            65535,
            "server_properties.server_port",
            warnings,
        ),
    }
    if properties["difficulty"] not in DIFFICULTIES:
        errors["server_properties.difficulty"] = "Difficulty must be peaceful, easy, normal, or hard."
    if properties["gamemode"] not in GAMEMODES:
        errors["server_properties.gamemode"] = "Gamemode must be survival, creative, adventure, or spectator."
    return properties


def _safe_paper_filename(value: Any, errors: dict[str, str]) -> str:
    raw = str(value or "").strip()
    filename = Path(raw).name
    if (
        not raw
        or filename != raw
        or raw.startswith(".")
        or not SAFE_JAR_RE.fullmatch(raw)
    ):
        errors["paper_filename"] = "Paper filename must be a safe .jar basename."
        return DEFAULT_PAPER_FILENAME
    return raw


def _sanitize_text(value: str, *, limit: int) -> str:
    sanitized = CONTROL_CHARS_RE.sub(" ", value).strip()
    return sanitized[:limit]


def _int_field(value: Any, *, field: str, errors: dict[str, str]) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        errors[field] = "Value must be an integer."
        return 0


def _optional_int_field(value: Any, *, field: str, errors: dict[str, str]) -> int | None:
    if value is None or value == "":
        return None
    return _int_field(value, field=field, errors=errors)


def _bool_field(value: Any, *, field: str, errors: dict[str, str]) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    errors[field] = "Value must be a boolean."
    return False


def _explicit_bool_field(value: Any, *, field: str, errors: dict[str, str]) -> bool:
    if isinstance(value, bool):
        return value
    errors[field] = "Value must be true or false."
    return False


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _clamp_with_warning(
    value: int,
    minimum: int,
    maximum: int,
    field: str,
    warnings: list[str],
) -> int:
    clamped = _clamp_int(value, minimum, maximum)
    if clamped != value:
        warnings.append(f"{field} was clamped to {clamped}.")
    return clamped


def _properties_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
