from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TextIO

from fastapi import HTTPException

from app.core.auth import is_admin
from app.core.config import HISTORY_DIR
from app.services import minecraft_settings
from app.services import minecraft_server
from app.services import permissions as permissions_service
from app.services.rate_limit import check_rate_limit


PreflightFn = Callable[[dict, dict[str, Any]], tuple[bool, str]]
ExecutorFn = Callable[[dict, dict[str, Any]], Awaitable[dict]]

_IDEMPOTENCY_TTL_SECONDS = int(os.getenv("OPERATIONS_IDEMPOTENCY_TTL_SECONDS", "900"))
_IDEMPOTENCY_LOCK = threading.Lock()
_IDEMPOTENCY_CACHE: dict[str, dict[str, Any]] = {}

_OPERATION_STATE_FILE = HISTORY_DIR / "operation_state.jsonl"
_OPERATION_STATE_LOCK = threading.Lock()
_SERVER_OPERATION_LOCK_FILE = HISTORY_DIR / "server_operation.lock"
_LAST_SERVER_OPERATION_LOCK_ERROR = ""


class OperationNotFound(Exception):
    pass


def _cleanup_expired_idempotency_entries(now: float) -> None:
    expired_keys = [
        cache_key
        for cache_key, entry in _IDEMPOTENCY_CACHE.items()
        if float(entry.get("expires_at", 0)) <= now
    ]
    for cache_key in expired_keys:
        _IDEMPOTENCY_CACHE.pop(cache_key, None)


def _append_operation_state(record: dict[str, Any]) -> None:
    state_file: Path = _OPERATION_STATE_FILE
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with _OPERATION_STATE_LOCK:
        with state_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _acquire_server_operation_lock() -> Optional[TextIO]:
    global _LAST_SERVER_OPERATION_LOCK_ERROR
    try:
        _SERVER_OPERATION_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = open(_SERVER_OPERATION_LOCK_FILE, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _LAST_SERVER_OPERATION_LOCK_ERROR = ""
            return handle
        except BlockingIOError:
            _LAST_SERVER_OPERATION_LOCK_ERROR = ""
            handle.close()
            return None
    except Exception as e:
        _LAST_SERVER_OPERATION_LOCK_ERROR = str(e)
        return None


def _release_server_operation_lock(handle: Optional[TextIO]) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


@dataclass(frozen=True)
class OperationSpec:
    key: str
    required_permission: Optional[str]
    admin_only: bool
    risk: str
    preflight: PreflightFn
    executor: ExecutorFn


def _preflight_always_ok(user_info: dict, params: dict[str, Any]) -> tuple[bool, str]:
    return True, ""


async def _exec_server_start(user_info: dict, params: dict[str, Any]) -> dict:
    return await minecraft_server.start_server()


async def _exec_server_restart(user_info: dict, params: dict[str, Any]) -> dict:
    source = str(params.get("source", "operations"))
    return await minecraft_server.restart_server(source=source)


async def _exec_server_stop(user_info: dict, params: dict[str, Any]) -> dict:
    force = bool(params.get("force", False))
    return await minecraft_server.stop_server(force=force)


async def _exec_server_recover(user_info: dict, params: dict[str, Any]) -> dict:
    return await minecraft_server.recover_server()


async def _exec_server_upgrade(user_info: dict, params: dict[str, Any]) -> dict:
    from app.services import minecraft_updater

    manifest_id = str(params.get("manifest_id") or "").strip()
    actor = user_info.get("email") or user_info.get("name") or "unknown"
    return await minecraft_updater.execute_upgrade_manifest(manifest_id, actor=actor)


_REGISTRY: dict[str, OperationSpec] = {
    "server:start": OperationSpec(
        key="server:start",
        required_permission="server:start",
        admin_only=False,
        risk="medium",
        preflight=_preflight_always_ok,
        executor=_exec_server_start,
    ),
    "server:restart": OperationSpec(
        key="server:restart",
        required_permission="server:restart",
        admin_only=False,
        risk="medium",
        preflight=_preflight_always_ok,
        executor=_exec_server_restart,
    ),
    "server:stop": OperationSpec(
        key="server:stop",
        required_permission="server:stop",
        admin_only=True,
        risk="high",
        preflight=_preflight_always_ok,
        executor=_exec_server_stop,
    ),
    "server:recover": OperationSpec(
        key="server:recover",
        required_permission=None,
        admin_only=True,
        risk="high",
        preflight=_preflight_always_ok,
        executor=_exec_server_recover,
    ),
    "server:upgrade": OperationSpec(
        key="server:upgrade",
        required_permission=None,
        admin_only=True,
        risk="high",
        preflight=_preflight_always_ok,
        executor=_exec_server_upgrade,
    ),
}


_PROFILE_GUARDED_SERVER_ACTIONS = {
    "server:start": "start server",
    "server:restart": "restart server",
    "server:stop": "stop server",
    "server:recover": "recover server",
    "server:upgrade": "upgrade server",
}


def get_operation_spec(key: str) -> OperationSpec:
    spec = _REGISTRY.get(key)
    if spec is None:
        raise OperationNotFound(key)
    return spec


async def execute_operation(
    *,
    key: str,
    user_info: dict,
    params: Optional[dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    params = params or {}
    spec = get_operation_spec(key)

    actor_email = user_info.get("email", "")
    actor_name = user_info.get("name", "")
    actor_label = actor_email or actor_name or "unknown"
    actor_is_admin = is_admin(user_info)

    allowed, retry_after = check_rate_limit(
        bucket="operations",
        key=f"{actor_email}:{spec.key}",
        limit=10,
        window_seconds=60,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded. Retry after {retry_after}s")

    if spec.admin_only and not actor_is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    if spec.required_permission and not actor_is_admin:
        if not permissions_service.has_permission(actor_email, spec.required_permission):
            raise HTTPException(status_code=403, detail=f"Permission denied: {spec.required_permission}")

    ok, error = spec.preflight(user_info, params)
    if not ok:
        return {"success": False, "error": error or "Preflight failed"}

    guarded_action = _PROFILE_GUARDED_SERVER_ACTIONS.get(spec.key)
    if guarded_action:
        block = minecraft_settings.get_active_profile_operation_block(guarded_action)
        if block:
            return block

    normalized_idempotency_key = (idempotency_key or "").strip() or None
    idempotency_cache_key = ""
    now = time.time()

    if normalized_idempotency_key:
        idempotency_cache_key = f"{spec.key}:{actor_label}:{normalized_idempotency_key}"
        with _IDEMPOTENCY_LOCK:
            _cleanup_expired_idempotency_entries(now)
            existing_entry = _IDEMPOTENCY_CACHE.get(idempotency_cache_key)
            if existing_entry:
                if existing_entry.get("status") == "done":
                    cached_result = dict(existing_entry.get("result") or {"success": False, "error": "Unknown idempotency replay result"})
                    cached_result["idempotent_replay"] = True
                    return cached_result
                return {
                    "success": False,
                    "error": "Operation already in progress for this idempotency key",
                    "status": "in_progress",
                    "idempotent_replay": True,
                }

            _IDEMPOTENCY_CACHE[idempotency_cache_key] = {
                "status": "in_progress",
                "expires_at": now + _IDEMPOTENCY_TTL_SECONDS,
                "result": None,
            }

    op_id = str(uuid.uuid4())
    started_at = int(time.time())
    base_state: dict[str, Any] = {
        "op_key": spec.key,
        "op_id": op_id,
        "actor": actor_label,
        "idempotency_key": normalized_idempotency_key,
        "started_at": started_at,
    }
    _append_operation_state({
        **base_state,
        "finished_at": None,
        "status": "started",
        "error": "",
    })

    server_operation_lock: Optional[TextIO] = None
    if spec.key.startswith("server:"):
        server_operation_lock = _acquire_server_operation_lock()
        if server_operation_lock is None:
            finished_at = int(time.time())
            if _LAST_SERVER_OPERATION_LOCK_ERROR:
                busy_result = {
                    "success": False,
                    "error": f"Server operation lock unavailable: {_LAST_SERVER_OPERATION_LOCK_ERROR}",
                    "error_code": "server_operation_lock_error",
                }
            else:
                busy_result = {
                    "success": False,
                    "error": "Another server operation is already in progress",
                    "error_code": "server_operation_in_progress",
                }
            _append_operation_state({
                **base_state,
                "finished_at": finished_at,
                "status": "failed",
                "error": busy_result["error"],
            })
            if normalized_idempotency_key:
                with _IDEMPOTENCY_LOCK:
                    _IDEMPOTENCY_CACHE[idempotency_cache_key] = {
                        "status": "done",
                        "expires_at": finished_at + _IDEMPOTENCY_TTL_SECONDS,
                        "result": busy_result,
                    }
            return busy_result

    try:
        result = await spec.executor(user_info, params)
    except Exception as exc:
        finished_at = int(time.time())
        error_message = str(exc) or "Operation execution failed"
        failure_result = {"success": False, "error": error_message}
        _append_operation_state({
            **base_state,
            "finished_at": finished_at,
            "status": "failed",
            "error": error_message,
        })
        if normalized_idempotency_key:
            with _IDEMPOTENCY_LOCK:
                _IDEMPOTENCY_CACHE[idempotency_cache_key] = {
                    "status": "done",
                    "expires_at": finished_at + _IDEMPOTENCY_TTL_SECONDS,
                    "result": failure_result,
                }
        return failure_result
    finally:
        _release_server_operation_lock(server_operation_lock)

    finished_at = int(time.time())
    success = bool(result.get("success")) if isinstance(result, dict) else False
    error_message = ""
    if not success:
        if isinstance(result, dict):
            error_message = str(result.get("error", ""))
        else:
            error_message = "Operation execution failed"
    _append_operation_state({
        **base_state,
        "finished_at": finished_at,
        "status": "succeeded" if success else "failed",
        "error": error_message,
    })

    if normalized_idempotency_key:
        with _IDEMPOTENCY_LOCK:
            _IDEMPOTENCY_CACHE[idempotency_cache_key] = {
                "status": "done",
                "expires_at": finished_at + _IDEMPOTENCY_TTL_SECONDS,
                "result": result,
            }

    return result
