"""Conservative scheduled update checks for tracked Minecraft plugins."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, TextIO

from app.core.config import DATA_DIR
from app.services import minecraft_updater


CONFIG_FILE = DATA_DIR / "minecraft_update_automation_config.json"
LOG_FILE = DATA_DIR / "minecraft_update_automation_log.json"
INSTANCE_LOCK_FILE = DATA_DIR / "minecraft_update_automation.instance.lock"
CHECK_INTERVAL_SECONDS = 60


@dataclass
class UpdateAutomationConfig:
    enabled: bool = False
    check_interval_hours: int = 24
    check_hour: int = 4
    check_minute: int = 0
    auto_apply: bool = False
    restart_after_apply: bool = False
    restart_message_label: str = "Plugin Update"
    excluded_plugins: list[str] = field(default_factory=list)
    last_run_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UpdateAutomationConfig":
        config = cls()
        for key in cls.__dataclass_fields__:
            if key in data:
                setattr(config, key, data[key])
        return normalize_config(config)


@dataclass
class UpdateAutomationLog:
    timestamp: str
    action: str
    status: str
    details: str
    actor: Optional[str] = None
    updates_available: int = 0
    applied_plugins: list[str] = field(default_factory=list)
    skipped_plugins: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_pid: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UpdateAutomationLog":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _normalize_plugin_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized = []
    seen = set()
    for value in values:
        plugin_id = str(value or "").strip().lower()
        if not plugin_id or plugin_id in seen:
            continue
        seen.add(plugin_id)
        normalized.append(plugin_id)
    return normalized


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def normalize_config(config: UpdateAutomationConfig) -> UpdateAutomationConfig:
    config.enabled = bool(config.enabled)
    config.check_interval_hours = _bounded_int(config.check_interval_hours, 24, 1, 24 * 30)
    config.check_hour = _bounded_int(config.check_hour, 0, 0, 23)
    config.check_minute = _bounded_int(config.check_minute, 0, 0, 59)
    config.auto_apply = bool(config.auto_apply)
    config.restart_after_apply = bool(config.restart_after_apply)
    config.restart_message_label = str(config.restart_message_label or "Plugin Update").strip()[:48] or "Plugin Update"
    config.excluded_plugins = _normalize_plugin_ids(config.excluded_plugins)
    return config


def _default_config() -> UpdateAutomationConfig:
    return UpdateAutomationConfig()


def _load_config() -> UpdateAutomationConfig:
    if not CONFIG_FILE.exists():
        return _default_config()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _default_config()
    if not isinstance(payload, dict):
        return _default_config()
    return UpdateAutomationConfig.from_dict(payload)


def _save_config(config: UpdateAutomationConfig) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(normalize_config(config).to_dict(), handle, indent=2, ensure_ascii=False)
    temp_path.replace(CONFIG_FILE)


def get_config() -> dict[str, Any]:
    return _load_config().to_dict()


def update_config(**kwargs: Any) -> dict[str, Any]:
    config = _load_config()
    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)
    config = normalize_config(config)
    _save_config(config)
    _add_log("config_changed", "success", "Update automation configuration updated")
    return {"success": True, "config": config.to_dict()}


def _load_logs() -> list[UpdateAutomationLog]:
    if not LOG_FILE.exists():
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    logs = []
    for item in payload[-100:]:
        if isinstance(item, dict):
            logs.append(UpdateAutomationLog.from_dict(item))
    return logs


def _save_logs(logs: list[UpdateAutomationLog]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = LOG_FILE.with_suffix(LOG_FILE.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump([log.to_dict() for log in logs[-100:]], handle, indent=2, ensure_ascii=False)
    temp_path.replace(LOG_FILE)


def _add_log(
    action: str,
    status: str,
    details: str,
    *,
    actor: str | None = None,
    updates_available: int = 0,
    applied_plugins: Optional[list[str]] = None,
    skipped_plugins: Optional[list[str]] = None,
    errors: Optional[list[str]] = None,
) -> UpdateAutomationLog:
    logs = _load_logs()
    log = UpdateAutomationLog(
        timestamp=_now().isoformat(),
        action=action,
        status=status,
        details=details,
        actor=actor,
        updates_available=updates_available,
        applied_plugins=applied_plugins or [],
        skipped_plugins=skipped_plugins or [],
        errors=errors or [],
        source_pid=os.getpid(),
    )
    logs.append(log)
    _save_logs(logs)
    return log


def get_logs(limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 50), 100))
    return [log.to_dict() for log in _load_logs()[-safe_limit:]][::-1]


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _scheduled_anchor(now: datetime, config: UpdateAutomationConfig) -> datetime:
    anchor = now.replace(hour=config.check_hour, minute=config.check_minute, second=0, microsecond=0)
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor


def is_due(config: Optional[UpdateAutomationConfig] = None, now: Optional[datetime] = None) -> bool:
    config = config or _load_config()
    if not config.enabled:
        return False
    now = (now or _now()).replace(microsecond=0)
    anchor = _scheduled_anchor(now, config)
    last_run = _parse_iso(config.last_run_at)
    if last_run is None:
        return now >= anchor
    interval = timedelta(hours=config.check_interval_hours)
    return now >= anchor and now - last_run >= interval


def _serializable_update(update: minecraft_updater.UpdateCheck) -> dict[str, Any]:
    return {
        "plugin_id": update.plugin_id,
        "source": update.source,
        "current_version": update.current_version,
        "latest_version": update.latest_version,
        "has_update": update.has_update,
        "download_url": update.download_url,
        "filename": update.filename,
        "changelog": update.changelog[:500] if update.changelog else None,
        "current_full_version": update.current_full_version,
        "latest_full_version": update.latest_full_version,
    }


async def run_once(*, manual: bool = False, actor: str = "automation") -> dict[str, Any]:
    config = _load_config()
    if not manual and not is_due(config):
        return {"status": "skipped", "reason": "not_due", "config": config.to_dict()}

    excluded = set(config.excluded_plugins)
    results = await minecraft_updater.check_all_updates(excluded_plugins=excluded)
    updates = [_serializable_update(result) for result in results]
    available = [result for result in results if result.has_update]
    applied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    if config.auto_apply:
        for update in available:
            if update.plugin_id.lower() in excluded:
                skipped.append(update.plugin_id)
                continue
            if update.plugin_id.lower() == "paper":
                skipped.append(update.plugin_id)
                continue
            try:
                log = await minecraft_updater.apply_update(update.plugin_id, update)
                if log.status == "success":
                    applied.append(update.plugin_id)
                else:
                    errors.append(f"{update.plugin_id}: {log.error or 'update failed'}")
            except Exception as exc:
                errors.append(f"{update.plugin_id}: {exc}")

    restart_result = None
    if applied and config.restart_after_apply and not errors:
        from app.services import reboot_scheduler

        restart_result = await reboot_scheduler.get_scheduler().trigger_labeled_restart(
            reason="plugin_update",
            label=config.restart_message_label,
        )

    config.last_run_at = _now().isoformat()
    _save_config(config)
    status = "failed" if errors else "success"
    details = (
        f"{len(available)} update(s) available"
        if available
        else "No updates available"
    )
    if applied:
        details += f"; applied {len(applied)}"
    _add_log(
        "manual_run" if manual else "scheduled_run",
        status,
        details,
        actor=actor,
        updates_available=len(available),
        applied_plugins=applied,
        skipped_plugins=sorted(set(skipped) | excluded),
        errors=errors,
    )
    return {
        "status": status,
        "checked_at": config.last_run_at,
        "updates": updates,
        "updates_available": len(available),
        "applied_plugins": applied,
        "skipped_plugins": sorted(set(skipped) | excluded),
        "errors": errors,
        "restart": restart_result,
        "config": config.to_dict(),
    }


class UpdateAutomationScheduler:
    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock_handle: Optional[TextIO] = None

    def _acquire_lock(self) -> bool:
        if self._lock_handle is not None:
            return True
        handle: Optional[TextIO] = None
        try:
            INSTANCE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            handle = open(INSTANCE_LOCK_FILE, "a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "acquired_at": _now().isoformat()}))
            handle.flush()
            self._lock_handle = handle
            return True
        except BlockingIOError:
            if handle:
                handle.close()
            return False
        except Exception as exc:
            if handle:
                handle.close()
            _add_log("scheduler_lock_failed", "failed", f"Failed to acquire scheduler lock: {exc}")
            return False

    def _release_lock(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
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

    async def start(self) -> None:
        if self._running:
            return
        if not self._acquire_lock():
            _add_log("scheduler_standby", "info", "Another process owns update automation scheduler lock")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="minecraft-update-automation")
        _add_log("scheduler_start", "success", "Update automation scheduler started")

    async def stop(self) -> None:
        was_running = self._running
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._release_lock()
        if was_running:
            _add_log("scheduler_stop", "success", "Update automation scheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                config = _load_config()
                if is_due(config):
                    await run_once(manual=False, actor="scheduler")
            except Exception as exc:
                _add_log("scheduled_run", "failed", f"Scheduled update automation failed: {exc}", errors=[str(exc)])
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


_scheduler: Optional[UpdateAutomationScheduler] = None


def get_scheduler() -> UpdateAutomationScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = UpdateAutomationScheduler()
    return _scheduler


async def start_scheduler() -> None:
    await get_scheduler().start()


async def stop_scheduler() -> None:
    await get_scheduler().stop()
