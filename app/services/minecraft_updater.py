# app/services/minecraft_updater.py
"""
Minecraft Server Plugin Update Automation Service

Handles version checking, downloading, and updating for:
- Paper Server (PaperMC API)
- Modrinth Plugins (GrimAC, ViaVersion, Geyser, LuckPerms, etc.)
"""

import json
import hashlib
import platform
import shutil
import logging
import re
import secrets
import subprocess
import time
import uuid
import zipfile
from email.message import Message
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, asdict

import httpx
import yaml

from app.core import config
from app.services import minecraft_settings

# Legacy aliases for imports that expect these names. Runtime code below calls
# helper functions so changing the server directory in Settings takes effect.
MINECRAFT_SERVER_PATH = minecraft_settings.get_server_directory()
PLUGINS_PATH = minecraft_settings.get_plugins_dir()
BACKUPS_PATH = minecraft_settings.get_backups_dir()
UPDATE_LOGS_PATH = minecraft_settings.get_update_logs_dir()
VERSIONS_FILE = minecraft_settings.get_versions_file()


def _server_path() -> Path:
    return minecraft_settings.get_server_directory()


def _plugins_path() -> Path:
    return minecraft_settings.get_plugins_dir()


def _backups_path() -> Path:
    return minecraft_settings.get_backups_dir()


def _update_logs_path() -> Path:
    return minecraft_settings.get_update_logs_dir()


def _versions_file() -> Path:
    return minecraft_settings.get_versions_file()


def _profile_operation_block(action: str) -> dict[str, Any] | None:
    return minecraft_settings.get_active_profile_operation_block(action)


def _cora_credentials_dir() -> Path:
    return _server_path() / ".cora" / "credentials"


def _voxelshop_credentials_file() -> Path:
    return _cora_credentials_dir() / "voxelshop.json"


def _voxelshop_auth_states_file() -> Path:
    return _cora_credentials_dir() / "voxelshop_auth_states.json"


def _voxelshop_browser_downloads_file() -> Path:
    return _server_path() / ".cora" / "voxelshop_browser_downloads.json"


def _upgrade_manifests_path() -> Path:
    return UPGRADE_MANIFESTS_DIR


def _upgrade_executions_path() -> Path:
    return UPGRADE_EXECUTIONS_DIR


def _upgrade_staging_path(execution_id: str) -> Path:
    return UPGRADE_STAGING_DIR / execution_id


def _upgrade_rollbacks_path(execution_id: str) -> Path:
    return UPGRADE_ROLLBACKS_DIR / execution_id

# API Endpoints
PAPERMC_API_V3 = "https://fill.papermc.io/v3"
PAPERMC_DATA = "https://fill-data.papermc.io/v1"
MODRINTH_API = "https://api.modrinth.com/v2"
POLYMART_API = "https://api.polymart.org/v1"
GITHUB_API = "https://api.github.com"
ESSENTIALSX_REPO = "EssentialsX/Essentials"
ESSENTIALSX_JENKINS_JOB = "https://ci.ender.zone/job/EssentialsX"
ESSENTIALSX_CHANNELS = {"stable", "dev", "auto"}

# HTTP client settings
TIMEOUT = 30.0
USER_AGENT = "CORA-MinecraftUpdater/1.0 (example.local)"
PAPER_TARGET_LIMIT = 20
UPGRADE_MANIFESTS_DIR = config.DATA_DIR / "minecraft_upgrade_manifests"
UPGRADE_EXECUTIONS_DIR = config.DATA_DIR / "minecraft_upgrade_executions"
UPGRADE_STAGING_DIR = config.DATA_DIR / "minecraft_upgrade_staging"
UPGRADE_ROLLBACKS_DIR = config.DATA_DIR / "minecraft_upgrade_rollbacks"
MANIFEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class VersionInfo:
    """Represents version information for a plugin"""
    version: str
    build: Optional[int] = None
    download_url: Optional[str] = None
    filename: Optional[str] = None
    sha256: Optional[str] = None
    sha512: Optional[str] = None
    changelog: Optional[str] = None
    game_versions: Optional[list] = None
    full_version: Optional[str] = None


@dataclass
class UpdateCheck:
    """Result of checking for updates"""
    plugin_id: str
    source: str
    current_version: str
    latest_version: str
    has_update: bool
    download_url: Optional[str] = None
    filename: Optional[str] = None
    sha256: Optional[str] = None
    sha512: Optional[str] = None
    changelog: Optional[str] = None
    current_full_version: Optional[str] = None
    latest_full_version: Optional[str] = None


@dataclass
class OperationLog:
    """Structured operation log entry"""
    timestamp: str
    plugin: str
    operation: str
    from_version: Optional[str] = None
    to_version: Optional[str] = None
    steps: list = None
    status: str = "pending"
    error: Optional[str] = None

    def __post_init__(self):
        if self.steps is None:
            self.steps = []

    def add_step(self, action: str, **details):
        self.steps.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": action,
            **details
        })

    def save(self):
        """Save log to file"""
        update_logs_path = _update_logs_path()
        update_logs_path.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}_{self.plugin}_{self.operation}.json"
        filepath = update_logs_path / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

        return filepath


def _mark_log_blocked_by_profile(log: OperationLog, block: dict[str, Any]) -> OperationLog:
    log.status = "failed"
    log.error = str(block.get("error") or "Active server profile does not allow this operation")
    log.add_step(
        "profile_guard_blocked",
        error_code=block.get("error_code"),
        profile_id=block.get("profile_id"),
        profile_name=block.get("profile_name"),
    )
    log.save()
    return log


def load_versions(*, apply_migrations: bool = True) -> dict:
    """Load current version tracking data and apply safe metadata migrations."""
    versions_file = _versions_file()
    if versions_file.exists():
        with open(versions_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not apply_migrations:
            return data

        # Auto-migration: Add full_version to existing entries
        plugins = data.get("plugins", {})
        migrated = False
        for plugin_id, plugin_config in plugins.items():
            if "full_version" not in plugin_config and "file" in plugin_config:
                # Extract full_version from filename
                filename = plugin_config["file"]
                full_ver = extract_version_from_filename(filename)
                if full_ver:
                    plugin_config["full_version"] = full_ver
                    migrated = True

        if sync_detected_plugin_versions(data):
            migrated = True

        # Save if we migrated any entries
        if migrated:
            save_versions(data)

        return data

    return {"plugins": {}, "pending_updates": []}


def save_versions(data: dict):
    """Save version tracking data"""
    versions_file = _versions_file()
    versions_file.parent.mkdir(parents=True, exist_ok=True)
    with open(versions_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _top_level_yaml_value(text: str, key: str) -> Optional[str]:
    """Read a simple top-level YAML scalar without requiring a YAML dependency."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None

    value = match.group(1).split("#", 1)[0].strip()
    if not value:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value or None


def read_plugin_metadata_version(jar_path: Path) -> Optional[str]:
    """Return version from plugin.yml or paper-plugin.yml inside a plugin JAR."""
    try:
        with zipfile.ZipFile(jar_path) as archive:
            for metadata_file in ("paper-plugin.yml", "plugin.yml"):
                try:
                    raw = archive.read(metadata_file)
                except KeyError:
                    continue
                text = raw.decode("utf-8", errors="replace")
                version = _top_level_yaml_value(text, "version")
                if version:
                    return version
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def _read_plugin_metadata(jar_path: Path) -> dict[str, Any]:
    """Read safe display metadata from plugin.yml or paper-plugin.yml in a JAR."""
    try:
        with zipfile.ZipFile(jar_path) as archive:
            metadata_name = next(
                (
                    name
                    for name in ("paper-plugin.yml", "plugin.yml")
                    if name in archive.namelist()
                ),
                None,
            )
            if not metadata_name:
                metadata_name = next(
                    (name for name in archive.namelist() if name.lower().endswith("plugin.yml")),
                    None,
                )
            if not metadata_name:
                return {}

            raw = archive.read(metadata_name).decode("utf-8", errors="replace")
            data = yaml.safe_load(raw) or {}
            if not isinstance(data, dict):
                return {}

            return {
                "metadata_file": metadata_name,
                "name": str(data.get("name") or "").strip(),
                "version": str(data.get("version") or "").strip(),
                "description": str(data.get("description") or "").strip(),
                "website": str(data.get("website") or "").strip(),
                "main": str(data.get("main") or "").strip(),
                "api_version": str(data.get("api-version") or data.get("api_version") or "").strip(),
            }
    except (OSError, zipfile.BadZipFile, yaml.YAMLError):
        return {}


def _read_polymart_metadata(jar_path: Path) -> dict[str, Any]:
    """Read Polymart/Voxel metadata embedded in a downloaded JAR."""
    try:
        with zipfile.ZipFile(jar_path) as archive:
            if "polymart.yml" not in archive.namelist():
                return {}
            raw = archive.read("polymart.yml").decode("utf-8", errors="replace")
            data = yaml.safe_load(raw) or {}
            if not isinstance(data, dict):
                return {}
    except (OSError, zipfile.BadZipFile, yaml.YAMLError):
        return {}

    polymart = data.get("polymart") if isinstance(data.get("polymart"), dict) else {}
    product = polymart.get("product") if isinstance(polymart.get("product"), dict) else {}
    upload = polymart.get("upload") if isinstance(polymart.get("upload"), dict) else {}
    resource = polymart.get("resource") if isinstance(polymart.get("resource"), dict) else {}

    product_id = product.get("id") or resource.get("id") or polymart.get("resource_id")
    upload_id = upload.get("id") or polymart.get("upload_id")
    return {
        "product_id": str(product_id).strip() if product_id not in (None, "") else None,
        "product_title": str(product.get("title") or "").strip() or None,
        "product_url": str(product.get("url") or "").strip() or None,
        "upload_id": str(upload_id).strip() if upload_id not in (None, "") else None,
        "upload_version": str(upload.get("version") or "").strip() or None,
        "upload_type": str(upload.get("type") or "").strip() or None,
    }


def _file_hashes(path: Path) -> dict[str, str]:
    sha1 = hashlib.sha1()
    sha512 = hashlib.sha512()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha1.update(chunk)
            sha512.update(chunk)
    return {
        "sha1": sha1.hexdigest(),
        "sha512": sha512.hexdigest(),
    }


def _slugify_plugin_id(value: str, *, fallback: str = "plugin") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or fallback


def _safe_plugin_jar_name(filename: str) -> Optional[str]:
    safe_name = Path(str(filename or "")).name
    if safe_name != filename or not safe_name.lower().endswith(".jar"):
        return None
    return safe_name


def scan_plugin_registry_candidates(*, include_hashes: bool = True) -> dict[str, Any]:
    """Return tracked plugins plus untracked local plugin JAR candidates."""
    versions_data = load_versions(apply_migrations=False)
    tracked_plugins = versions_data.get("plugins", {})
    if not isinstance(tracked_plugins, dict):
        tracked_plugins = {}

    tracked_files = {
        str(config.get("file") or "")
        for config in tracked_plugins.values()
        if isinstance(config, dict) and config.get("file")
    }

    plugins_path = _plugins_path()
    untracked = []
    if plugins_path.exists():
        for jar_path in sorted(plugins_path.glob("*.jar"), key=lambda path: path.name.lower()):
            metadata = _read_plugin_metadata(jar_path)
            detected_version = (
                metadata.get("version")
                or extract_version_from_filename(jar_path.name)
                or "Unknown"
            )
            candidate = {
                "filename": jar_path.name,
                "tracked": jar_path.name in tracked_files,
                "size": jar_path.stat().st_size,
                "modified": datetime.fromtimestamp(jar_path.stat().st_mtime).isoformat(),
                "detected_plugin_id": _slugify_plugin_id(metadata.get("name") or jar_path.stem),
                "metadata": metadata,
                "detected_version": detected_version,
            }
            if include_hashes:
                hashes = _file_hashes(jar_path)
                candidate["sha1"] = hashes["sha1"]
                candidate["sha512"] = hashes["sha512"]
            untracked.append(candidate)

    return {
        "minecraft_version": versions_data.get("minecraft_version", "1.21.11"),
        "plugins_dir": str(plugins_path),
        "tracked": tracked_plugins,
        "untracked": [item for item in untracked if not item["tracked"]],
        "all_local_jars": untracked,
    }


def _safe_detection_glob(raw_glob: Any) -> Optional[str]:
    glob = str(raw_glob or "").strip()
    if not glob:
        return None
    if Path(glob).is_absolute() or "/" in glob or "\\" in glob:
        return None
    return glob


def _version_from_filename_pattern(filename: str, pattern: str) -> Optional[str]:
    try:
        match = re.fullmatch(pattern, filename)
    except re.error:
        return None
    if not match:
        return None

    if "version" in match.groupdict():
        return match.group("version")
    if match.groups():
        return match.group(1)
    return None


def _detected_version_from_jar(jar_path: Path, detection: dict[str, Any]) -> Optional[str]:
    mode = str(detection.get("mode") or "").strip().lower()
    if mode == "metadata":
        return read_plugin_metadata_version(jar_path)
    if mode == "filename":
        pattern = str(detection.get("pattern") or "").strip()
        if pattern:
            return _version_from_filename_pattern(jar_path.name, pattern)
        return extract_version_from_filename(jar_path.name)
    return None


def sync_detected_plugin_versions(data: dict) -> bool:
    """
    Sync tracked plugin versions from local JARs only when explicitly enabled.

    A plugin opts in with:
      "version_detection": {
        "auto_sync": true,
        "mode": "filename" | "metadata",
        "glob": "Plugin-*.jar",
        "pattern": "^Plugin-(?P<version>\\d+\\.\\d+\\.\\d+)\\.jar$"
      }

    Sync is intentionally conservative: exactly one matching JAR is required.
    """
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        return False

    plugins_path = _plugins_path()
    if not plugins_path.exists():
        return False

    changed = False
    detected_at = datetime.now().replace(microsecond=0).isoformat()

    for plugin_id, plugin_config in plugins.items():
        if not isinstance(plugin_config, dict):
            continue

        detection = plugin_config.get("version_detection")
        if not isinstance(detection, dict) or not detection.get("auto_sync"):
            continue

        glob = _safe_detection_glob(detection.get("glob"))
        if not glob:
            continue

        candidates = sorted(
            (
                candidate
                for candidate in plugins_path.glob(glob)
                if candidate.is_file() and candidate.suffix.lower() == ".jar"
            ),
            key=lambda path: path.name.lower(),
        )
        if len(candidates) != 1:
            continue

        jar_path = candidates[0]
        detected_version = _detected_version_from_jar(jar_path, detection)
        if not detected_version:
            continue

        current_file = plugin_config.get("file")
        current_version = plugin_config.get("current_version")
        current_full_version = plugin_config.get("full_version")
        if (
            current_file == jar_path.name
            and current_version == detected_version
            and current_full_version == detected_version
        ):
            continue

        plugin_config["file"] = jar_path.name
        plugin_config["current_version"] = detected_version
        plugin_config["full_version"] = detected_version
        plugin_config["detected_at"] = detected_at
        plugin_config["version_source"] = f"{detection.get('mode')}:{plugin_id}"
        plugin_config.pop("sha256", None)
        plugin_config.pop("sha512", None)
        changed = True

    return changed


async def get_papermc_latest(minecraft_version: str = "1.21.11") -> VersionInfo:
    """
    Fetch latest Paper build from PaperMC v3 API (Fill system)

    API: GET /v3/projects/paper/versions/{version}/builds
    Note: v3 API returns builds sorted newest-first (index 0 = latest)
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Get list of builds for version (v3 API)
        url = f"{PAPERMC_API_V3}/projects/paper/versions/{minecraft_version}/builds"
        response = await client.get(url, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()

        builds = response.json()  # v3 returns array directly, not {"builds": [...]}

        if not builds:
            raise ValueError(f"No builds found for Paper {minecraft_version}")

        # v3 API: Latest build is FIRST in array (index 0)
        latest = builds[0]
        build_number = latest["id"]  # v3 uses "id" instead of "build"

        # Get download info (v3 structure)
        downloads = latest.get("downloads", {})
        server_download = downloads.get("server:default", {})
        filename = server_download.get("name", f"paper-{minecraft_version}-{build_number}.jar")
        checksums = server_download.get("checksums", {})
        sha256 = checksums.get("sha256")

        # v3 uses fill-data.papermc.io for downloads
        download_url = server_download.get("url")
        if not download_url:
            # Fallback: construct URL from sha256
            download_url = f"{PAPERMC_DATA}/objects/{sha256}/{filename}"

        # Get changelog from commits
        commits = latest.get("commits", [])
        changelog = "\n".join([f"- {c.get('message', '').strip()}" for c in commits[:5]]) if commits else None

        return VersionInfo(
            version=f"{minecraft_version}-{build_number}",
            build=build_number,
            download_url=download_url,
            filename=filename,
            sha256=sha256,
            changelog=changelog,
            game_versions=[minecraft_version]
        )


def _paper_target_dict(target: VersionInfo) -> dict[str, Any]:
    minecraft_version = (
        target.game_versions[0]
        if target.game_versions
        else target.version.rsplit("-", 1)[0]
    )
    return {
        "minecraft_version": minecraft_version,
        "paper_version": target.version,
        "build": target.build,
        "filename": target.filename,
        "download_url": target.download_url,
        "sha256": target.sha256,
        "changelog": target.changelog,
        "channel": "STABLE",
    }


def _flatten_paper_versions(project_payload: dict[str, Any]) -> list[str]:
    versions_by_family = project_payload.get("versions", {})
    if not isinstance(versions_by_family, dict):
        return []

    versions: list[str] = []
    for family_versions in versions_by_family.values():
        if not isinstance(family_versions, list):
            continue
        for version in family_versions:
            if isinstance(version, str) and version not in versions:
                versions.append(version)
    return versions


async def _fetch_paper_stable_target(
    client: httpx.AsyncClient,
    minecraft_version: str,
) -> Optional[VersionInfo]:
    url = f"{PAPERMC_API_V3}/projects/paper/versions/{minecraft_version}/builds"
    response = await client.get(url, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    builds = response.json()

    for build in builds:
        if build.get("channel") != "STABLE":
            continue

        server_download = build.get("downloads", {}).get("server:default", {})
        if not server_download:
            continue

        build_number = build["id"]
        filename = server_download.get("name", f"paper-{minecraft_version}-{build_number}.jar")
        checksums = server_download.get("checksums", {})
        download_url = server_download.get("url")
        sha256 = checksums.get("sha256")
        if not download_url and sha256:
            download_url = f"{PAPERMC_DATA}/objects/{sha256}/{filename}"

        if not download_url:
            continue

        commits = build.get("commits", [])
        changelog = "\n".join(
            [f"- {c.get('message', '').strip()}" for c in commits[:5]]
        ) if commits else None

        return VersionInfo(
            version=f"{minecraft_version}-{build_number}",
            build=build_number,
            download_url=download_url,
            filename=filename,
            sha256=sha256,
            changelog=changelog,
            game_versions=[minecraft_version],
        )

    return None


async def get_paper_stable_target(minecraft_version: str) -> VersionInfo:
    """Fetch the newest STABLE Paper build for a specific target version."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        target = await _fetch_paper_stable_target(client, minecraft_version)

    if not target:
        raise ValueError(f"No stable Paper build found for {minecraft_version}")
    return target


async def get_paper_upgrade_targets(limit: int = PAPER_TARGET_LIMIT) -> dict[str, Any]:
    """Return recent Paper versions that have a stable server build."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{PAPERMC_API_V3}/projects/paper",
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        versions = _flatten_paper_versions(response.json())

        targets: list[dict[str, Any]] = []
        for version in versions:
            try:
                target = await _fetch_paper_stable_target(client, version)
            except Exception as exc:
                logging.warning("Failed to inspect Paper target %s: %s", version, exc)
                continue

            if target:
                targets.append(_paper_target_dict(target))
                if len(targets) >= limit:
                    break

    if not targets:
        raise ValueError("No stable Paper targets found")

    return {
        "latest": targets[0],
        "targets": targets,
    }


async def get_modrinth_latest(
    project_id: str,
    minecraft_version: str = "1.21.11",
    loader: str = "paper",
    release_only: bool = True
) -> VersionInfo:
    """
    Fetch latest STABLE version from Modrinth API

    API: GET /v2/project/{id}/version

    Args:
        project_id: Modrinth project slug
        minecraft_version: Target MC version
        loader: Server loader (paper, bukkit, spigot, folia)
        release_only: If True, prefer stable releases but accept beta if no release exists

    Strategy:
    1. For each loader (paper, bukkit, spigot), find releases first, then betas
    2. Prefer release > beta for each loader before moving to next loader
    3. This ensures we get correct loader file even if only betas exist
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        url = f"{MODRINTH_API}/project/{project_id}/version"

        # Loaders to try in order of preference for Paper servers
        loaders_to_try = ["paper", "bukkit", "spigot", "folia"] if loader == "paper" else [loader]

        best_version = None

        for try_loader in loaders_to_try:
            # Try with game version filter first
            params = {
                "game_versions": f'["{minecraft_version}"]',
                "loaders": f'["{try_loader}"]'
            }

            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            versions = response.json()

            # If no results with game version, try loader only
            if not versions:
                params = {"loaders": f'["{try_loader}"]'}
                response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
                response.raise_for_status()
                versions = response.json()

            if not versions:
                continue

            # Prefer release, but accept beta for this loader
            releases = [v for v in versions if v.get("version_type") == "release"]
            betas = [v for v in versions if v.get("version_type") in ("beta", "alpha")]

            if releases:
                best_version = releases[0]
                break  # Found a release for this loader, use it
            elif betas and not best_version:
                # No release, but found beta - remember it but keep trying other loaders for releases
                best_version = betas[0]
                # Don't break - see if another loader has a release

        # If we found nothing, try without any loader filter as last resort
        if not best_version:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            versions = response.json()

            if versions:
                releases = [v for v in versions if v.get("version_type") == "release"]
                if releases:
                    best_version = releases[0]
                else:
                    best_version = versions[0]
                    print(f"[Warning] No stable releases found for {project_id}, using latest available")

        if not best_version:
            raise ValueError(f"No versions found for {project_id}")

        # Get primary file
        files = best_version.get("files", [])
        primary_file = next((f for f in files if f.get("primary")), files[0] if files else None)

        if not primary_file:
            raise ValueError(f"No download file found for {project_id}")

        hashes = primary_file.get("hashes", {})

        # Extract full version from filename (includes commit hash if present)
        filename = primary_file.get("filename")
        full_version = extract_version_from_filename(filename) if filename else None

        return VersionInfo(
            version=best_version.get("version_number"),
            download_url=primary_file.get("url"),
            filename=filename,
            sha256=hashes.get("sha256"),
            sha512=hashes.get("sha512"),
            changelog=best_version.get("changelog"),
            game_versions=best_version.get("game_versions", []),
            full_version=full_version
        )


def _github_digest_hashes(asset: dict[str, Any]) -> dict[str, Optional[str]]:
    digest = str(asset.get("digest") or "").strip()
    if ":" not in digest:
        return {"sha256": None, "sha512": None}
    algorithm, value = digest.split(":", 1)
    algorithm = algorithm.lower().strip()
    value = value.strip()
    if algorithm == "sha256":
        return {"sha256": value, "sha512": None}
    if algorithm == "sha512":
        return {"sha256": None, "sha512": value}
    return {"sha256": None, "sha512": None}


def _essentialsx_channel(raw_channel: Any) -> str:
    channel = str(raw_channel or "auto").strip().lower()
    if channel not in ESSENTIALSX_CHANNELS:
        raise ValueError("EssentialsX channel must be stable, dev, or auto")
    return channel


def _essentialsx_module(plugin_id: str, plugin_config: dict[str, Any]) -> str:
    configured = str(plugin_config.get("module") or plugin_config.get("project_id") or "").strip()
    if configured and configured.lower().startswith("essentialsx"):
        return configured

    filename = str(plugin_config.get("file") or "")
    match = re.match(r"^(EssentialsX[A-Za-z]*)-", filename)
    if match:
        return match.group(1)

    if str(plugin_id or "").lower().startswith("essentialsx"):
        suffix = str(plugin_id)[len("essentialsx"):]
        return "EssentialsX" + suffix if suffix else "EssentialsX"

    return "EssentialsX"


async def get_essentialsx_stable_latest(
    minecraft_version: str,
    *,
    module: str = "EssentialsX",
) -> VersionInfo:
    """Fetch the latest compatible EssentialsX stable artifact from GitHub Releases."""
    target = await get_modrinth_target_release("essentialsx", minecraft_version)
    if not target:
        raise ValueError(f"No EssentialsX stable release found for Minecraft {minecraft_version}")

    compatible_version = str(target.get("version") or "").lstrip("v")
    if not compatible_version:
        raise ValueError(f"No EssentialsX stable version found for Minecraft {minecraft_version}")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{GITHUB_API}/repos/{ESSENTIALSX_REPO}/releases/tags/{compatible_version}",
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        release = response.json()

    expected_name = f"{module}-{compatible_version}.jar"
    assets = release.get("assets", []) if isinstance(release, dict) else []
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == expected_name
        ),
        None,
    )
    if not asset:
        raise ValueError(f"EssentialsX stable artifact not found: {expected_name}")

    hashes = _github_digest_hashes(asset)
    return VersionInfo(
        version=compatible_version,
        download_url=asset.get("browser_download_url"),
        filename=expected_name,
        sha256=hashes["sha256"],
        sha512=hashes["sha512"],
        changelog=release.get("body"),
        game_versions=target.get("game_versions", []),
        full_version=compatible_version,
    )


async def get_essentialsx_dev_latest(
    minecraft_version: str,
    *,
    module: str = "EssentialsX",
    fallback_reason: Optional[str] = None,
) -> VersionInfo:
    """Fetch the latest successful EssentialsX dev artifact from Jenkins."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{ESSENTIALSX_JENKINS_JOB}/lastSuccessfulBuild/api/json",
            params={
                "tree": "number,displayName,timestamp,result,url,artifacts[fileName,relativePath]",
            },
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        build = response.json()

    artifacts = build.get("artifacts", []) if isinstance(build, dict) else []
    artifact = next(
        (
            item
            for item in artifacts
            if isinstance(item, dict)
            and item.get("fileName", "").startswith(f"{module}-")
            and item.get("fileName", "").endswith(".jar")
        ),
        None,
    )
    if not artifact:
        raise ValueError(f"EssentialsX dev artifact not found: {module}")

    filename = artifact.get("fileName")
    relative_path = artifact.get("relativePath")
    build_url = str(build.get("url") or f"{ESSENTIALSX_JENKINS_JOB}/lastSuccessfulBuild/").rstrip("/") + "/"
    version = extract_version_from_filename(filename) or str(build.get("displayName") or build.get("number") or "dev")
    reason = fallback_reason or "Using latest EssentialsX Jenkins development build."
    return VersionInfo(
        version=version,
        download_url=f"{build_url}artifact/{relative_path}",
        filename=filename,
        changelog=(
            f"{reason}\n"
            f"Build: {build.get('displayName') or build.get('number')}\n"
            f"Compatibility note: stable release metadata did not prove a better match for Minecraft {minecraft_version}."
        ),
        game_versions=[minecraft_version],
        full_version=version,
    )


async def get_essentialsx_latest(
    minecraft_version: str,
    *,
    channel: str = "auto",
    module: str = "EssentialsX",
) -> VersionInfo:
    """Resolve EssentialsX according to stable/dev/auto channel policy."""
    resolved_channel = _essentialsx_channel(channel)
    if resolved_channel == "stable":
        return await get_essentialsx_stable_latest(minecraft_version, module=module)
    if resolved_channel == "dev":
        return await get_essentialsx_dev_latest(minecraft_version, module=module)

    try:
        return await get_essentialsx_stable_latest(minecraft_version, module=module)
    except Exception as exc:
        return await get_essentialsx_dev_latest(
            minecraft_version,
            module=module,
            fallback_reason=f"Stable EssentialsX is unavailable for Minecraft {minecraft_version}: {exc}",
        )


async def search_modrinth_plugin_projects(
    query: str,
    minecraft_version: Optional[str] = None,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search Modrinth plugin projects for operator-confirmed matching."""
    query = str(query or "").strip()
    if not query:
        return []

    safe_limit = max(1, min(int(limit or 10), 20))
    facets: list[list[str]] = [["project_type:plugin"]]
    if minecraft_version:
        facets.append([f"versions:{minecraft_version}"])

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{MODRINTH_API}/search",
            params={
                "query": query,
                "limit": safe_limit,
                "facets": json.dumps(facets),
            },
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()

    hits = payload.get("hits", []) if isinstance(payload, dict) else []
    results = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        results.append({
            "project_id": hit.get("project_id"),
            "slug": hit.get("slug"),
            "title": hit.get("title"),
            "description": hit.get("description"),
            "author": hit.get("author"),
            "icon_url": hit.get("icon_url"),
            "downloads": hit.get("downloads"),
            "follows": hit.get("follows"),
            "latest_version": hit.get("latest_version"),
            "client_side": hit.get("client_side"),
            "server_side": hit.get("server_side"),
            "categories": hit.get("categories", []),
            "versions": hit.get("versions", []),
            "date_modified": hit.get("date_modified"),
            "project_type": hit.get("project_type"),
        })
    return results


def _polymart_false(value: Any) -> bool:
    return value is False or str(value).strip().lower() in {"false", "0", "no"}


def _polymart_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Invalid Voxel.shop API response")

    response = payload.get("response", payload)
    if not isinstance(response, dict):
        raise ValueError("Invalid Voxel.shop API response")
    if _polymart_false(response.get("success")):
        errors = response.get("errors")
        error = response.get("error") or response.get("message") or response.get("reason")
        if not error and isinstance(errors, dict):
            error = errors.get("global")
        if not error and isinstance(errors, list) and errors:
            error = "; ".join(str(item) for item in errors)
        if not error:
            error = "Voxel.shop API request failed"
        raise ValueError(str(error))
    return response


def _polymart_supported_versions(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _voxelshop_resource_id(resource_id: Any) -> str:
    resolved = str(resource_id or "").strip()
    if not resolved:
        raise ValueError("resource_id is required for Voxel.shop tracking")
    if not re.fullmatch(r"\d+", resolved):
        raise ValueError("Voxel.shop resource_id must be numeric")
    return resolved


async def get_voxelshop_latest(
    resource_id: str,
    minecraft_version: Optional[str] = None,
) -> VersionInfo:
    """Fetch latest public Voxel.shop/Polymart update metadata.

    Premium downloads require an auth token, so this intentionally returns
    metadata only for now. Operators can still track paid plugins safely and
    see update availability without automatic file replacement.
    """
    resource_id = _voxelshop_resource_id(resource_id)
    headers = {"User-Agent": USER_AGENT}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        info_response = await client.get(
            f"{POLYMART_API}/getResourceInfo",
            params={"resource_id": resource_id, "stringify": "0"},
            headers=headers,
        )
        info_response.raise_for_status()
        info_payload = _polymart_response(info_response.json())

        updates_response = await client.get(
            f"{POLYMART_API}/getResourceUpdates",
            params={"resource_id": resource_id, "limit": 10, "stringify": "0"},
            headers=headers,
        )
        updates_response.raise_for_status()
        updates_payload = _polymart_response(updates_response.json())

    resource = info_payload.get("resource") if isinstance(info_payload.get("resource"), dict) else {}
    updates = updates_payload.get("updates") if isinstance(updates_payload.get("updates"), list) else []
    release_updates = [
        update
        for update in updates
        if isinstance(update, dict)
        and update.get("version")
        and not update.get("snapshot")
        and not update.get("beta")
    ]

    latest_update = release_updates[0] if release_updates else None
    if latest_update is None:
        info_latest = resource.get("updates", {}).get("latest") if isinstance(resource.get("updates"), dict) else None
        if isinstance(info_latest, dict) and info_latest.get("version"):
            latest_update = info_latest

    if latest_update is None:
        raise ValueError(f"No Voxel.shop update metadata found for resource {resource_id}")

    changelog = latest_update.get("description") or latest_update.get("title")
    supported_versions = _polymart_supported_versions(resource.get("supportedMinecraftVersions"))
    if minecraft_version and supported_versions and minecraft_version not in supported_versions:
        # Polymart exposes resource-level compatibility, not strict per-file
        # metadata. Keep the latest visible, but make the mismatch obvious.
        changelog_note = f"Voxel.shop lists supported Minecraft versions: {', '.join(supported_versions)}."
        changelog = f"{changelog_note}\n\n{changelog or ''}".strip()

    return VersionInfo(
        version=str(latest_update.get("version")),
        download_url=None,
        filename=None,
        changelog=changelog,
        game_versions=supported_versions,
        full_version=str(latest_update.get("version")),
    )


async def search_voxelshop_plugin_projects(
    query: str,
    minecraft_version: Optional[str] = None,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search Voxel.shop resources for explicit operator matching."""
    query = str(query or "").strip()
    if not query:
        return []

    safe_limit = max(1, min(int(limit or 10), 20))
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{POLYMART_API}/search",
            params={"query": query, "limit": safe_limit, "stringify": "0"},
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        payload = _polymart_response(response.json())

    raw_results = payload.get("result", []) if isinstance(payload, dict) else []
    results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        versions = _polymart_supported_versions(item.get("supportedMinecraftVersions"))
        if minecraft_version and versions and minecraft_version not in versions:
            # Keep paid plugin search broad because Voxel.shop sometimes uses
            # version families such as 1.21 instead of exact Paper versions.
            pass
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        results.append({
            "resource_id": str(item.get("id")) if item.get("id") is not None else None,
            "title": item.get("title"),
            "description": item.get("subtitle"),
            "author": owner.get("name"),
            "icon_url": item.get("thumbnailURL"),
            "downloads": item.get("downloads"),
            "price": item.get("price"),
            "currency": item.get("currency"),
            "can_download": bool(item.get("canDownload")),
            "url": item.get("url"),
            "versions": versions,
            "server_software": item.get("supportedServerSoftware"),
            "date_modified": item.get("lastUpdateTime"),
            "project_type": "plugin",
        })
    return results


def _mask_secret(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


def _load_voxelshop_credentials() -> dict[str, Any]:
    path = _voxelshop_credentials_file()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _ensure_voxelshop_credentials_dir() -> Path:
    credentials_dir = _cora_credentials_dir()
    credentials_dir.mkdir(parents=True, exist_ok=True)
    try:
        (_server_path() / ".cora").chmod(0o700)
        credentials_dir.chmod(0o700)
    except OSError:
        pass
    return credentials_dir


def _save_voxelshop_credentials(payload: dict[str, Any]) -> None:
    _ensure_voxelshop_credentials_dir()
    path = _voxelshop_credentials_file()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    temp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load_voxelshop_auth_states() -> dict[str, Any]:
    path = _voxelshop_auth_states_file()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_voxelshop_auth_states(states: dict[str, Any]) -> None:
    _ensure_voxelshop_credentials_dir()
    path = _voxelshop_auth_states_file()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(states, handle, indent=2, ensure_ascii=False)
    temp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load_voxelshop_browser_downloads() -> dict[str, Any]:
    path = _voxelshop_browser_downloads_file()
    if not path.exists():
        return {"download_directory": "", "sessions": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"download_directory": "", "sessions": {}}
    if not isinstance(payload, dict):
        return {"download_directory": "", "sessions": {}}
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        payload["sessions"] = {}
    payload.setdefault("download_directory", "")
    return payload


def _save_voxelshop_browser_downloads(payload: dict[str, Any]) -> None:
    _ensure_voxelshop_credentials_dir()
    path = _voxelshop_browser_downloads_file()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    temp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _resolve_existing_directory(raw_path: Any) -> Path:
    raw_value = str(raw_path or "").strip()
    if not raw_value:
        raise ValueError("Download folder is required")
    expanded = Path(raw_value).expanduser()
    if not expanded.is_absolute():
        raise ValueError("Use an absolute download folder path")
    if not expanded.exists():
        raise FileNotFoundError(f"Download folder does not exist: {expanded}")
    if not expanded.is_dir():
        raise ValueError(f"Download folder is not a directory: {expanded}")
    return expanded


def _session_age_seconds(session: dict[str, Any]) -> int:
    try:
        started = datetime.fromisoformat(str(session.get("started_at") or ""))
        return max(0, int((datetime.now() - started).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _active_voxelshop_browser_sessions(payload: dict[str, Any]) -> dict[str, Any]:
    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), dict) else {}
    return {
        session_id: session
        for session_id, session in sessions.items()
        if isinstance(session, dict)
        and session.get("status") in {"waiting", "ready"}
        and _session_age_seconds(session) < 3600
    }


def get_voxelshop_browser_download_config() -> dict[str, Any]:
    payload = _load_voxelshop_browser_downloads()
    download_directory = str(payload.get("download_directory") or "").strip()
    exists = False
    if download_directory:
        path = Path(download_directory).expanduser()
        exists = path.exists() and path.is_dir()
    return {
        "download_directory": download_directory,
        "download_directory_exists": exists,
        "active_sessions": len(_active_voxelshop_browser_sessions(payload)),
    }


def set_voxelshop_browser_download_directory(raw_path: str, *, actor: str = "") -> dict[str, Any]:
    directory = _resolve_existing_directory(raw_path)
    payload = _load_voxelshop_browser_downloads()
    payload["download_directory"] = str(directory)
    payload["updated_at"] = _now_iso()
    payload["updated_by"] = actor or None
    payload["sessions"] = _active_voxelshop_browser_sessions(payload)
    _save_voxelshop_browser_downloads(payload)
    return get_voxelshop_browser_download_config()


def _native_folder_picker_applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _choose_directory_with_native_dialog(prompt: str) -> str:
    system = platform.system()
    if system == "Darwin":
        script = (
            "POSIX path of (choose folder with prompt "
            f"{_native_folder_picker_applescript_string(prompt)})"
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if result.returncode == 1 and "-128" in (result.stderr or ""):
            raise ValueError("Folder selection was cancelled.")
        raise RuntimeError((result.stderr or "macOS folder picker failed.").strip())

    if system == "Windows":
        escaped_prompt = prompt.replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '{escaped_prompt}'
$dialog.ShowNewFolderButton = $false
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
    Write-Output $dialog.SelectedPath
}} else {{
    Write-Output '__CANCELLED__'
}}
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "Windows folder picker failed.").strip())
        selected = result.stdout.strip()
        if not selected or selected == "__CANCELLED__":
            raise ValueError("Folder selection was cancelled.")
        return selected

    for command in (
        ["zenity", "--file-selection", "--directory", "--title", prompt],
        ["kdialog", "--getexistingdirectory", str(Path.home())],
    ):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except FileNotFoundError:
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode in {1, 2}:
            raise ValueError("Folder selection was cancelled.")
        raise RuntimeError((result.stderr or "Linux folder picker failed.").strip())

    raise RuntimeError(
        "Native folder picker is not available on this server. Enter the folder path manually."
    )


def choose_voxelshop_browser_download_directory(*, actor: str = "") -> dict[str, Any]:
    selected = _choose_directory_with_native_dialog(
        "Choose the folder where your browser saves Voxel.shop plugin JARs"
    )
    return set_voxelshop_browser_download_directory(selected, actor=actor)


def _voxelshop_state_age_seconds(state_payload: dict[str, Any]) -> Optional[int]:
    try:
        return int(time.time()) - int(state_payload.get("created_at", 0) or 0)
    except (TypeError, ValueError):
        return None


def get_voxelshop_credentials_status() -> dict[str, Any]:
    payload = _load_voxelshop_credentials()
    token = str(payload.get("token") or "").strip()
    return {
        "connected": bool(token),
        "masked_token": _mask_secret(token) if token else None,
        "verified_at": payload.get("verified_at"),
        "expires_at": payload.get("expires_at"),
        "updated_at": payload.get("updated_at"),
        "updated_by": payload.get("updated_by"),
        "user": payload.get("user") if isinstance(payload.get("user"), dict) else None,
    }


async def verify_voxelshop_auth_token(token: str) -> dict[str, Any]:
    token = str(token or "").strip()
    if not token:
        raise ValueError("Voxel.shop token is required")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{POLYMART_API}/verifyAuthToken",
            data={"token": token, "stringify": "0"},
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        payload = _polymart_response(response.json())
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    if _polymart_false(result.get("success")):
        raise ValueError(str(result.get("message") or "Voxel.shop token verification failed"))
    return payload


async def connect_voxelshop_token(token: str, *, actor: str = "") -> dict[str, Any]:
    verification = await verify_voxelshop_auth_token(token)
    now = _now_iso()
    result = verification.get("result") if isinstance(verification.get("result"), dict) else {}
    expires = result.get("expires")
    expires_at = None
    if expires not in (None, ""):
        try:
            expires_at = datetime.fromtimestamp(int(expires)).replace(microsecond=0).isoformat()
        except (TypeError, ValueError, OSError):
            expires_at = str(expires)
    payload = {
        "token": str(token or "").strip(),
        "verified_at": now,
        "expires_at": expires_at,
        "updated_at": now,
        "updated_by": actor or None,
        "user": result.get("user") if isinstance(result.get("user"), dict) else None,
        "verify_message": result.get("message"),
    }
    _save_voxelshop_credentials(payload)
    return get_voxelshop_credentials_status()


async def disconnect_voxelshop_token() -> dict[str, Any]:
    try:
        _voxelshop_credentials_file().unlink()
    except FileNotFoundError:
        pass

    return get_voxelshop_credentials_status()


async def create_voxelshop_authorization_url(
    *,
    public_base_url: str,
    actor: str = "",
) -> dict[str, Any]:
    from urllib.parse import urlparse

    parsed = urlparse(str(public_base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Public admin URL must start with http:// or https://")

    origin = f"{parsed.scheme}://{parsed.netloc}"
    service = parsed.netloc
    return_url = f"{origin}/minecraft/admin/api/minecraft/plugin-registry/voxelshop-auth/callback"
    state = secrets.token_urlsafe(24)

    states = _load_voxelshop_auth_states()
    now = int(time.time())
    states = {
        key: value
        for key, value in states.items()
        if isinstance(value, dict) and (_voxelshop_state_age_seconds(value) or 1800) < 1800
    }
    states[state] = {
        "created_at": now,
        "created_by": actor or None,
        "service": service,
        "return_url": return_url,
    }
    _save_voxelshop_auth_states(states)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{POLYMART_API}/authorizeUser",
            data={
                "service": service,
                "return_url": return_url,
                "state": state,
                "stringify": "0",
            },
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        payload = _polymart_response(response.json())

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    authorize_url = str(result.get("url") or "").strip()
    if not authorize_url:
        raise ValueError("Voxel.shop did not return an authorization URL")
    return {
        "authorize_url": authorize_url,
        "state": state,
        "return_url": return_url,
        "service": service,
    }


async def complete_voxelshop_authorization(
    *,
    success: Any,
    token: str,
    state: str,
) -> dict[str, Any]:
    state = str(state or "").strip()
    states = _load_voxelshop_auth_states()
    state_payload = states.pop(state, None)
    _save_voxelshop_auth_states(states)

    if not state_payload:
        raise ValueError("Voxel.shop authorization state is invalid or expired")
    state_age = _voxelshop_state_age_seconds(state_payload)
    if state_age is None or state_age >= 1800:
        raise ValueError("Voxel.shop authorization state has expired")
    if _polymart_false(success):
        raise ValueError("Voxel.shop authorization was denied")

    return await connect_voxelshop_token(
        token,
        actor=state_payload.get("created_by") or "voxelshop-callback",
    )


async def get_voxelshop_download_url(resource_id: str) -> dict[str, Any]:
    credentials = _load_voxelshop_credentials()
    token = str(credentials.get("token") or "").strip()
    if not token:
        raise ValueError("Voxel.shop token is not connected")

    resource_id = _voxelshop_resource_id(resource_id)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{POLYMART_API}/getDownloadURL",
            data={
                "resource_id": resource_id,
                "token": token,
                "allow_redirects": "0",
                "stringify": "0",
            },
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        payload = _polymart_response(response.json())

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    url = str(result.get("url") or "").strip()
    if not url:
        raise ValueError("Voxel.shop did not return a download URL")
    return {
        "url": url,
        "version": str(result.get("version") or "").strip() or None,
        "expires": result.get("expires"),
    }


async def get_voxelshop_resource_summary(resource_id: str) -> dict[str, Any]:
    resource_id = _voxelshop_resource_id(resource_id)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{POLYMART_API}/getResourceInfo",
            params={"resource_id": resource_id, "stringify": "0"},
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        payload = _polymart_response(response.json())

    resource = payload.get("resource") if isinstance(payload.get("resource"), dict) else {}
    return {
        "resource_id": resource_id,
        "title": resource.get("title"),
        "url": resource.get("url") or f"https://voxel.shop/product/{resource_id}",
        "thumbnail_url": resource.get("thumbnailURL"),
    }


def _normalized_plugin_match_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _validate_browser_download_candidate(
    jar_path: Path,
    *,
    plugin_id: str,
    plugin_config: dict[str, Any],
    expected_resource_id: str,
    expected_version: str,
) -> dict[str, Any]:
    metadata = _read_plugin_metadata(jar_path)
    polymart = _read_polymart_metadata(jar_path)
    metadata_version = str(metadata.get("version") or "").strip()
    marketplace_version = str(polymart.get("upload_version") or "").strip()
    filename_version = extract_version_from_filename(jar_path.name) or ""
    current_name = (
        (plugin_config.get("local_metadata") or {}).get("name")
        if isinstance(plugin_config.get("local_metadata"), dict)
        else None
    )
    expected_names = {
        _normalized_plugin_match_value(plugin_id),
        _normalized_plugin_match_value(current_name),
        _normalized_plugin_match_value(Path(str(plugin_config.get("file") or "")).stem),
    }
    expected_names.discard("")
    detected_name = _normalized_plugin_match_value(metadata.get("name") or jar_path.stem)
    product_id = str(polymart.get("product_id") or "").strip()
    product_matches = product_id == expected_resource_id
    product_conflicts = bool(product_id and not product_matches)
    name_matches = bool(detected_name and detected_name in expected_names)
    marketplace_version_matches = (
        not expected_version
        or not marketplace_version
        or normalize_version(marketplace_version) == normalize_version(expected_version)
    )
    marketplace_matches = bool(product_matches and marketplace_version and marketplace_version_matches)
    detected_version = (
        marketplace_version
        if marketplace_matches
        else (metadata_version or marketplace_version or filename_version)
    )
    if marketplace_matches:
        validation_source = "marketplace"
    elif metadata_version:
        validation_source = "plugin_metadata"
    elif marketplace_version:
        validation_source = "marketplace"
    else:
        validation_source = "filename"
    version_matches = (
        not expected_version
        or not detected_version
        or normalize_version(detected_version) == normalize_version(expected_version)
    )
    accepted = bool(
        marketplace_matches
        or (not product_conflicts and (product_matches or name_matches) and version_matches)
    )
    reasons = []
    if product_conflicts:
        reasons.append(
            f"Voxel.shop product id {product_id} does not match expected {expected_resource_id}"
        )
    elif not product_matches and not name_matches:
        reasons.append("JAR metadata does not match the tracked Voxel.shop resource or plugin name")
    if not marketplace_matches and not version_matches:
        reasons.append(f"Detected version {detected_version or 'unknown'} does not match expected {expected_version}")

    version_mismatch = None
    if (
        metadata_version
        and marketplace_version
        and normalize_version(metadata_version) != normalize_version(marketplace_version)
    ):
        version_mismatch = {
            "plugin_metadata_version": metadata_version,
            "marketplace_version": marketplace_version,
            "message": "plugin.yml version differs from Voxel.shop upload version",
        }

    return {
        "filename": jar_path.name,
        "path": str(jar_path),
        "size": jar_path.stat().st_size,
        "modified": datetime.fromtimestamp(jar_path.stat().st_mtime).isoformat(),
        "metadata": metadata,
        "polymart": polymart,
        "detected_version": detected_version or None,
        "validation_source": validation_source,
        "version_mismatch": version_mismatch,
        "accepted": accepted,
        "reason": "; ".join(reasons) if reasons else None,
    }


def _scan_voxelshop_browser_download_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    directory = _resolve_existing_directory(session.get("download_directory"))
    plugin_id = str(session.get("plugin_id") or "").strip()
    expected_resource_id = _voxelshop_resource_id(session.get("resource_id"))
    expected_version = str(session.get("expected_version") or "").strip()
    versions_data = load_versions()
    plugin_config = versions_data.get("plugins", {}).get(plugin_id)
    if not isinstance(plugin_config, dict):
        raise KeyError(f"Plugin not found: {plugin_id}")

    try:
        started = datetime.fromisoformat(str(session.get("started_at") or ""))
        earliest_mtime = started.timestamp() - 30
    except (TypeError, ValueError):
        earliest_mtime = time.time() - 3600

    candidates = []
    for jar_path in sorted(directory.glob("*.jar"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            if jar_path.stat().st_mtime < earliest_mtime:
                continue
            candidates.append(_validate_browser_download_candidate(
                jar_path,
                plugin_id=plugin_id,
                plugin_config=plugin_config,
                expected_resource_id=expected_resource_id,
                expected_version=expected_version,
            ))
        except (OSError, ValueError):
            continue
    return candidates


def _save_voxelshop_browser_session(session_id: str, session: dict[str, Any]) -> None:
    payload = _load_voxelshop_browser_downloads()
    sessions = _active_voxelshop_browser_sessions(payload)
    sessions[session_id] = session
    payload["sessions"] = sessions
    _save_voxelshop_browser_downloads(payload)


async def start_voxelshop_browser_download_session(plugin_id: str, *, actor: str = "") -> dict[str, Any]:
    payload = _load_voxelshop_browser_downloads()
    directory = str(payload.get("download_directory") or "").strip()
    if not directory:
        return {
            "status": "folder_required",
            "message": "Choose a browser download folder before starting Voxel.shop assisted updates.",
            "config": get_voxelshop_browser_download_config(),
        }
    try:
        download_directory = _resolve_existing_directory(directory)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "status": "folder_required",
            "message": str(exc),
            "config": get_voxelshop_browser_download_config(),
        }

    versions_data = load_versions()
    plugin_config = versions_data.get("plugins", {}).get(plugin_id)
    if not isinstance(plugin_config, dict):
        raise KeyError(f"Plugin not found: {plugin_id}")
    if str(plugin_config.get("source") or "").lower() != "voxelshop":
        raise ValueError("Browser-assisted download is only available for Voxel.shop plugins")

    minecraft_version = versions_data.get("minecraft_version", "1.21.11")
    update = await check_plugin_update(plugin_id, plugin_config, minecraft_version)
    if not update.has_update:
        return {
            "status": "no_update",
            "message": f"{plugin_id} is already up to date.",
            "update": asdict(update),
        }

    resource_id = _voxelshop_resource_id(plugin_config.get("project_id"))
    resource = await get_voxelshop_resource_summary(resource_id)
    session_id = secrets.token_urlsafe(18)
    now = _now_iso()
    session = {
        "id": session_id,
        "status": "waiting",
        "plugin_id": plugin_id,
        "resource_id": resource_id,
        "product_url": resource["url"],
        "resource_title": resource.get("title"),
        "download_directory": str(download_directory),
        "expected_version": update.latest_version,
        "expected_full_version": update.latest_full_version,
        "current_version": update.current_version,
        "started_at": now,
        "updated_at": now,
        "created_by": actor or None,
    }
    _save_voxelshop_browser_session(session_id, session)
    return {
        "status": "waiting",
        "session": get_voxelshop_browser_download_session(session_id)["session"],
        "config": get_voxelshop_browser_download_config(),
    }


def get_voxelshop_browser_download_session(session_id: str) -> dict[str, Any]:
    payload = _load_voxelshop_browser_downloads()
    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), dict) else {}
    session = sessions.get(str(session_id or ""))
    if not isinstance(session, dict):
        raise KeyError("Voxel.shop browser download session not found")

    candidates = _scan_voxelshop_browser_download_session(session)
    accepted = [candidate for candidate in candidates if candidate.get("accepted")]
    if accepted and session.get("status") == "waiting":
        session["status"] = "ready"
        session["updated_at"] = _now_iso()
        _save_voxelshop_browser_session(str(session_id), session)

    return {
        "status": session.get("status") or "waiting",
        "session": {**session, "candidates": candidates, "ready_candidate": accepted[0] if accepted else None},
    }


async def apply_voxelshop_browser_download_session(session_id: str) -> OperationLog:
    status = get_voxelshop_browser_download_session(session_id)
    session = status["session"]
    candidate = session.get("ready_candidate")
    if not candidate:
        raise ValueError("No matching downloaded JAR found yet")

    versions_data = load_versions()
    plugin_id = session["plugin_id"]
    plugin_config = versions_data.get("plugins", {}).get(plugin_id)
    if not isinstance(plugin_config, dict):
        raise KeyError(f"Plugin not found: {plugin_id}")

    minecraft_version = versions_data.get("minecraft_version", "1.21.11")
    update = await check_plugin_update(plugin_id, plugin_config, minecraft_version)
    update.filename = _safe_plugin_jar_name(candidate["filename"]) or _fallback_update_filename(update)
    marketplace_validated = candidate.get("validation_source") == "marketplace"
    if marketplace_validated:
        validated_version = str(candidate.get("detected_version") or session.get("expected_version") or "").strip()
        validated_full_version = str(
            session.get("expected_full_version")
            or candidate.get("detected_version")
            or validated_version
        ).strip()
        if validated_version:
            update.latest_version = validated_version
        if validated_full_version:
            update.latest_full_version = validated_full_version
    local_path = Path(candidate["path"])
    log = await apply_update_from_local_file(
        plugin_id,
        update,
        local_path,
        operation="voxelshop_browser_update",
        prefer_update_version=marketplace_validated,
    )

    payload = _load_voxelshop_browser_downloads()
    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), dict) else {}
    if str(session_id) in sessions:
        sessions[str(session_id)]["status"] = "applied" if log.status == "success" else "failed"
        sessions[str(session_id)]["updated_at"] = _now_iso()
        sessions[str(session_id)]["applied_log_status"] = log.status
        payload["sessions"] = sessions
        _save_voxelshop_browser_downloads(payload)
    return log


async def get_plugin_connection_status(plugin_id: str) -> dict[str, Any]:
    versions_data = load_versions()
    plugins = versions_data.get("plugins", {})
    plugin_config = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    if not isinstance(plugin_config, dict):
        raise KeyError(f"Plugin not found: {plugin_id}")

    minecraft_version = versions_data.get("minecraft_version", "1.21.11")
    source = str(plugin_config.get("source") or "manual").strip().lower()
    project_id = plugin_config.get("project_id", plugin_id)
    status: dict[str, Any] = {
        "plugin_id": plugin_id,
        "source": source,
        "project_id": project_id,
        "installed_version": plugin_config.get("current_version"),
        "installed_full_version": plugin_config.get("full_version"),
        "file": plugin_config.get("file"),
        "has_update": False,
        "latest_version": None,
        "latest_full_version": None,
        "download_ready": False,
        "download_status": "not_applicable",
    }

    if source == "voxelshop":
        credential_status = get_voxelshop_credentials_status()
        status["voxelshop"] = {
            "resource_id": project_id,
            "token_connected": credential_status["connected"],
            "masked_token": credential_status["masked_token"],
            "verified_at": credential_status["verified_at"],
            "expires_at": credential_status["expires_at"],
            "updated_at": credential_status["updated_at"],
            "updated_by": credential_status["updated_by"],
        }
        check = await check_plugin_update(plugin_id, plugin_config, minecraft_version)
        status.update({
            "has_update": check.has_update,
            "latest_version": check.latest_version,
            "latest_full_version": check.latest_full_version,
            "download_ready": bool(credential_status["connected"] and check.has_update),
            "download_status": "ready" if credential_status["connected"] else "token_required",
            "changelog": check.changelog,
        })
    elif source in {"modrinth", "papermc", "essentialsx"}:
        check = await check_plugin_update(plugin_id, plugin_config, minecraft_version)
        status.update({
            "has_update": check.has_update,
            "latest_version": check.latest_version,
            "latest_full_version": check.latest_full_version,
            "download_ready": bool(check.download_url),
            "download_status": "ready" if check.download_url else "metadata_only",
            "changelog": check.changelog,
        })
        if source == "essentialsx":
            status["essentialsx"] = {
                "channel": plugin_config.get("channel", "auto"),
                "module": _essentialsx_module(plugin_id, plugin_config),
            }

    return status


def add_tracked_plugin_from_local_jar(
    *,
    filename: str,
    source: str,
    plugin_id: Optional[str] = None,
    project_id: Optional[str] = None,
    loader: str = "paper",
    actor: str = "",
) -> dict[str, Any]:
    """Add an existing local JAR to versions.json without touching tracked entries."""
    safe_filename = _safe_plugin_jar_name(filename)
    if not safe_filename:
        raise ValueError("Invalid plugin JAR filename")

    source = str(source or "").strip().lower()
    if source not in {"modrinth", "voxelshop", "essentialsx", "manual"}:
        raise ValueError("source must be modrinth, voxelshop, essentialsx, or manual")
    if source == "modrinth" and not str(project_id or "").strip():
        raise ValueError("project_id is required for Modrinth tracking")
    if source == "voxelshop":
        project_id = _voxelshop_resource_id(project_id)
    if source == "essentialsx":
        project_id = str(project_id or plugin_id or "EssentialsX").strip() or "EssentialsX"

    jar_path = _plugins_path() / safe_filename
    if not jar_path.is_file():
        raise FileNotFoundError(f"Plugin JAR not found: {safe_filename}")

    versions_data = load_versions()
    plugins = versions_data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Invalid versions.json plugins payload")

    for existing_id, config in plugins.items():
        if isinstance(config, dict) and config.get("file") == safe_filename:
            raise ValueError(f"{safe_filename} is already tracked as {existing_id}")

    metadata = _read_plugin_metadata(jar_path)
    resolved_plugin_id = _slugify_plugin_id(
        plugin_id or project_id or metadata.get("name") or jar_path.stem
    )
    if resolved_plugin_id in plugins:
        raise ValueError(f"Plugin id already exists: {resolved_plugin_id}")

    detected_version = (
        metadata.get("version")
        or extract_version_from_filename(safe_filename)
        or "Unknown"
    )
    hashes = _file_hashes(jar_path)
    now = datetime.now().isoformat()

    entry = {
        "source": source,
        "project_id": str(project_id).strip() if source in {"modrinth", "voxelshop", "essentialsx"} else None,
        "current_version": detected_version,
        "full_version": detected_version,
        "file": safe_filename,
        "installed_at": now,
        "auto_update": False,
        "sha512": hashes["sha512"],
        "loader": loader or "paper",
        "tracking_added_at": now,
        "tracking_added_by": actor or None,
        "tracking_source": "local_jar",
    }
    if metadata:
        entry["local_metadata"] = metadata

    plugins[resolved_plugin_id] = entry
    save_versions(versions_data)
    return {
        "plugin_id": resolved_plugin_id,
        "entry": entry,
    }


def update_tracked_plugin_settings(
    plugin_id: str,
    *,
    source: Optional[str] = None,
    project_id: Optional[str] = None,
    loader: Optional[str] = None,
    auto_update: Optional[bool] = None,
    channel: Optional[str] = None,
    actor: str = "",
) -> dict[str, Any]:
    """Update editable tracking metadata without changing the local JAR binding."""
    resolved_plugin_id = str(plugin_id or "").strip()
    if not resolved_plugin_id:
        raise ValueError("plugin_id is required")

    versions_data = load_versions()
    plugins = versions_data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Invalid versions.json plugins payload")
    if resolved_plugin_id not in plugins or not isinstance(plugins[resolved_plugin_id], dict):
        raise KeyError(f"Plugin not found: {resolved_plugin_id}")

    plugin_config = plugins[resolved_plugin_id]
    current_source = str(plugin_config.get("source") or "manual").strip().lower()
    next_source = str(source or current_source).strip().lower()
    if next_source not in {"modrinth", "voxelshop", "essentialsx", "manual", "papermc"}:
        raise ValueError("source must be modrinth, voxelshop, essentialsx, manual, or papermc")
    if resolved_plugin_id == "paper" and next_source != "papermc":
        raise ValueError("paper must remain managed by PaperMC")
    if resolved_plugin_id != "paper" and next_source == "papermc":
        raise ValueError("Only paper can be managed by PaperMC")

    next_project_id = str(project_id).strip() if project_id is not None else plugin_config.get("project_id")
    if next_source == "modrinth" and not str(next_project_id or "").strip():
        raise ValueError("project_id is required for Modrinth tracking")
    if next_source == "voxelshop":
        next_project_id = _voxelshop_resource_id(next_project_id)
    if next_source == "essentialsx":
        next_project_id = str(next_project_id or _essentialsx_module(resolved_plugin_id, plugin_config)).strip()
        if not next_project_id.lower().startswith("essentialsx"):
            raise ValueError("EssentialsX project_id must be an EssentialsX module name")

    if next_source == "manual":
        next_project_id = None
    elif next_source == "papermc":
        next_project_id = next_project_id or resolved_plugin_id

    plugin_config["source"] = next_source
    plugin_config["project_id"] = next_project_id
    if loader is not None:
        plugin_config["loader"] = str(loader or "paper").strip() or "paper"
    if auto_update is not None:
        plugin_config["auto_update"] = bool(auto_update)
    if next_source == "essentialsx":
        plugin_config["channel"] = _essentialsx_channel(channel if channel is not None else plugin_config.get("channel"))
        plugin_config["module"] = next_project_id
    else:
        plugin_config.pop("channel", None)
        plugin_config.pop("module", None)
    plugin_config["tracking_updated_at"] = datetime.now().isoformat()
    plugin_config["tracking_updated_by"] = actor or None

    plugins[resolved_plugin_id] = plugin_config
    save_versions(versions_data)
    return {
        "plugin_id": resolved_plugin_id,
        "entry": plugin_config,
    }


async def install_modrinth_plugin_project(
    *,
    project_id: str,
    plugin_id: Optional[str] = None,
    loader: str = "paper",
    actor: str = "",
) -> dict[str, Any]:
    """Download a selected Modrinth plugin and add it to tracking."""
    project_id = str(project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")

    versions_data = load_versions()
    plugins = versions_data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Invalid versions.json plugins payload")

    resolved_plugin_id = _slugify_plugin_id(plugin_id or project_id)
    if resolved_plugin_id in plugins:
        raise ValueError(f"Plugin id already exists: {resolved_plugin_id}")

    minecraft_version = versions_data.get("minecraft_version", "1.21.11")
    latest = await get_modrinth_latest(project_id, minecraft_version, loader=loader or "paper")
    if not latest.filename or not latest.download_url:
        raise ValueError(f"No downloadable file found for {project_id}")

    safe_filename = _safe_plugin_jar_name(latest.filename)
    if not safe_filename:
        raise ValueError("Modrinth returned an unsafe filename")

    plugins_path = _plugins_path()
    plugins_path.mkdir(parents=True, exist_ok=True)
    dest_path = plugins_path / safe_filename
    if dest_path.exists():
        raise FileExistsError(f"{safe_filename} already exists in plugins/")

    for existing_id, config in plugins.items():
        if isinstance(config, dict) and config.get("file") == safe_filename:
            raise ValueError(f"{safe_filename} is already tracked as {existing_id}")

    update = UpdateCheck(
        plugin_id=resolved_plugin_id,
        source="modrinth",
        current_version="not installed",
        latest_version=latest.version,
        has_update=True,
        download_url=latest.download_url,
        filename=safe_filename,
        sha256=latest.sha256,
        sha512=latest.sha512,
        changelog=latest.changelog,
        latest_full_version=latest.full_version,
    )
    temp_path = await download_update(update)
    shutil.move(str(temp_path), str(dest_path))

    metadata = _read_plugin_metadata(dest_path)
    installed_version = metadata.get("version") or latest.full_version or latest.version
    now = datetime.now().isoformat()
    entry = {
        "source": "modrinth",
        "project_id": project_id,
        "current_version": latest.version,
        "full_version": installed_version,
        "file": safe_filename,
        "installed_at": now,
        "auto_update": False,
        "loader": loader or "paper",
        "game_versions": latest.game_versions or [],
        "tracking_added_at": now,
        "tracking_added_by": actor or None,
        "tracking_source": "modrinth_install",
    }
    if latest.sha256:
        entry["sha256"] = latest.sha256
    if latest.sha512:
        entry["sha512"] = latest.sha512
    if metadata:
        entry["local_metadata"] = metadata

    plugins[resolved_plugin_id] = entry
    save_versions(versions_data)
    return {
        "plugin_id": resolved_plugin_id,
        "entry": entry,
    }


async def get_modrinth_target_release(
    project_id: str,
    minecraft_version: str,
    loader: str = "paper",
) -> Optional[dict[str, Any]]:
    """Fetch a strict Modrinth release compatible with the target MC version."""
    loaders_to_try = ["paper", "bukkit", "spigot", "folia"] if loader == "paper" else [loader]
    url = f"{MODRINTH_API}/project/{project_id}/version"

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for try_loader in loaders_to_try:
            params = {
                "game_versions": f'["{minecraft_version}"]',
                "loaders": f'["{try_loader}"]',
            }
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            versions = response.json()

            releases = [
                v for v in versions
                if v.get("version_type") == "release"
                and minecraft_version in v.get("game_versions", [])
            ]

            for release in releases:
                files = release.get("files", [])
                primary_file = next((f for f in files if f.get("primary")), files[0] if files else None)
                if not primary_file:
                    continue

                hashes = primary_file.get("hashes", {})
                filename = primary_file.get("filename")
                return {
                    "version": release.get("version_number"),
                    "download_url": primary_file.get("url"),
                    "filename": filename,
                    "sha256": hashes.get("sha256"),
                    "sha512": hashes.get("sha512"),
                    "changelog": release.get("changelog"),
                    "game_versions": release.get("game_versions", []),
                    "loaders": release.get("loaders", []),
                    "loader": try_loader,
                    "full_version": extract_version_from_filename(filename) if filename else None,
                    "version_type": release.get("version_type"),
                }

    return None


def normalize_version(version_str: str) -> str:
    """
    Normalize version string by removing:
    - Git commit hashes (e.g., -b7a719d)
    - Build suffixes (e.g., -SNAPSHOT, -beta, -bukkit)
    - Leading 'v' prefix
    """
    import re

    v = version_str.strip()

    # Remove leading 'v' (e.g., v5.5.17 -> 5.5.17)
    if v.startswith('v'):
        v = v[1:]

    # Remove platform suffixes like -bukkit, -neoforge, -paper
    v = re.sub(r'-(bukkit|spigot|paper|neoforge|fabric|folia)$', '', v, flags=re.IGNORECASE)

    # Remove git commit hash suffix (e.g., -b7a719d, -abc1234)
    v = re.sub(r'-[a-f0-9]{6,}$', '', v, flags=re.IGNORECASE)

    # Remove SNAPSHOT/beta/alpha suffixes
    v = re.sub(r'-(SNAPSHOT|beta|alpha|rc\d*).*$', '', v, flags=re.IGNORECASE)

    return v


def parse_version_number(version_str: str) -> tuple:
    """Parse version string into comparable tuple of integers"""
    import re

    # Normalize first
    v = normalize_version(version_str)

    # Extract numeric parts (e.g., "2.3.73" -> (2, 3, 73))
    parts = re.findall(r'\d+', v)
    return tuple(int(p) for p in parts) if parts else (0,)


def extract_version_from_filename(filename: str) -> Optional[str]:
    """
    Extract full version string from JAR filename.

    Examples:
        grimac-bukkit-2.3.73-cd86c14.jar → 2.3.73-cd86c14
        ViaVersion-5.1.1.jar → 5.1.1
        paper-1.21.11-123.jar → 1.21.11-123

    Returns:
        Full version string with commit hash if present, or None if unable to parse
    """
    import re

    # Remove .jar extension
    name = filename.replace('.jar', '')

    # Pattern to match version strings with optional commit hash/release suffix.
    # Matches: X.Y.Z, X.Y.Z.W, X.Y.Z-hash, or X.Y.Z-text-hash
    pattern = r'(\d+(?:\.\d+){1,5}(?:-[a-zA-Z0-9]+(?:-[a-f0-9]{6,})?)?)$'

    match = re.search(pattern, name)
    if match:
        return match.group(1)

    # Fallback: try to find any version-like pattern
    pattern2 = r'(\d+(?:\.\d+){1,5}(?:-[a-zA-Z0-9\-]+)?)$'
    match = re.search(pattern2, name)
    if match:
        return match.group(1)

    return None


def _is_related_full_version(version: Optional[str], full_version: Optional[str]) -> bool:
    """Return True when a file/full version looks like detail for the main version."""
    version_n = normalize_version(str(version or ""))
    full_n = normalize_version(str(full_version or ""))
    if not version_n or not full_n:
        return False
    return full_n == version_n or full_n.startswith(f"{version_n}-") or version_n.startswith(f"{full_n}-")


def is_newer_version(
    current: str,
    latest: str,
    current_full: Optional[str] = None,
    latest_full: Optional[str] = None,
    current_filename: Optional[str] = None,
    latest_filename: Optional[str] = None
) -> bool:
    """
    Compare two version strings using three-tier comparison:
    1. Compare normalized versions (semantic: 2.3.72 < 2.3.73)
    2. If equal, compare full versions (commit hash: cd86c14 ≠ b7a719d → update available)
    3. If still equal, compare filenames (safest fallback)

    Returns True if latest is strictly newer than current.
    """
    try:
        # Tier 1: Normalize both versions for semantic comparison
        current_norm = normalize_version(current)
        latest_norm = normalize_version(latest)

        # If normalized versions differ, use semantic comparison
        if current_norm != latest_norm:
            current_parts = parse_version_number(current)
            latest_parts = parse_version_number(latest)

            # Pad shorter tuple with zeros for comparison
            max_len = max(len(current_parts), len(latest_parts))
            current_padded = current_parts + (0,) * (max_len - len(current_parts))
            latest_padded = latest_parts + (0,) * (max_len - len(latest_parts))

            return latest_padded > current_padded

        # Tier 2: Normalized versions equal, compare full versions (for commit hash changes)
        if current_full and latest_full:
            if current_full != latest_full:
                return True  # Full versions differ → update available

        # Tier 3: Full versions equal or not available, compare filenames
        if current_filename and latest_filename:
            if current_filename != latest_filename:
                return True  # Filenames differ → update available

        # Everything matches, no update
        return False

    except Exception:
        # Fallback: if parsing fails, compare normalized strings
        return normalize_version(latest) != normalize_version(current)


async def check_plugin_update(plugin_id: str, plugin_config: dict, minecraft_version: str) -> UpdateCheck:
    """Check if a specific plugin has an update available"""
    source = plugin_config.get("source")
    current_version = plugin_config.get("current_version", "0")
    current_full_version = plugin_config.get("full_version")
    current_filename = plugin_config.get("file")
    project_id = plugin_config.get("project_id", plugin_id)

    # Skip manual plugins (not trackable via API)
    if source == "manual":
        return UpdateCheck(
            plugin_id=plugin_id,
            source=source,
            current_version=current_version,
            latest_version="Manual update required",
            has_update=False,
            current_full_version=current_full_version
        )

    try:
        if source == "papermc":
            latest = await get_papermc_latest(minecraft_version)
        elif source == "modrinth":
            latest = await get_modrinth_latest(project_id, minecraft_version)
        elif source == "voxelshop":
            latest = await get_voxelshop_latest(project_id, minecraft_version)
        elif source == "essentialsx":
            latest = await get_essentialsx_latest(
                minecraft_version,
                channel=plugin_config.get("channel", "auto"),
                module=_essentialsx_module(plugin_id, plugin_config),
            )
        else:
            raise ValueError(f"Unknown source: {source}")

        # For Paper, compare build numbers
        if source == "papermc":
            current_build = plugin_config.get("current_build", 0)
            has_update = latest.build > current_build
        else:
            trusted_current_full = (
                current_full_version
                if _is_related_full_version(current_version, current_full_version)
                else None
            )
            trusted_latest_full = (
                latest.full_version
                if _is_related_full_version(latest.version, latest.full_version)
                else None
            )
            # Use enhanced version comparison with full version and filename
            has_update = is_newer_version(
                current_version,
                latest.version,
                current_full=trusted_current_full,
                latest_full=trusted_latest_full,
                current_filename=current_filename,
                latest_filename=latest.filename
            )

        return UpdateCheck(
            plugin_id=plugin_id,
            source=source,
            current_version=current_version,
            latest_version=latest.version,
            has_update=has_update,
            download_url=latest.download_url,
            filename=latest.filename,
            sha256=latest.sha256,
            sha512=latest.sha512,
            changelog=latest.changelog,
            current_full_version=current_full_version,
            latest_full_version=latest.full_version
        )

    except Exception as e:
        return UpdateCheck(
            plugin_id=plugin_id,
            source=source,
            current_version=current_version,
            latest_version=f"Error: {str(e)}",
            has_update=False,
            current_full_version=current_full_version
        )


async def check_all_updates(excluded_plugins: Optional[set[str] | list[str] | tuple[str, ...]] = None) -> list[UpdateCheck]:
    """Check all tracked plugins for updates"""
    versions_data = load_versions()
    minecraft_version = versions_data.get("minecraft_version", "1.21.11")
    plugins = versions_data.get("plugins", {})
    excluded = {str(plugin_id).strip().lower() for plugin_id in (excluded_plugins or []) if str(plugin_id).strip()}

    results = []
    for plugin_id, config in plugins.items():
        if str(plugin_id).strip().lower() in excluded:
            continue
        result = await check_plugin_update(plugin_id, config, minecraft_version)
        results.append(result)

    # Update last check time
    versions_data["last_check"] = datetime.now().isoformat()
    save_versions(versions_data)

    return results


async def check_plugin_target_compatibility(
    plugin_id: str,
    plugin_config: dict[str, Any],
    target_version: str,
) -> dict[str, Any]:
    """Check whether a tracked plugin has a strict release for target_version."""
    source = plugin_config.get("source")
    current_version = plugin_config.get("current_version", "0")
    current_full_version = plugin_config.get("full_version")
    current_filename = plugin_config.get("file")
    project_id = plugin_config.get("project_id", plugin_id)

    result: dict[str, Any] = {
        "plugin_id": plugin_id,
        "source": source,
        "project_id": project_id,
        "target_version": target_version,
        "current_version": current_version,
        "current_full_version": current_full_version,
        "current_filename": current_filename,
        "latest_version": None,
        "latest_full_version": None,
        "filename": None,
        "download_url": None,
        "sha256": None,
        "sha512": None,
        "loader": None,
        "loaders": [],
        "game_versions": [],
        "has_update": False,
        "blocking": True,
        "status": "blocker",
        "reason": "",
    }

    if source == "manual":
        result.update({
            "status": "manual",
            "reason": "Manual plugin; compatibility must be reviewed by an admin.",
        })
        return result

    if source == "essentialsx":
        channel = _essentialsx_channel(plugin_config.get("channel", "auto"))
        module = _essentialsx_module(plugin_id, plugin_config)
        try:
            latest = await get_essentialsx_latest(
                target_version,
                channel=channel,
                module=module,
            )
        except Exception as exc:
            result.update({
                "status": "blocker",
                "reason": str(exc),
            })
            return result

        latest_version = latest.version
        latest_full_version = latest.full_version
        latest_filename = latest.filename
        has_update = is_newer_version(
            current_version,
            latest_version or "0",
            current_full=current_full_version,
            latest_full=latest_full_version,
            current_filename=current_filename,
            latest_filename=latest_filename,
        )
        result.update({
            "status": "ready",
            "blocking": False,
            "reason": latest.changelog.splitlines()[0] if latest.changelog else "EssentialsX artifact available.",
            "latest_version": latest_version,
            "latest_full_version": latest_full_version,
            "filename": latest_filename,
            "download_url": latest.download_url,
            "sha256": latest.sha256,
            "sha512": latest.sha512,
            "loader": "paper",
            "loaders": ["paper", "bukkit", "spigot"],
            "game_versions": latest.game_versions or [target_version],
            "has_update": has_update,
            "channel": channel,
            "module": module,
        })
        return result

    if source != "modrinth":
        result.update({
            "status": "error",
            "reason": f"Unsupported source for preflight: {source}",
        })
        return result

    if not project_id:
        result.update({
            "status": "error",
            "reason": "Missing Modrinth project id.",
        })
        return result

    try:
        candidate = await get_modrinth_target_release(project_id, target_version)
    except Exception as exc:
        result.update({
            "status": "error",
            "reason": str(exc),
        })
        return result

    if not candidate:
        result.update({
            "status": "blocker",
            "reason": f"No Modrinth release found for {target_version}.",
        })
        return result

    latest_version = candidate.get("version")
    latest_full_version = candidate.get("full_version")
    latest_filename = candidate.get("filename")
    has_update = is_newer_version(
        current_version,
        latest_version or "0",
        current_full=current_full_version,
        latest_full=latest_full_version,
        current_filename=current_filename,
        latest_filename=latest_filename,
    )

    result.update({
        "status": "ready",
        "blocking": False,
        "reason": "Target release available.",
        "latest_version": latest_version,
        "latest_full_version": latest_full_version,
        "filename": latest_filename,
        "download_url": candidate.get("download_url"),
        "sha256": candidate.get("sha256"),
        "sha512": candidate.get("sha512"),
        "loader": candidate.get("loader"),
        "loaders": candidate.get("loaders", []),
        "game_versions": candidate.get("game_versions", []),
        "has_update": has_update,
    })
    return result


async def build_upgrade_preflight(target_version: Optional[str] = None) -> dict[str, Any]:
    """Build a read-only upgrade preflight report for a Paper target version."""
    versions_data = load_versions()
    current_minecraft_version = versions_data.get("minecraft_version", "unknown")
    plugins = versions_data.get("plugins", {})
    current_paper = plugins.get("paper", {})
    normalized_target = (target_version or "").strip()

    if not normalized_target:
        targets = await get_paper_upgrade_targets(limit=1)
        normalized_target = targets["latest"]["minecraft_version"]

    paper_target = await get_paper_stable_target(normalized_target)
    paper = _paper_target_dict(paper_target)
    paper.update({
        "status": "ready",
        "current_version": current_paper.get("current_version"),
        "current_build": current_paper.get("current_build"),
        "current_filename": current_paper.get("file"),
        "has_upgrade": paper.get("paper_version") != current_paper.get("current_version"),
    })

    plugin_results: list[dict[str, Any]] = []
    counts = {
        "ready": 0,
        "blocker": 0,
        "manual": 0,
        "error": 0,
    }

    for plugin_id, plugin_config in plugins.items():
        if plugin_id == "paper":
            continue

        result = await check_plugin_target_compatibility(
            plugin_id,
            plugin_config,
            normalized_target,
        )
        status = result.get("status", "error")
        if status not in counts:
            status = "error"
            result["status"] = status
        counts[status] += 1
        plugin_results.append(result)

    blocking_count = counts["blocker"] + counts["manual"] + counts["error"]

    return {
        "aggregate_status": "ready" if blocking_count == 0 else "blocked",
        "current_minecraft_version": current_minecraft_version,
        "target_version": normalized_target,
        "paper": paper,
        "results": plugin_results,
        "counts": {
            **counts,
            "blocking": blocking_count,
            "total": len(plugin_results),
        },
        "ready_count": counts["ready"],
        "blocker_count": counts["blocker"],
    }


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _generate_manifest_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _validate_manifest_id(manifest_id: str) -> str:
    normalized = str(manifest_id or "").strip()
    if not normalized or not MANIFEST_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Invalid manifest id")
    return normalized


def _manifest_file(manifest_id: str) -> Path:
    normalized = _validate_manifest_id(manifest_id)
    return _upgrade_manifests_path() / f"{normalized}.json"


def _ensure_manual_review_defaults(result: dict[str, Any]) -> None:
    if result.get("status") != "manual":
        return

    review = result.get("manual_review")
    if not isinstance(review, dict):
        review = {}

    review.setdefault("status", "pending")
    review.setdefault("note", None)
    review.setdefault("reviewed_at", None)
    review.setdefault("reviewed_by", None)
    result["manual_review"] = review
    result["manual_review_status"] = review.get("status", "pending")
    result["blocking"] = review.get("status") != "resolved"


def _apply_manifest_gate(manifest: dict[str, Any]) -> dict[str, Any]:
    results = manifest.get("results", [])
    if not isinstance(results, list):
        results = []
        manifest["results"] = results

    counts = {
        "ready": 0,
        "blocker": 0,
        "manual": 0,
        "error": 0,
    }
    hard_blocker_count = 0
    manual_pending_count = 0

    for result in results:
        if not isinstance(result, dict):
            continue

        status = result.get("status")
        if status not in counts:
            status = "error"
            result["status"] = status

        counts[status] += 1
        _ensure_manual_review_defaults(result)

        if status in {"blocker", "error"}:
            hard_blocker_count += 1
            result["blocking"] = True
        elif status == "manual":
            if result.get("manual_review", {}).get("status") != "resolved":
                manual_pending_count += 1
                result["blocking"] = True
            else:
                result["blocking"] = False
        else:
            result["blocking"] = False

    ready_for_execution = hard_blocker_count == 0 and manual_pending_count == 0
    manifest["counts"] = {
        **counts,
        "blocking": hard_blocker_count + manual_pending_count,
        "total": len(results),
    }
    manifest["ready_count"] = counts["ready"]
    manifest["blocker_count"] = counts["blocker"]
    manifest["hard_blocker_count"] = hard_blocker_count
    manifest["manual_pending_count"] = manual_pending_count
    manifest["ready_for_execution"] = ready_for_execution
    manifest["gate_status"] = "ready" if ready_for_execution else "blocked"
    manifest["aggregate_status"] = manifest["gate_status"]
    return manifest


def _save_upgrade_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = _apply_manifest_gate(manifest)
    manifests_path = _upgrade_manifests_path()
    manifests_path.mkdir(parents=True, exist_ok=True)
    manifest_id = _validate_manifest_id(manifest.get("manifest_id", ""))
    manifest_path = manifests_path / f"{manifest_id}.json"
    temp_path = manifest_path.with_suffix(".json.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    temp_path.replace(manifest_path)
    return manifest


def _build_upgrade_manifest(preflight: dict[str, Any]) -> dict[str, Any]:
    now = _now_iso()
    manifest = {
        "manifest_id": _generate_manifest_id(),
        "created_at": now,
        "updated_at": now,
        "current_minecraft_version": preflight.get("current_minecraft_version"),
        "target_version": preflight.get("target_version"),
        "paper": preflight.get("paper", {}),
        "results": preflight.get("results", []),
        "counts": preflight.get("counts", {}),
        "ready_count": preflight.get("ready_count", 0),
        "blocker_count": preflight.get("blocker_count", 0),
    }
    return _apply_manifest_gate(manifest)


async def create_upgrade_manifest(target_version: Optional[str] = None) -> dict[str, Any]:
    """Run upgrade preflight and persist a gated manifest for later execution."""
    preflight = await build_upgrade_preflight(target_version)
    manifest = _build_upgrade_manifest(preflight)
    return _save_upgrade_manifest(manifest)


def load_upgrade_manifest(manifest_id: str) -> dict[str, Any]:
    """Load an upgrade manifest and return it with current gate fields applied."""
    manifest_path = _manifest_file(manifest_id)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Upgrade manifest not found: {manifest_id}")

    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    if not isinstance(manifest, dict):
        raise ValueError("Invalid upgrade manifest")

    return _apply_manifest_gate(manifest)


def resolve_upgrade_manifest_manual_review(
    manifest_id: str,
    plugin_id: str,
    *,
    note: str = "",
    reviewed_by: str = "",
) -> dict[str, Any]:
    """Mark a manual plugin in a manifest as reviewed by an admin."""
    normalized_plugin_id = str(plugin_id or "").strip()
    if not normalized_plugin_id:
        raise ValueError("plugin_id is required")

    manifest = load_upgrade_manifest(manifest_id)
    results = manifest.get("results", [])
    target = next(
        (item for item in results if isinstance(item, dict) and item.get("plugin_id") == normalized_plugin_id),
        None,
    )

    if not target:
        raise KeyError(f"Plugin not found in manifest: {normalized_plugin_id}")
    if target.get("status") != "manual":
        raise ValueError("Only manual plugins can be marked as reviewed")

    target["manual_review"] = {
        "status": "resolved",
        "note": str(note or "").strip()[:500] or None,
        "reviewed_at": _now_iso(),
        "reviewed_by": str(reviewed_by or "").strip() or None,
    }
    target["manual_review_status"] = "resolved"
    target["blocking"] = False
    manifest["updated_at"] = _now_iso()
    return _save_upgrade_manifest(manifest)


def _generate_execution_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _validate_execution_id(execution_id: str) -> str:
    normalized = str(execution_id or "").strip()
    if not normalized or not MANIFEST_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Invalid execution id")
    return normalized


def _execution_file(execution_id: str) -> Path:
    normalized = _validate_execution_id(execution_id)
    return _upgrade_executions_path() / f"{normalized}.json"


def _safe_upgrade_filename(filename: Any) -> str:
    raw = str(filename or "").strip()
    safe = Path(raw).name
    if not raw or raw != safe or safe in {".", ".."} or not safe.endswith(".jar"):
        raise ValueError(f"Invalid upgrade artifact filename: {raw or '<missing>'}")
    return safe


def _safe_snapshot_name(prefix: str, filename: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
    return f"{prefix}__{safe_name}"


def _save_upgrade_execution(execution: dict[str, Any]) -> dict[str, Any]:
    execution["updated_at"] = _now_iso()
    executions_path = _upgrade_executions_path()
    executions_path.mkdir(parents=True, exist_ok=True)
    execution_id = _validate_execution_id(execution.get("execution_id", ""))
    execution_path = executions_path / f"{execution_id}.json"
    temp_path = execution_path.with_suffix(".json.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(execution, handle, indent=2, ensure_ascii=False)
    temp_path.replace(execution_path)
    return execution


def load_upgrade_execution(execution_id: str) -> dict[str, Any]:
    """Load a persisted Paper/Minecraft upgrade execution record."""
    execution_path = _execution_file(execution_id)
    if not execution_path.exists():
        raise FileNotFoundError(f"Upgrade execution not found: {execution_id}")

    with open(execution_path, "r", encoding="utf-8") as handle:
        execution = json.load(handle)

    if not isinstance(execution, dict):
        raise ValueError("Invalid upgrade execution")

    return execution


def _record_execution_step(
    execution: dict[str, Any],
    step: str,
    status: str,
    **details: Any,
) -> None:
    execution.setdefault("steps", []).append({
        "time": _now_iso(),
        "step": step,
        "status": status,
        **details,
    })


def _set_execution_status(execution: dict[str, Any], status: str) -> None:
    execution["status"] = status
    execution["success"] = status == "succeeded"


def _build_upgrade_execution(manifest: dict[str, Any], actor: str = "") -> dict[str, Any]:
    execution_id = _generate_execution_id()
    now = _now_iso()
    return {
        "execution_id": execution_id,
        "manifest_id": manifest.get("manifest_id"),
        "target_version": manifest.get("target_version"),
        "current_minecraft_version": manifest.get("current_minecraft_version"),
        "status": "pending",
        "success": False,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "actor": str(actor or "").strip() or None,
        "server_was_running": None,
        "steps": [],
        "staging_dir": str(_upgrade_staging_path(execution_id)),
        "rollback_dir": str(_upgrade_rollbacks_path(execution_id)),
        "staged_files": {},
        "rollback_files": {},
        "applied_files": {},
        "error": None,
        "rollback_error": None,
    }


def _execution_rejection(manifest_id: str, error: str, error_code: str) -> dict[str, Any]:
    return {
        "success": False,
        "status": "rejected",
        "manifest_id": manifest_id,
        "error": error,
        "error_code": error_code,
    }


def _ready_upgrade_plugins(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in manifest.get("results", [])
        if isinstance(item, dict) and item.get("status") == "ready"
    ]


def _paper_upgrade_artifact(manifest: dict[str, Any]) -> dict[str, Any]:
    paper = manifest.get("paper")
    if not isinstance(paper, dict):
        raise ValueError("Upgrade manifest is missing Paper target data")
    if paper.get("status") != "ready":
        raise ValueError("Paper target is not ready")
    if not paper.get("download_url"):
        raise ValueError("Paper target is missing a download URL")
    _safe_upgrade_filename(paper.get("filename"))
    return paper


def _validate_executable_manifest(
    manifest: dict[str, Any],
    versions_data: dict[str, Any],
) -> Optional[dict[str, Any]]:
    manifest_id = str(manifest.get("manifest_id") or "")
    if not manifest.get("ready_for_execution"):
        return _execution_rejection(
            manifest_id,
            "Upgrade manifest gate is blocked. Resolve manual items and rerun blockers first.",
            "gate_blocked",
        )

    current_version = versions_data.get("minecraft_version")
    manifest_current = manifest.get("current_minecraft_version")
    if current_version != manifest_current:
        return _execution_rejection(
            manifest_id,
            "Upgrade manifest is stale because the tracked Minecraft version changed. Run preflight again.",
            "stale_manifest",
        )

    try:
        _paper_upgrade_artifact(manifest)
        for item in _ready_upgrade_plugins(manifest):
            if not item.get("plugin_id"):
                raise ValueError("Ready plugin entry is missing plugin_id")
            if not item.get("download_url"):
                raise ValueError(f"{item.get('plugin_id')} is missing a download URL")
            _safe_upgrade_filename(item.get("filename"))
    except ValueError as exc:
        return _execution_rejection(manifest_id, str(exc), "invalid_manifest")

    return None


async def _download_upgrade_artifact(artifact: dict[str, Any], destination_dir: Path) -> Path:
    """Download and hash-check one manifest artifact into a staging directory."""
    filename = _safe_upgrade_filename(artifact.get("filename"))
    download_url = str(artifact.get("download_url") or "").strip()
    if not download_url:
        raise ValueError(f"{filename} is missing a download URL")

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    temp_path = destination.with_suffix(destination.suffix + ".download")

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(download_url, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()

    with open(temp_path, "wb") as handle:
        handle.write(response.content)

    if not verify_hash(temp_path, artifact.get("sha256"), artifact.get("sha512")):
        temp_path.unlink(missing_ok=True)
        raise ValueError(f"Hash verification failed for {filename}")

    temp_path.replace(destination)
    return destination


async def _stage_upgrade_artifacts(
    execution: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    stage_dir = _upgrade_staging_path(execution["execution_id"])
    paper = _paper_upgrade_artifact(manifest)
    paper_path = await _download_upgrade_artifact(paper, stage_dir)

    staged_files: dict[str, Any] = {
        "paper": {
            "path": str(paper_path),
            "filename": paper.get("filename"),
            "sha256": paper.get("sha256"),
        },
        "plugins": {},
    }

    plugin_stage_dir = stage_dir / "plugins"
    for item in _ready_upgrade_plugins(manifest):
        plugin_path = await _download_upgrade_artifact(item, plugin_stage_dir)
        staged_files["plugins"][item["plugin_id"]] = {
            "path": str(plugin_path),
            "filename": item.get("filename"),
            "sha256": item.get("sha256"),
            "sha512": item.get("sha512"),
        }

    execution["staged_files"] = staged_files


def _copy_required_snapshot(source: Path, destination: Path, label: str) -> dict[str, str]:
    if not source.exists():
        raise FileNotFoundError(f"Cannot create rollback snapshot; missing {label}: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {
        "backup": str(destination),
        "restore": str(source),
    }


def _create_upgrade_rollback(
    execution: dict[str, Any],
    manifest: dict[str, Any],
    versions_data: dict[str, Any],
) -> None:
    rollback_dir = _upgrade_rollbacks_path(execution["execution_id"])
    rollback_dir.mkdir(parents=True, exist_ok=True)

    rollback_files: dict[str, Any] = {
        "versions_file": _copy_required_snapshot(
            _versions_file(),
            rollback_dir / "versions.json",
            "versions.json",
        ),
        "start_script": _copy_required_snapshot(
            minecraft_settings.get_start_script_path(),
            rollback_dir / "start.sh",
            "start.sh",
        ),
        "paper": {},
        "plugins": {},
    }

    plugins = versions_data.get("plugins", {})
    paper_config = plugins.get("paper", {})
    current_paper_file = _safe_upgrade_filename(paper_config.get("file"))
    rollback_files["paper"] = _copy_required_snapshot(
        _server_path() / current_paper_file,
        rollback_dir / _safe_snapshot_name("paper", current_paper_file),
        "current Paper JAR",
    )

    for item in _ready_upgrade_plugins(manifest):
        plugin_id = item["plugin_id"]
        plugin_config = plugins.get(plugin_id, {})
        current_file = _safe_upgrade_filename(plugin_config.get("file"))
        rollback_files["plugins"][plugin_id] = _copy_required_snapshot(
            _plugins_path() / current_file,
            rollback_dir / _safe_snapshot_name(plugin_id, current_file),
            f"current plugin JAR for {plugin_id}",
        )

    execution["rollback_files"] = rollback_files


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def _restore_file(snapshot: dict[str, Any]) -> None:
    backup = Path(snapshot["backup"])
    restore = Path(snapshot["restore"])
    restore.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, restore)


def _restore_upgrade_rollback(execution: dict[str, Any]) -> None:
    rollback_files = execution.get("rollback_files") or {}
    applied_files = execution.get("applied_files") or {}

    paper_snapshot = rollback_files.get("paper")
    paper_applied = applied_files.get("paper")
    if paper_snapshot and paper_applied:
        applied_path = Path(paper_applied)
        restore_path = Path(paper_snapshot["restore"])
        if applied_path.exists() and not _same_path(applied_path, restore_path):
            applied_path.unlink()

    plugin_snapshots = rollback_files.get("plugins") or {}
    plugin_applied = applied_files.get("plugins") or {}
    for plugin_id, snapshot in plugin_snapshots.items():
        applied_path_raw = plugin_applied.get(plugin_id)
        if not applied_path_raw:
            continue
        applied_path = Path(applied_path_raw)
        restore_path = Path(snapshot["restore"])
        if applied_path.exists() and not _same_path(applied_path, restore_path):
            applied_path.unlink()

    if paper_snapshot:
        _restore_file(paper_snapshot)
    for snapshot in plugin_snapshots.values():
        _restore_file(snapshot)
    if rollback_files.get("start_script"):
        _restore_file(rollback_files["start_script"])
    if rollback_files.get("versions_file"):
        _restore_file(rollback_files["versions_file"])


def _apply_staged_paper(
    execution: dict[str, Any],
    manifest: dict[str, Any],
    versions_data: dict[str, Any],
) -> None:
    from app.services.minecraft_server import update_start_script

    paper = _paper_upgrade_artifact(manifest)
    target_filename = _safe_upgrade_filename(paper.get("filename"))
    staged_path = Path(execution["staged_files"]["paper"]["path"])
    destination = _server_path() / target_filename

    current_filename = _safe_upgrade_filename(
        versions_data.get("plugins", {}).get("paper", {}).get("file")
    )
    current_path = _server_path() / current_filename

    shutil.copy2(staged_path, destination)
    execution.setdefault("applied_files", {})["paper"] = str(destination)

    if current_path.exists() and not _same_path(current_path, destination):
        current_path.unlink()

    if not update_start_script(target_filename):
        raise RuntimeError(f"Failed to update start.sh for {target_filename}")


def _apply_staged_plugins(
    execution: dict[str, Any],
    manifest: dict[str, Any],
    versions_data: dict[str, Any],
) -> int:
    applied_count = 0
    execution.setdefault("applied_files", {}).setdefault("plugins", {})
    staged_plugins = execution.get("staged_files", {}).get("plugins", {})
    plugins = versions_data.get("plugins", {})

    for item in _ready_upgrade_plugins(manifest):
        plugin_id = item["plugin_id"]
        target_filename = _safe_upgrade_filename(item.get("filename"))
        staged_entry = staged_plugins.get(plugin_id)
        if not staged_entry:
            raise RuntimeError(f"Missing staged artifact for {plugin_id}")

        staged_path = Path(staged_entry["path"])
        destination = _plugins_path() / target_filename
        current_filename = _safe_upgrade_filename(plugins.get(plugin_id, {}).get("file"))
        current_path = _plugins_path() / current_filename

        shutil.copy2(staged_path, destination)
        execution["applied_files"]["plugins"][plugin_id] = str(destination)

        if current_path.exists() and not _same_path(current_path, destination):
            current_path.unlink()

        applied_count += 1

    return applied_count


def _set_optional_hash(config_data: dict[str, Any], key: str, value: Any) -> None:
    if value:
        config_data[key] = value
    else:
        config_data.pop(key, None)


def _update_versions_for_upgrade(
    manifest: dict[str, Any],
    versions_data: dict[str, Any],
) -> None:
    versions_data["minecraft_version"] = manifest.get("target_version")
    versions_data["last_upgrade"] = _now_iso()

    plugins = versions_data.setdefault("plugins", {})
    paper = _paper_upgrade_artifact(manifest)
    paper_config = plugins.setdefault("paper", {})
    paper_config["current_version"] = paper.get("paper_version")
    paper_config["current_build"] = paper.get("build")
    paper_config["file"] = _safe_upgrade_filename(paper.get("filename"))
    paper_config["full_version"] = paper.get("paper_version")
    paper_config["installed_at"] = _now_iso()
    _set_optional_hash(paper_config, "sha256", paper.get("sha256"))
    _set_optional_hash(paper_config, "sha512", paper.get("sha512"))

    for item in _ready_upgrade_plugins(manifest):
        plugin_id = item["plugin_id"]
        plugin_config = plugins.setdefault(plugin_id, {})
        plugin_config["current_version"] = item.get("latest_version") or plugin_config.get("current_version")
        plugin_config["file"] = _safe_upgrade_filename(item.get("filename"))
        plugin_config["installed_at"] = _now_iso()
        if item.get("latest_full_version"):
            plugin_config["full_version"] = item.get("latest_full_version")
        else:
            full_version = extract_version_from_filename(plugin_config["file"])
            if full_version:
                plugin_config["full_version"] = full_version
        _set_optional_hash(plugin_config, "sha256", item.get("sha256"))
        _set_optional_hash(plugin_config, "sha512", item.get("sha512"))
        if item.get("loader"):
            plugin_config["loader"] = item.get("loader")
        if item.get("game_versions"):
            plugin_config["game_versions"] = item.get("game_versions")

    save_versions(versions_data)


async def execute_upgrade_manifest(manifest_id: str, *, actor: str = "") -> dict[str, Any]:
    """Execute a gated Paper/Minecraft upgrade manifest with staging and rollback."""
    block = _profile_operation_block("execute upgrade manifest")
    if block:
        return {
            "success": False,
            "status": "rejected",
            "manifest_id": manifest_id,
            "error": block["error"],
            "error_code": block["error_code"],
            "profile_id": block.get("profile_id"),
            "profile_name": block.get("profile_name"),
        }

    try:
        manifest = load_upgrade_manifest(manifest_id)
    except FileNotFoundError as exc:
        return _execution_rejection(manifest_id, str(exc), "manifest_not_found")
    except ValueError as exc:
        return _execution_rejection(manifest_id, str(exc), "invalid_manifest_id")

    versions_data = load_versions()
    rejection = _validate_executable_manifest(manifest, versions_data)
    if rejection:
        return rejection

    execution = _build_upgrade_execution(manifest, actor=actor)
    _save_upgrade_execution(execution)

    try:
        _set_execution_status(execution, "staging")
        _record_execution_step(execution, "stage_artifacts", "started")
        _save_upgrade_execution(execution)
        await _stage_upgrade_artifacts(execution, manifest)
        _record_execution_step(
            execution,
            "stage_artifacts",
            "completed",
            plugin_count=len(_ready_upgrade_plugins(manifest)),
        )
        _save_upgrade_execution(execution)

        _record_execution_step(execution, "create_rollback_snapshot", "started")
        _create_upgrade_rollback(execution, manifest, versions_data)
        _record_execution_step(execution, "create_rollback_snapshot", "completed")
        _save_upgrade_execution(execution)

        from app.services import minecraft_server

        server_was_running = minecraft_server.is_server_running()
        execution["server_was_running"] = server_was_running
        if server_was_running:
            _set_execution_status(execution, "stopping")
            _record_execution_step(execution, "stop_server", "started")
            _save_upgrade_execution(execution)
            stop_result = await minecraft_server.stop_server()
            if not stop_result.get("success"):
                raise RuntimeError(f"Failed to stop server: {stop_result.get('error', 'unknown error')}")
            _record_execution_step(execution, "stop_server", "completed", result=stop_result)
        else:
            _record_execution_step(execution, "stop_server", "skipped", reason="server_not_running")
        _save_upgrade_execution(execution)

        _set_execution_status(execution, "applying_paper")
        _record_execution_step(execution, "apply_paper", "started")
        _save_upgrade_execution(execution)
        _apply_staged_paper(execution, manifest, versions_data)
        _record_execution_step(
            execution,
            "apply_paper",
            "completed",
            filename=manifest.get("paper", {}).get("filename"),
        )
        _save_upgrade_execution(execution)

        _set_execution_status(execution, "applying_plugins")
        _record_execution_step(execution, "apply_plugins", "started")
        _save_upgrade_execution(execution)
        applied_count = _apply_staged_plugins(execution, manifest, versions_data)
        _record_execution_step(execution, "apply_plugins", "completed", applied_count=applied_count)
        _record_execution_step(execution, "update_versions", "started")
        _update_versions_for_upgrade(manifest, versions_data)
        _record_execution_step(
            execution,
            "update_versions",
            "completed",
            minecraft_version=manifest.get("target_version"),
        )
        _save_upgrade_execution(execution)

        if server_was_running:
            _set_execution_status(execution, "starting")
            _record_execution_step(execution, "start_server", "started")
            _save_upgrade_execution(execution)
            start_result = await minecraft_server.start_server(wait_for_ready=True)
            if not start_result.get("success"):
                raise RuntimeError(f"Failed to start server: {start_result.get('error', 'unknown error')}")
            _record_execution_step(execution, "start_server", "completed", result=start_result)
        else:
            _record_execution_step(execution, "start_server", "skipped", reason="server_was_not_running")

        _set_execution_status(execution, "succeeded")
        execution["completed_at"] = _now_iso()
        _save_upgrade_execution(execution)
        return execution

    except Exception as exc:
        execution["error"] = str(exc)
        _record_execution_step(execution, "execution_error", "failed", error=str(exc))

        if execution.get("rollback_files"):
            try:
                if execution.get("status") == "starting":
                    from app.services import minecraft_server

                    if minecraft_server.is_server_running():
                        _record_execution_step(execution, "stop_failed_upgrade_server", "started")
                        stop_result = await minecraft_server.stop_server()
                        if not stop_result.get("success"):
                            raise RuntimeError(
                                f"Failed to stop unsuccessful upgraded server before rollback: "
                                f"{stop_result.get('error', 'unknown error')}"
                            )
                        _record_execution_step(
                            execution,
                            "stop_failed_upgrade_server",
                            "completed",
                            result=stop_result,
                        )

                _record_execution_step(execution, "rollback", "started")
                _restore_upgrade_rollback(execution)
                _record_execution_step(execution, "rollback", "completed")

                if execution.get("server_was_running"):
                    from app.services import minecraft_server

                    _record_execution_step(execution, "start_previous_server", "started")
                    start_result = await minecraft_server.start_server(wait_for_ready=True)
                    if not start_result.get("success"):
                        raise RuntimeError(
                            f"Rollback restored files but previous server failed to start: "
                            f"{start_result.get('error', 'unknown error')}"
                        )
                    _record_execution_step(execution, "start_previous_server", "completed", result=start_result)

                _set_execution_status(execution, "rolled_back")
            except Exception as rollback_exc:
                execution["rollback_error"] = str(rollback_exc)
                _record_execution_step(execution, "rollback", "failed", error=str(rollback_exc))
                _set_execution_status(execution, "rollback_failed")
        else:
            _set_execution_status(execution, "failed")

        execution["completed_at"] = _now_iso()
        _save_upgrade_execution(execution)
        return execution


def backup_plugin(plugin_id: str, filename: str) -> Path:
    """Create backup of current plugin JAR"""
    server_path = _server_path()
    plugins_path = _plugins_path()
    backups_path = _backups_path()
    backups_path.mkdir(parents=True, exist_ok=True)

    # Determine source path (Paper is in root, plugins are in plugins/)
    if plugin_id == "paper":
        source = server_path / filename
    else:
        source = plugins_path / filename

    if not source.exists():
        raise FileNotFoundError(f"Plugin file not found: {source}")

    # Create timestamped backup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{filename}.{timestamp}.bak"
    backup_path = backups_path / backup_name

    shutil.copy2(source, backup_path)

    # Clean up old backups for this file
    _cleanup_old_backups(filename, keep=5)

    return backup_path


def _cleanup_old_backups(original_filename: str, keep: int = 5) -> None:
    """
    Delete old backup files, keeping only the most recent N versions.

    For versioned files (Paper, plugins), groups all versions together.
    Examples:
        - paper-1.21.11-100.jar → matches all paper-*.jar.*.bak
        - grimac-bukkit-2.3.73-cd86c14.jar → matches all grimac-bukkit-*.jar.*.bak
        - Geyser-Spigot.jar → matches Geyser-Spigot.jar.*.bak (exact)

    Args:
        original_filename: Original JAR filename (e.g., "paper-1.21.11-110.jar")
        keep: Number of recent backups to preserve per file pattern
    """
    import re

    # Normalize filename to base pattern for grouping
    # paper-1.21.11-100.jar → paper-*.jar
    # grimac-bukkit-2.3.73-cd86c14.jar → grimac-bukkit-*.jar
    # Geyser-Spigot.jar → Geyser-Spigot.jar (no version, keep exact)

    # Remove .jar extension for processing
    base = original_filename.replace('.jar', '')

    # Try to identify versioned pattern (contains digits)
    if re.search(r'-?\d+\.\d+', base):
        # Has version numbers - extract base name before version
        # paper-1.21.11-100 → paper
        # grimac-bukkit-2.3.73-cd86c14 → grimac-bukkit
        # ViaVersion-5.7.0 → ViaVersion
        parts = re.split(r'-\d+\.\d+', base, maxsplit=1)
        if parts and parts[0]:
            backup_pattern = f"{parts[0]}-*.jar.*.bak"
        else:
            # Fallback to exact match
            backup_pattern = f"{original_filename}.*.bak"
    else:
        # No version pattern detected, use exact match
        backup_pattern = f"{original_filename}.*.bak"

    # Find all backups matching this pattern
    backups_path = _backups_path()
    backup_files = sorted(
        backups_path.glob(backup_pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True  # Newest first
    )

    # Delete old backups beyond retention limit
    files_to_delete = backup_files[keep:]
    if files_to_delete:
        logging.info(f"Cleaning up {len(files_to_delete)} old backups for pattern: {backup_pattern}")

    for backup_file in files_to_delete:
        try:
            backup_file.unlink()
            logging.info(f"Deleted old backup: {backup_file.name}")
        except Exception as e:
            logging.warning(f"Failed to delete backup {backup_file.name}: {e}")


def verify_hash(filepath: Path, expected_sha256: str = None, expected_sha512: str = None) -> bool:
    """Verify file hash matches expected value"""
    with open(filepath, "rb") as f:
        content = f.read()

    if expected_sha256:
        actual = hashlib.sha256(content).hexdigest()
        return actual.lower() == expected_sha256.lower()

    if expected_sha512:
        actual = hashlib.sha512(content).hexdigest()
        return actual.lower() == expected_sha512.lower()

    # No hash to verify
    return True


def _filename_from_content_disposition(value: str) -> Optional[str]:
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    filename = message.get_param("filename", header="content-disposition")
    if not filename:
        return None
    return _safe_plugin_jar_name(str(filename))


def _fallback_update_filename(update: UpdateCheck) -> str:
    base = _slugify_plugin_id(update.plugin_id or "plugin")
    version = _slugify_plugin_id(update.latest_version or "latest", fallback="latest")
    return f"{base}-{version}.jar"


async def _resolve_voxelshop_update_download(update: UpdateCheck) -> None:
    if update.source != "voxelshop" or update.download_url:
        return

    versions_data = load_versions()
    plugin_config = versions_data.get("plugins", {}).get(update.plugin_id, {})
    resource_id = plugin_config.get("project_id")
    download = await get_voxelshop_download_url(resource_id)
    update.download_url = download["url"]
    if download.get("version"):
        update.latest_version = str(download["version"])
        update.latest_full_version = update.latest_version


async def download_update(update: UpdateCheck) -> Path:
    """Download plugin update to temp location"""
    await _resolve_voxelshop_update_download(update)
    if not update.download_url:
        raise ValueError("No download URL available")

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(
            update.download_url,
            headers={"User-Agent": USER_AGENT}
        )
        response.raise_for_status()

        filename = (
            _safe_plugin_jar_name(update.filename or "")
            or _filename_from_content_disposition(response.headers.get("content-disposition", ""))
            or _safe_plugin_jar_name(Path(str(response.url.path)).name)
            or _fallback_update_filename(update)
        )
        update.filename = filename

        # Save to backups folder temporarily
        backups_path = _backups_path()
        backups_path.mkdir(parents=True, exist_ok=True)
        temp_path = backups_path / f"download_{filename}"

        with open(temp_path, "wb") as f:
            f.write(response.content)

        # Verify hash
        if not verify_hash(temp_path, update.sha256, update.sha512):
            temp_path.unlink()
            raise ValueError("Hash verification failed")

        return temp_path


async def apply_update_from_local_file(
    plugin_id: str,
    update: UpdateCheck,
    local_file: Path,
    *,
    operation: str = "manual_file_update",
    prefer_update_version: bool = False,
) -> OperationLog:
    """Apply a plugin update from a locally downloaded JAR."""
    log = OperationLog(
        timestamp=datetime.now().isoformat(),
        plugin=plugin_id,
        operation=operation,
        from_version=update.current_version,
        to_version=update.latest_version,
    )
    block = _profile_operation_block("apply local update")
    if block:
        return _mark_log_blocked_by_profile(log, block)

    safe_filename = _safe_plugin_jar_name(update.filename or local_file.name)
    if not safe_filename:
        raise ValueError("Downloaded file must be a .jar")
    update.filename = safe_filename

    source_path = Path(local_file)
    if not source_path.is_file():
        raise FileNotFoundError(f"Downloaded JAR not found: {source_path}")

    versions_data = load_versions()
    plugin_config = versions_data.get("plugins", {}).get(plugin_id, {})
    current_file = plugin_config.get("file")

    try:
        log.add_step("backup_started", file=current_file)
        backup_path = backup_plugin(plugin_id, current_file)
        log.add_step("backup_created", path=str(backup_path))

        file_size = source_path.stat().st_size
        log.add_step("local_download_detected", file=source_path.name, size=f"{file_size / 1024 / 1024:.2f}MB")
        if not verify_hash(source_path, update.sha256, update.sha512):
            raise ValueError("Hash verification failed")
        log.add_step("hash_verified", sha256=update.sha256 or "N/A", sha512=update.sha512 or "N/A")

        server_path = _server_path()
        plugins_path = _plugins_path()
        if plugin_id == "paper":
            dest_path = server_path / update.filename
            old_jar = server_path / current_file
            if old_jar.exists() and old_jar != dest_path:
                old_jar.unlink()
        else:
            dest_path = plugins_path / update.filename
            old_jar = plugins_path / current_file
            if old_jar.exists() and old_jar != dest_path:
                old_jar.unlink()

        shutil.move(str(source_path), str(dest_path))
        log.add_step("file_replaced", path=str(dest_path))

        local_metadata = _read_plugin_metadata(dest_path) if plugin_id != "paper" else {}
        detected_version = str(local_metadata.get("version") or "").strip()
        installed_version = (
            update.latest_version
            if prefer_update_version
            else (detected_version or update.latest_version)
        )
        plugin_config["current_version"] = installed_version
        plugin_config["file"] = update.filename
        plugin_config["installed_at"] = datetime.now().isoformat()
        if local_metadata:
            plugin_config["local_metadata"] = local_metadata
        hashes = _file_hashes(dest_path)
        plugin_config["sha512"] = hashes["sha512"]
        if update.sha256:
            plugin_config["sha256"] = update.sha256
        if update.sha512:
            plugin_config["sha512"] = update.sha512
        if plugin_id == "paper" and "-" in update.latest_version:
            plugin_config["current_build"] = int(update.latest_version.split("-")[-1])

        if prefer_update_version and update.latest_full_version:
            plugin_config["full_version"] = update.latest_full_version
        elif prefer_update_version and update.latest_version:
            plugin_config["full_version"] = update.latest_version
        elif detected_version:
            plugin_config["full_version"] = detected_version
        elif update.latest_full_version:
            plugin_config["full_version"] = update.latest_full_version
        elif update.filename:
            full_ver = extract_version_from_filename(update.filename)
            if full_ver:
                plugin_config["full_version"] = full_ver

        versions_data["plugins"][plugin_id] = plugin_config
        save_versions(versions_data)
        log.add_step("versions_updated")
        log.status = "success"
    except Exception as e:
        log.status = "failed"
        log.error = str(e)
        log.add_step("error", message=str(e))

    log.save()
    return log


async def apply_update(plugin_id: str, update: UpdateCheck) -> OperationLog:
    """
    Apply a plugin update with full logging

    Steps:
    1. Create backup of current version
    2. Download new version
    3. Verify hash
    4. Replace file
    5. Update versions.json
    """
    log = OperationLog(
        timestamp=datetime.now().isoformat(),
        plugin=plugin_id,
        operation="update",
        from_version=update.current_version,
        to_version=update.latest_version
    )
    block = _profile_operation_block("apply update")
    if block:
        return _mark_log_blocked_by_profile(log, block)

    versions_data = load_versions()
    plugin_config = versions_data.get("plugins", {}).get(plugin_id, {})
    current_file = plugin_config.get("file")

    try:
        # Step 1: Backup
        log.add_step("backup_started", file=current_file)
        backup_path = backup_plugin(plugin_id, current_file)
        log.add_step("backup_created", path=str(backup_path))

        # Step 2: Download
        log.add_step("download_started", url=update.download_url)
        temp_file = await download_update(update)
        file_size = temp_file.stat().st_size
        log.add_step("download_complete", size=f"{file_size / 1024 / 1024:.2f}MB")

        # Step 3: Verify (already done in download_update)
        log.add_step("hash_verified", sha256=update.sha256 or "N/A", sha512=update.sha512 or "N/A")

        # Step 4: Replace file
        server_path = _server_path()
        plugins_path = _plugins_path()
        if plugin_id == "paper":
            dest_path = server_path / update.filename
            # Also remove old JAR
            old_jar = server_path / current_file
            if old_jar.exists() and old_jar != dest_path:
                old_jar.unlink()
        else:
            dest_path = plugins_path / update.filename
            # Remove old JAR if different name
            old_jar = plugins_path / current_file
            if old_jar.exists() and old_jar != dest_path:
                old_jar.unlink()

        shutil.move(str(temp_file), str(dest_path))
        log.add_step("file_replaced", path=str(dest_path))

        # Step 4.5: For Paper, update start.sh
        if plugin_id == "paper":
            from app.services.minecraft_server import update_start_script
            if update_start_script(update.filename):
                log.add_step("start_script_updated", new_jar=update.filename)
            else:
                log.add_step("start_script_update_failed", new_jar=update.filename)

        # Step 5: Update versions.json
        local_metadata = _read_plugin_metadata(dest_path) if plugin_id != "paper" else {}
        detected_version = str(local_metadata.get("version") or "").strip()
        plugin_config["current_version"] = detected_version or update.latest_version
        plugin_config["file"] = update.filename
        plugin_config["installed_at"] = datetime.now().isoformat()
        if local_metadata:
            plugin_config["local_metadata"] = local_metadata
        hashes = _file_hashes(dest_path)
        plugin_config["sha512"] = hashes["sha512"]
        if update.sha256:
            plugin_config["sha256"] = update.sha256
        if update.sha512:
            plugin_config["sha512"] = update.sha512
        if plugin_id == "paper" and "-" in update.latest_version:
            plugin_config["current_build"] = int(update.latest_version.split("-")[-1])

        # Save full_version, preferring the plugin's own metadata over filename parsing.
        if detected_version:
            plugin_config["full_version"] = detected_version
        elif update.latest_full_version:
            plugin_config["full_version"] = update.latest_full_version
        elif update.filename:
            full_ver = extract_version_from_filename(update.filename)
            if full_ver:
                plugin_config["full_version"] = full_ver

        versions_data["plugins"][plugin_id] = plugin_config
        save_versions(versions_data)
        log.add_step("versions_updated")

        log.status = "success"

    except Exception as e:
        log.status = "failed"
        log.error = str(e)
        log.add_step("error", message=str(e))

    log.save()
    return log


def get_update_logs(limit: int = 20) -> list[dict]:
    """Get recent update operation logs"""
    logs = []
    update_logs_path = _update_logs_path()

    if not update_logs_path.exists():
        return logs

    # Get all log files sorted by modification time
    log_files = sorted(
        update_logs_path.glob("*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )[:limit]

    for filepath in log_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                logs.append(json.load(f))
        except:
            pass

    return logs


def get_server_status() -> dict:
    """Get basic server status information"""
    server_path = _server_path()
    plugins_path = _plugins_path()
    status = {
        "server_path": str(server_path),
        "exists": server_path.exists(),
        "plugins_count": 0,
        "jar_files": []
    }

    if plugins_path.exists():
        jar_files = list(plugins_path.glob("*.jar"))
        status["plugins_count"] = len(jar_files)
        status["jar_files"] = [f.name for f in jar_files]

    # Check for Paper JAR
    paper_jars = list(server_path.glob("paper-*.jar"))
    if paper_jars:
        status["paper_jar"] = paper_jars[0].name

    return status
