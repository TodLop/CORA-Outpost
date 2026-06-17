"""Execution helpers for the guarded Minecraft setup create flow."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.services import minecraft_settings, minecraft_setup, minecraft_updater


OWNER = "cora-minecraft-setup"
CLAIM_FILE = ".cora-setup-claim.json"
STAGING_DIR = ".cora-setup-staging"
LEDGER_FILE = "attempt.json"
CLAIM_STALE_SECONDS = 60 * 60


class SetupExecutionError(RuntimeError):
    """Raised when setup execution must fail with a structured API payload."""

    def __init__(
        self,
        *,
        error_code: str,
        error: str,
        status_code: int,
        errors: dict[str, str] | None = None,
        warnings: list[str] | None = None,
        preflight: dict[str, Any] | None = None,
        preview: dict[str, Any] | None = None,
        cleanup: dict[str, Any] | None = None,
    ):
        super().__init__(error)
        self.error_code = error_code
        self.error = error
        self.status_code = status_code
        self.errors = errors or {}
        self.warnings = warnings or []
        self.preflight = preflight
        self.preview = preview
        self.cleanup = cleanup or {}

    def to_response(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "error",
            "success": False,
            "error_code": self.error_code,
            "error": self.error,
        }
        if self.errors:
            payload["errors"] = self.errors
        if self.warnings:
            payload["warnings"] = self.warnings
        if self.preflight is not None:
            payload["preflight"] = self.preflight
        if self.preview is not None:
            payload["preview"] = self.preview
        if self.cleanup:
            payload["cleanup"] = self.cleanup
        return payload


async def resolve_paper_target(minecraft_version: str) -> minecraft_updater.VersionInfo:
    return await minecraft_updater.get_paper_stable_target(minecraft_version)


async def download_paper_bytes(download_url: str) -> bytes:
    async with httpx.AsyncClient(timeout=minecraft_updater.TIMEOUT) as client:
        response = await client.get(download_url, headers={"User-Agent": minecraft_updater.USER_AGENT})
        response.raise_for_status()
        return response.content


async def execute_create_server(payload: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """Create a new Paper server from the setup contract without starting it."""
    if not isinstance(payload, dict):
        raise SetupExecutionError(
            error_code="setup_create_invalid",
            error="Setup create request body must be a JSON object.",
            status_code=400,
            errors={"payload": "Request body must be a JSON object."},
        )

    target = _payload_target(payload)
    profile_id = _deterministic_profile_id(payload, target) if target is not None else None
    recovered_attempts: list[dict[str, Any]] = []

    if target is not None and profile_id is not None:
        recovered_attempts = _recover_target_setup_markers(target)
        existing_result = await _existing_profile_result(payload, target, profile_id)
        if existing_result is not None:
            existing_result["result"]["recovered_attempts"] = recovered_attempts + existing_result["result"].get("recovered_attempts", [])
            return existing_result

    preflight_result = _build_preflight(payload)
    preflight = preflight_result["preflight"]
    preview = preflight_result["preview"]
    target = Path(preflight["target"]["server_directory"]).expanduser().resolve(strict=False)
    profile_id = _deterministic_profile_id(payload, target)

    existing_result = await _existing_profile_result(payload, target, profile_id, preflight=preflight, preview=preview)
    if existing_result is not None:
        return existing_result

    paper_target = await _validated_paper_target(payload)
    jar_bytes = await _download_validated_jar(paper_target)

    artifacts = _build_artifacts(preflight, preview, paper_target, jar_bytes)
    claim: dict[str, Any] | None = None
    ledger: dict[str, Any] | None = None
    cleanup: dict[str, Any] = {}
    profile_created = False

    try:
        claim = _claim_target(target, profile_id)
        ledger = _create_attempt_ledger(target, claim, artifacts)
        _write_claim(target, {**claim, "state": "claimed", "ledger_path": _relative_to_target(target, _ledger_path(target, ledger))})
        _stage_artifacts(target, ledger)
        _write_claim(target, {**claim, "state": "staged", "ledger_path": _relative_to_target(target, _ledger_path(target, ledger))})
        _publish_artifacts(target, ledger)
        _write_claim(target, {**claim, "state": "published", "ledger_path": _relative_to_target(target, _ledger_path(target, ledger))})
        profile = _create_profile_metadata(payload, target, profile_id)
        profile_created = True
        ledger["state"] = "profile_created"
        ledger["profile"] = profile
        _write_ledger(target, ledger)
        _write_claim(target, {**claim, "state": "profile_created", "ledger_path": _relative_to_target(target, _ledger_path(target, ledger))})
        _complete_attempt(target, ledger)
    except SetupExecutionError as exc:
        if ledger is not None:
            cleanup = _cleanup_ledger(target, ledger, delete_publishing_finals=not profile_created)
        elif claim is not None:
            cleanup = _remove_claim_only(target)
        exc.cleanup.update(cleanup)
        raise
    except Exception as exc:
        if ledger is not None:
            cleanup = _cleanup_ledger(target, ledger, delete_publishing_finals=not profile_created)
        elif claim is not None:
            cleanup = _remove_claim_only(target)
        raise SetupExecutionError(
            error_code="setup_create_failed",
            error=f"Minecraft setup execution failed: {exc}",
            status_code=500,
            cleanup=cleanup,
            preflight=preflight,
            preview=preview,
        ) from exc

    return {
        "status": "ok",
        "success": True,
        "result": {
            "mode": "execution",
            "profile": profile,
            "created_artifacts": _public_artifacts(ledger),
            "preflight": preflight,
            "recovered_attempts": recovered_attempts,
            "idempotent": False,
        },
    }


def _build_preflight(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return minecraft_setup.build_create_server_preflight(payload)
    except minecraft_setup.SetupCreationNotReady as exc:
        error = "Minecraft setup target is not ready for creation."
        if "eula_accepted" in exc.errors:
            error = "Minecraft setup policy is not ready for execution."
        raise SetupExecutionError(
            error_code="setup_create_not_ready",
            error=error,
            status_code=409,
            errors=exc.errors,
            warnings=exc.warnings,
            preflight=exc.preflight,
            preview=exc.preview,
        ) from exc
    except minecraft_setup.SetupValidationError as exc:
        raise SetupExecutionError(
            error_code="setup_create_invalid",
            error="Invalid Minecraft setup create-server payload.",
            status_code=400,
            errors=exc.errors,
            warnings=exc.warnings,
        ) from exc


async def _validated_paper_target(payload: dict[str, Any]) -> minecraft_updater.VersionInfo:
    minecraft_version = str(payload.get("minecraft_version") or "").strip()
    paper_version = str(payload.get("paper_version") or "").strip()
    paper_filename = str(payload.get("paper_filename") or "").strip()

    try:
        target = await resolve_paper_target(minecraft_version)
    except Exception as exc:
        raise SetupExecutionError(
            error_code="setup_paper_target_unavailable",
            error=f"Could not resolve the selected Paper target: {exc}",
            status_code=502,
        ) from exc

    target_game_version = _target_game_version(target)
    target_build = str(target.build or "")
    target_version = str(target.version or "")
    target_filename = str(target.filename or "").strip()
    if (
        minecraft_version != target_game_version
        or paper_version not in {target_build, target_version}
        or not target_filename
        or paper_filename != target_filename
    ):
        raise SetupExecutionError(
            error_code="setup_paper_target_mismatch",
            error="Selected Paper target no longer matches the server-side stable target.",
            status_code=409,
            errors={
                "paper": "Refresh the Paper target list and run setup preflight again.",
            },
        )
    if not target.download_url:
        raise SetupExecutionError(
            error_code="setup_paper_target_unavailable",
            error="Selected Paper target does not include a download URL.",
            status_code=502,
        )
    return target


async def _download_validated_jar(target: minecraft_updater.VersionInfo) -> bytes:
    try:
        content = await download_paper_bytes(str(target.download_url))
    except Exception as exc:
        raise SetupExecutionError(
            error_code="setup_paper_download_failed",
            error=f"Failed to download the selected Paper jar: {exc}",
            status_code=502,
        ) from exc

    expected_sha = str(target.sha256 or "").strip().lower()
    actual_sha = _sha256_bytes(content)
    if expected_sha and actual_sha != expected_sha:
        raise SetupExecutionError(
            error_code="setup_paper_hash_mismatch",
            error="Downloaded Paper jar did not match the expected checksum.",
            status_code=502,
            errors={"paper_sha256": "Downloaded file checksum mismatch."},
        )
    return content


def _build_artifacts(
    preflight: dict[str, Any],
    preview: dict[str, Any],
    paper_target: minecraft_updater.VersionInfo,
    jar_bytes: bytes,
) -> list[dict[str, Any]]:
    filename = str(paper_target.filename or preflight["paper"]["filename"])
    artifacts = [
        _artifact("paper_jar", filename, jar_bytes),
        _artifact("start_script", "start.sh", str(preview["start_script"]).encode("utf-8"), executable=True),
        _artifact("server_properties", "server.properties", str(preview["server_properties"]).encode("utf-8")),
        _artifact("eula", "eula.txt", b"eula=true\n"),
    ]
    return artifacts


def _artifact(kind: str, relative_path: str, content: bytes, *, executable: bool = False) -> dict[str, Any]:
    return {
        "kind": kind,
        "relative_path": _safe_relative_path(relative_path),
        "size": len(content),
        "sha256": _sha256_bytes(content),
        "content": content,
        "executable": executable,
        "state": "planned",
    }


def _claim_target(target: Path, profile_id: str) -> dict[str, Any]:
    if target.exists():
        if not target.is_dir():
            raise _conflict("Target path exists but is not a directory.")
    else:
        try:
            target.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            raise _in_progress("Target directory was claimed by another setup request.") from None
        except OSError as exc:
            raise SetupExecutionError(
                error_code="setup_create_failed",
                error=f"Could not create target directory: {exc}",
                status_code=500,
            ) from exc

    _assert_target_claimable(target)
    attempt_id = secrets.token_urlsafe(12)
    claim = {
        "owner": OWNER,
        "target": str(target),
        "profile_id": profile_id,
        "attempt_id": attempt_id,
        "state": "claimed",
        "created_at": _now_iso(),
    }
    _create_claim_file(target, claim)
    try:
        _assert_target_claimable(target, allow_claim=True)
    except SetupExecutionError:
        _remove_claim_only(target)
        raise
    return claim


def _assert_target_claimable(target: Path, *, allow_claim: bool = False) -> None:
    allowed = {CLAIM_FILE} if allow_claim else set()
    entries = [entry.name for entry in target.iterdir() if entry.name not in allowed]
    if entries:
        raise _conflict("Target directory is no longer empty.")


def _create_attempt_ledger(target: Path, claim: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    attempt_dir = target / STAGING_DIR / str(claim["attempt_id"])
    files_dir = attempt_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=False)
    ledger = {
        "owner": OWNER,
        "attempt_id": claim["attempt_id"],
        "profile_id": claim["profile_id"],
        "target": str(target),
        "state": "claimed",
        "created_at": claim["created_at"],
        "artifacts": [
            {key: value for key, value in artifact.items() if key != "content"}
            for artifact in artifacts
        ],
    }
    for artifact, ledger_artifact in zip(artifacts, ledger["artifacts"]):
        staged_path = files_dir / artifact["relative_path"]
        ledger_artifact["staged_path"] = _relative_to_target(target, staged_path)
    _write_ledger(target, ledger)
    for artifact, ledger_artifact in zip(artifacts, ledger["artifacts"]):
        ledger_artifact["_content"] = artifact["content"]
    return ledger


def _stage_artifacts(target: Path, ledger: dict[str, Any]) -> None:
    for artifact in ledger["artifacts"]:
        staged_path = _target_child(target, artifact["staged_path"])
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(staged_path, flags, 0o755 if artifact.get("executable") else 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(artifact.pop("_content"))
            handle.flush()
            os.fsync(handle.fileno())
        if artifact.get("executable"):
            staged_path.chmod(staged_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        artifact["state"] = "staged"
    ledger["state"] = "staged"
    _write_ledger(target, ledger)


def _publish_artifacts(target: Path, ledger: dict[str, Any]) -> None:
    for artifact in ledger["artifacts"]:
        staged_path = _target_child(target, artifact["staged_path"])
        final_path = _target_child(target, artifact["relative_path"])
        artifact["state"] = "publishing"
        ledger["state"] = "publishing"
        _write_ledger(target, ledger)
        try:
            os.link(staged_path, final_path)
        except FileExistsError as exc:
            raise _conflict(f"Target artifact already exists: {artifact['relative_path']}") from exc
        artifact["state"] = "published"
        artifact["published_at"] = _now_iso()
        ledger["state"] = "published"
        _write_ledger(target, ledger)
        staged_path.unlink()
    _fsync_dir(target)


def _create_profile_metadata(payload: dict[str, Any], target: Path, profile_id: str) -> dict[str, Any]:
    existing = _get_profile(profile_id)
    if existing is not None:
        if _profile_matches(existing, payload, target):
            return existing
        raise _conflict("A different Minecraft server profile already uses the deterministic setup id.")

    try:
        return minecraft_settings.create_profile(
            profile_id=profile_id,
            name=str(payload.get("profile_name") or "").strip(),
            kind="live",
            server_directory=target,
            operations_enabled=True,
            rcon_enabled=False,
            readonly=False,
            set_active=False,
        )
    except minecraft_settings.ProfileValidationError as exc:
        existing = _get_profile(profile_id)
        if existing is not None and _profile_matches(existing, payload, target):
            return existing
        raise SetupExecutionError(
            error_code="setup_profile_invalid",
            error="Could not create Minecraft server profile metadata.",
            status_code=409,
            errors=exc.errors,
        ) from exc
    except minecraft_settings.PathValidationError as exc:
        raise SetupExecutionError(
            error_code="setup_profile_directory_invalid",
            error="Generated Minecraft server directory did not pass profile validation.",
            status_code=500,
            errors=exc.errors,
            warnings=exc.warnings,
        ) from exc


def _complete_attempt(target: Path, ledger: dict[str, Any]) -> None:
    ledger["state"] = "complete"
    ledger["completed_at"] = _now_iso()
    _write_ledger(target, ledger)
    _remove_claim_only(target)
    _remove_verified_attempt_dir(target, ledger)
    staging = target / STAGING_DIR
    try:
        staging.rmdir()
    except OSError:
        pass


def _recover_target_setup_markers(target: Path) -> list[dict[str, Any]]:
    if not target.exists() or not target.is_dir():
        return []
    entries = list(target.iterdir())
    if not entries:
        return []
    known = {CLAIM_FILE, STAGING_DIR}
    if any(entry.name not in known for entry in entries):
        return []

    recovered: list[dict[str, Any]] = []
    claim_path = target / CLAIM_FILE
    if claim_path.exists():
        claim = _read_marker_json(claim_path)
        if not _marker_owned(claim):
            raise _conflict("Target contains an unknown setup claim marker.")
        if not _is_stale_marker(claim):
            raise _in_progress("A setup create attempt is already in progress for this target.")
        ledger_rel = str(claim.get("ledger_path") or "").strip()
        if ledger_rel:
            ledger_path = _target_child(target, ledger_rel)
            if ledger_path.exists():
                ledger = _read_marker_json(ledger_path)
                recovered.append(_recover_ledger(target, ledger))
        recovered.append(_remove_claim_only(target))

    staging = target / STAGING_DIR
    if staging.exists():
        if not staging.is_dir():
            raise _conflict("Target contains an unknown setup staging marker.")
        for attempt_dir in sorted(staging.iterdir()):
            if not attempt_dir.is_dir():
                raise _conflict("Target contains an unknown setup staging entry.")
            ledger_path = attempt_dir / LEDGER_FILE
            if not ledger_path.exists():
                raise _conflict("Target contains an unmarked setup staging directory.")
            ledger = _read_marker_json(ledger_path)
            if not _marker_owned(ledger):
                raise _conflict("Target contains an unknown setup staging ledger.")
            if not _is_stale_marker(ledger):
                raise _in_progress("A setup create attempt is already in progress for this target.")
            recovered.append(_recover_ledger(target, ledger))
        try:
            staging.rmdir()
        except OSError:
            pass
    return recovered


def _recover_ledger(target: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    if ledger.get("state") == "profile_created":
        raise _conflict("A setup attempt reached profile creation and needs manual review.")
    cleanup = _cleanup_ledger(target, ledger, delete_publishing_finals=True)
    cleanup["attempt_id"] = ledger.get("attempt_id")
    return cleanup


def _cleanup_ledger(target: Path, ledger: dict[str, Any], *, delete_publishing_finals: bool) -> dict[str, Any]:
    removed: list[str] = []
    for artifact in ledger.get("artifacts", []):
        state = artifact.get("state")
        should_delete_final = bool(delete_publishing_finals and state == "published")
        final_path = _target_child(target, artifact.get("relative_path"))
        if should_delete_final and final_path.exists():
            if not _file_matches_artifact(final_path, artifact):
                raise _conflict(f"Target artifact changed after setup attempt: {artifact.get('relative_path')}")
            final_path.unlink()
            removed.append(str(artifact.get("relative_path")))
        staged_rel = artifact.get("staged_path")
        if staged_rel:
            staged_path = _target_child(target, staged_rel)
            if staged_path.exists():
                if not _file_matches_artifact(staged_path, artifact):
                    raise _conflict(f"Staged artifact changed after setup attempt: {staged_rel}")
                staged_path.unlink()
                removed.append(str(staged_rel))

    _remove_verified_attempt_dir(target, ledger)
    staging = target / STAGING_DIR
    try:
        staging.rmdir()
    except OSError:
        pass
    _remove_claim_only(target)
    return {"removed": removed}


def _remove_verified_attempt_dir(target: Path, ledger: dict[str, Any]) -> None:
    ledger_path = _ledger_path(target, ledger)
    if ledger_path.exists():
        marker = _read_marker_json(ledger_path)
        if not _marker_owned(marker):
            raise _conflict("Refusing to remove an unknown setup ledger marker.")
        ledger_path.unlink()

    files_dir = ledger_path.parent / "files"
    if files_dir.exists():
        try:
            files_dir.rmdir()
        except OSError as exc:
            raise _conflict("Setup staging files directory contains unlisted files.") from exc

    attempt_dir = ledger_path.parent
    if attempt_dir.exists():
        try:
            attempt_dir.rmdir()
        except OSError as exc:
            raise _conflict("Setup staging attempt directory contains unlisted files.") from exc


def _remove_claim_only(target: Path) -> dict[str, Any]:
    claim_path = target / CLAIM_FILE
    if claim_path.exists():
        claim = _read_marker_json(claim_path)
        if not _marker_owned(claim):
            raise _conflict("Refusing to remove an unknown setup claim marker.")
        claim_path.unlink()
        return {"removed": [CLAIM_FILE]}
    return {"removed": []}


async def _existing_profile_result(
    payload: dict[str, Any],
    target: Path,
    profile_id: str,
    *,
    preflight: dict[str, Any] | None = None,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    profile = _get_profile(profile_id)
    if profile is None:
        return None
    if not _profile_matches(profile, payload, target):
        raise _conflict("A different Minecraft server profile already uses the deterministic setup id.")
    marker_cleanup = _guard_existing_profile_markers(target, profile_id)
    verified = await _verify_existing_setup_artifacts(payload, target)
    return {
        "status": "ok",
        "success": True,
        "result": {
            "mode": "execution",
            "profile": profile,
            "created_artifacts": verified["artifacts"],
            "preflight": preflight or verified["preflight"],
            "preview": preview or verified["preview"],
            "recovered_attempts": marker_cleanup,
            "idempotent": True,
        },
    }


async def _verify_existing_setup_artifacts(
    payload: dict[str, Any],
    target: Path,
) -> dict[str, Any]:
    try:
        preview = minecraft_setup.build_setup_preview(payload)
    except minecraft_setup.SetupValidationError as exc:
        raise SetupExecutionError(
            error_code="setup_create_invalid",
            error="Invalid Minecraft setup create-server payload.",
            status_code=400,
            errors=exc.errors,
            warnings=exc.warnings,
        ) from exc

    creation_policy = preview["creation_policy"]
    if not creation_policy.get("eula_accepted"):
        raise SetupExecutionError(
            error_code="setup_create_not_ready",
            error="Minecraft setup policy is not ready for execution.",
            status_code=409,
            errors={"eula_accepted": "Accept the Minecraft EULA before reusing a setup-created profile."},
            warnings=preview.get("warnings", []),
            preview=preview,
        )

    paper_target = await _validated_paper_target(payload)
    paper_filename = str(paper_target.filename or payload.get("paper_filename") or "").strip()
    expected_text = {
        "start.sh": str(preview["start_script"]).encode("utf-8"),
        "server.properties": str(preview["server_properties"]).encode("utf-8"),
        "eula.txt": b"eula=true\n",
    }
    expected_names = {paper_filename, *expected_text.keys()}
    if not paper_filename:
        raise _conflict("Existing setup profile could not be verified without a Paper filename.")

    _assert_existing_setup_entries_are_expected(target, expected_names)

    for relative_path, expected in expected_text.items():
        path = target / relative_path
        if not path.is_file() or path.read_bytes() != expected:
            raise _conflict(f"Existing setup artifact does not match the current setup contract: {relative_path}")

    paper_path = target / paper_filename
    if not paper_path.is_file():
        raise _conflict("Existing setup Paper jar is missing.")
    expected_sha = str(paper_target.sha256 or "").strip().lower()
    paper_sha = _sha256_file(paper_path)
    if expected_sha and paper_sha != expected_sha:
        raise SetupExecutionError(
            error_code="setup_paper_hash_mismatch",
            error="Existing Paper jar did not match the selected Paper checksum.",
            status_code=409,
            errors={"paper_sha256": "Existing file checksum mismatch."},
        )

    artifacts = [
        _public_existing_artifact("paper_jar", paper_path),
        _public_existing_artifact("start_script", target / "start.sh"),
        _public_existing_artifact("server_properties", target / "server.properties"),
        _public_existing_artifact("eula", target / "eula.txt"),
    ]
    return {
        "artifacts": artifacts,
        "preflight": None,
        "preview": preview,
    }


def _assert_existing_setup_entries_are_expected(target: Path, expected_names: set[str]) -> None:
    for entry in sorted(target.iterdir()) if target.exists() and target.is_dir() else []:
        if entry.name not in expected_names:
            raise _conflict("Existing setup target contains files outside the setup artifact set.")
        if not entry.is_file():
            raise _conflict("Existing setup target contains non-file artifacts.")


def _guard_existing_profile_markers(target: Path, profile_id: str) -> list[dict[str, Any]]:
    cleanup: list[dict[str, Any]] = []
    claim_path = target / CLAIM_FILE
    staging = target / STAGING_DIR

    if claim_path.exists():
        claim = _read_marker_json(claim_path)
        if not _marker_owned(claim) or claim.get("profile_id") != profile_id:
            raise _conflict("Target contains an unknown setup claim marker.")
        if not _is_stale_marker(claim):
            raise _in_progress("A setup create attempt is already in progress for this target.")
        if claim.get("state") not in {"claimed", "staged"} or staging.exists():
            raise _conflict("A setup attempt reached profile creation and needs manual review.")
        cleanup.append(_remove_claim_only(target))

    if staging.exists():
        if not staging.is_dir():
            raise _conflict("Target contains an unknown setup staging marker.")
        for attempt_dir in sorted(staging.iterdir()):
            if not attempt_dir.is_dir():
                raise _conflict("Target contains an unknown setup staging entry.")
            ledger_path = attempt_dir / LEDGER_FILE
            if not ledger_path.exists():
                raise _conflict("Target contains an unmarked setup staging directory.")
            ledger = _read_marker_json(ledger_path)
            if not _marker_owned(ledger) or ledger.get("profile_id") != profile_id:
                raise _conflict("Target contains an unknown setup staging ledger.")
            if not _is_stale_marker(ledger):
                raise _in_progress("A setup create attempt is already in progress for this target.")
            raise _conflict("A stale setup staging marker exists alongside an existing profile and needs manual review.")

    return cleanup


def _get_profile(profile_id: str) -> dict[str, Any] | None:
    try:
        return minecraft_settings.get_profile(profile_id)
    except KeyError:
        return None


def _profile_matches(profile: dict[str, Any], payload: dict[str, Any], target: Path) -> bool:
    expected_name = str(payload.get("profile_name") or "").strip()
    try:
        profile_path = Path(str(profile.get("server_directory") or "")).expanduser().resolve(strict=False)
    except OSError:
        return False
    return (
        profile_path == target
        and str(profile.get("name") or "").strip() == expected_name
        and str(profile.get("kind") or "live") == "live"
        and bool(profile.get("operations_enabled")) is True
        and bool(profile.get("rcon_enabled")) is False
        and bool(profile.get("readonly")) is False
    )


def _payload_target(payload: dict[str, Any]) -> Path | None:
    raw = str(payload.get("server_directory") or "").strip()
    if not raw:
        return None
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return None
    return expanded.resolve(strict=False)


def _deterministic_profile_id(payload: dict[str, Any], target: Path) -> str:
    slug = minecraft_setup.suggest_profile_id(str(payload.get("profile_name") or "server"))
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:12]
    max_slug = 64 - len("setup--") - len(digest)
    slug = slug[:max_slug].strip("-") or "server"
    return f"setup-{slug}-{digest}"


def _target_game_version(target: minecraft_updater.VersionInfo) -> str:
    if target.game_versions:
        return str(target.game_versions[0])
    return str(target.version or "").rsplit("-", 1)[0]


def _safe_relative_path(value: Any) -> str:
    raw = str(value or "").strip()
    path = Path(raw)
    if not raw or path.name != raw or raw.startswith("."):
        raise SetupExecutionError(
            error_code="setup_create_invalid",
            error="Generated setup artifact path was not safe.",
            status_code=500,
        )
    return raw


def _target_child(target: Path, relative_path: Any) -> Path:
    raw = str(relative_path or "").strip()
    path = Path(raw)
    if not raw or path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise _conflict("Setup artifact path escaped the target directory.")
    child = (target / path).resolve(strict=False)
    try:
        child.relative_to(target.resolve(strict=False))
    except ValueError as exc:
        raise _conflict("Setup artifact path escaped the target directory.")
    return child


def _relative_to_target(target: Path, path: Path) -> str:
    return str(path.resolve(strict=False).relative_to(target.resolve(strict=False)))


def _ledger_path(target: Path, ledger: dict[str, Any]) -> Path:
    return target / STAGING_DIR / str(ledger["attempt_id"]) / LEDGER_FILE


def _write_ledger(target: Path, ledger: dict[str, Any]) -> None:
    public_ledger = {
        key: [
            {artifact_key: artifact_value for artifact_key, artifact_value in artifact.items() if not artifact_key.startswith("_")}
            for artifact in value
        ] if key == "artifacts" else value
        for key, value in ledger.items()
    }
    _write_json_atomic(_ledger_path(target, ledger), public_ledger)


def _write_claim(target: Path, claim: dict[str, Any]) -> None:
    _write_json_atomic(target / CLAIM_FILE, claim)


def _create_claim_file(target: Path, claim: dict[str, Any]) -> None:
    path = target / CLAIM_FILE
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise _in_progress("A setup create attempt is already in progress for this target.") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(claim, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir(target)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _read_marker_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise _conflict(f"Could not read setup marker {path.name}.") from exc
    if not isinstance(payload, dict):
        raise _conflict(f"Setup marker {path.name} was not an object.")
    return payload


def _marker_owned(payload: dict[str, Any]) -> bool:
    return payload.get("owner") == OWNER


def _is_stale_marker(payload: dict[str, Any]) -> bool:
    created_at = str(payload.get("created_at") or "").strip()
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created.astimezone(timezone.utc)
    return age.total_seconds() > CLAIM_STALE_SECONDS


def _file_matches_artifact(path: Path, artifact: dict[str, Any]) -> bool:
    try:
        if path.stat().st_size != int(artifact.get("size")):
            return False
        expected = str(artifact.get("sha256") or "").lower()
        return not expected or _sha256_file(path) == expected
    except (OSError, TypeError, ValueError):
        return False


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public_artifacts(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "kind": artifact["kind"],
            "path": artifact["relative_path"],
            "size": artifact["size"],
            "sha256": artifact["sha256"],
        }
        for artifact in ledger.get("artifacts", [])
    ]


def _public_existing_artifact(kind: str, path: Path) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _conflict(message: str) -> SetupExecutionError:
    return SetupExecutionError(
        error_code="setup_create_conflict",
        error=message,
        status_code=409,
    )


def _in_progress(message: str) -> SetupExecutionError:
    return SetupExecutionError(
        error_code="setup_create_in_progress",
        error=message,
        status_code=409,
    )
