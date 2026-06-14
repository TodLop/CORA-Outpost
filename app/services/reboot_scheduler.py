# app/services/reboot_scheduler.py
"""
Minecraft Server Automation Service

Implements automatic maintenance logic from Project Octopus:
1. Reboot: If 0 users for >6 hours → Execute stop and restart
2. Reboot: If uptime >12 hours (with users online) → Announce restart timer, then restart
3. Maintenance: Auto-delete CoreProtect logs older than configured days

Features:
- Configurable thresholds via admin panel
- In-game countdown warnings before restart
- CoreProtect log purge automation
- Detailed action logging with success/failure status
- Real-time status monitoring
"""

import asyncio
import fcntl
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List, TextIO
from enum import Enum

from app.core.config import DATA_DIR
from app.services import minecraft_server, minecraft_settings

# Configuration file path
CONFIG_FILE = DATA_DIR / "reboot_scheduler_config.json"
LOG_FILE = DATA_DIR / "reboot_scheduler_log.json"
INSTANCE_LOCK_FILE = DATA_DIR / "reboot_scheduler.instance.lock"
RESTART_READY_TIMEOUT_SEC = 120
RESTART_START_RETRIES = 2
RESTART_RETRY_DELAY_SEC = 3

# Module-level dedup guard for warnings (shared across instances in same process)
_last_warning_sent: Dict[str, float] = {}
_WARNING_DEDUP_WINDOW = 25  # seconds — same warning is suppressed within this window
MAINTENANCE_WARNING_MINUTES = [5, 1]
MAINTENANCE_WARNING_SECONDS = [30, 10]


class SchedulerState(str, Enum):
    """Current state of the scheduler"""
    DISABLED = "disabled"
    MONITORING = "monitoring"
    COUNTDOWN_EMPTY = "countdown_empty"      # Counting down for empty server restart
    COUNTDOWN_UPTIME = "countdown_uptime"    # Counting down for uptime-based restart
    RESTARTING = "restarting"
    ERROR = "error"


@dataclass
class SchedulerConfig:
    """Scheduler configuration"""
    enabled: bool = True

    # Trigger 1: Empty server restart
    empty_server_enabled: bool = True
    empty_hours_threshold: float = 6.0  # Restart if empty for this many hours

    # Trigger 2: Uptime-based restart
    uptime_restart_enabled: bool = True
    max_uptime_hours: float = 12.0  # Restart after this many hours of uptime

    # Countdown settings
    countdown_minutes: int = 5  # Warning time before restart
    warning_intervals: List[int] = field(default_factory=lambda: [5, 3, 1])  # Minutes to warn at

    # Post-restart grace period
    restart_grace_minutes: int = 30  # Skip auto-restart triggers for this long after a restart

    # One-time maintenance stop (auto-clears after execution/cancel)
    maintenance_stop_scheduled_at: Optional[str] = None

    # CoreProtect maintenance
    coreprotect_purge_enabled: bool = True
    coreprotect_retention_days: int = 30  # Delete logs older than this
    coreprotect_purge_hour: int = 4  # Hour of day to run purge (0-23, default 4 AM)
    coreprotect_last_purge: Optional[str] = None  # ISO timestamp of last purge

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SchedulerConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SchedulerStatus:
    """Current scheduler status"""
    state: SchedulerState = SchedulerState.DISABLED
    server_running: bool = False
    players_online: int = 0

    # Timing info
    server_started_at: Optional[str] = None
    uptime_seconds: int = 0
    uptime_formatted: str = "0h 0m"

    empty_since: Optional[str] = None
    empty_seconds: int = 0
    empty_formatted: str = "0h 0m"

    # Countdown info (when in countdown state)
    countdown_reason: Optional[str] = None
    countdown_remaining_seconds: int = 0
    countdown_formatted: str = ""
    next_warning_at: Optional[str] = None

    # Next action prediction
    next_action: Optional[str] = None
    next_action_at: Optional[str] = None

    # One-time maintenance stop status
    maintenance_scheduled_at: Optional[str] = None
    maintenance_remaining_seconds: int = 0
    maintenance_remaining_formatted: str = ""

    # CoreProtect purge status
    coreprotect_last_purge: Optional[str] = None
    coreprotect_next_purge: Optional[str] = None
    coreprotect_purge_running: bool = False

    last_check: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass
class ActionLog:
    """Log entry for scheduler actions"""
    timestamp: str
    action: str  # "restart_empty", "restart_uptime", "warning_sent", "config_changed", "error"
    status: str  # "success", "failed", "info"
    details: str
    trigger_reason: Optional[str] = None
    players_affected: int = 0
    source_pid: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ActionLog":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class RebootScheduler:
    """Manages automatic server restarts"""

    def __init__(self):
        self.config = SchedulerConfig()
        self.status = SchedulerStatus()
        self.logs: List[ActionLog] = []

        # Tracking state
        self._server_start_time: Optional[datetime] = None
        self._empty_since: Optional[datetime] = None
        self._last_player_count: int = 0
        self._countdown_start: Optional[datetime] = None
        self._countdown_target: Optional[datetime] = None
        self._warnings_sent: set = set()  # Track which warnings have been sent
        self._restart_token_seq: int = 0
        self._active_restart_token: Optional[int] = None
        self._active_restart_label: str = "Auto-Restart"
        self._maintenance_warnings_sent: set = set()
        self._maintenance_schedule_cursor: Optional[str] = None

        # Post-restart grace period tracking
        self._last_restart_completed_at: Optional[datetime] = None

        # Zombie process (process_no_port) auto-recovery
        self._degraded_since: Optional[datetime] = None
        self._DEGRADED_AUTO_RECOVER_SECONDS: int = 180  # 3 minutes

        # Background task
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._instance_lock_handle: Optional[TextIO] = None

        # Load saved config and logs
        self._load_config()
        self._load_logs()

    def _load_config(self):
        """Load configuration from file"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    self.config = SchedulerConfig.from_dict(data)
            except Exception as e:
                print(f"[RebootScheduler] Failed to load config: {e}")

    def _save_config(self):
        """Save configuration to file"""
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config.to_dict(), f, indent=2)
        except Exception as e:
            print(f"[RebootScheduler] Failed to save config: {e}")

    def _save_coreprotect_last_purge(self, purge_iso: str):
        """
        Save purge timestamp while preserving the latest enabled toggle from disk.
        This avoids stale in-memory config overwriting a newer external disable.
        """
        latest_enabled = self.config.enabled
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    disk_data = json.load(f)
                    if isinstance(disk_data, dict) and isinstance(disk_data.get("enabled"), bool):
                        latest_enabled = disk_data["enabled"]
            except Exception as e:
                print(f"[RebootScheduler] Failed to read config before purge save: {e}")

        self.config.coreprotect_last_purge = purge_iso
        self.config.enabled = latest_enabled
        self._save_config()

    def _load_logs(self):
        """Load recent logs from file"""
        if LOG_FILE.exists():
            try:
                with open(LOG_FILE, "r") as f:
                    data = json.load(f)
                    self.logs = [ActionLog.from_dict(log) for log in data[-100:] if isinstance(log, dict)]  # Keep last 100
            except Exception as e:
                print(f"[RebootScheduler] Failed to load logs: {e}")

    def _save_logs(self):
        """Save logs to file"""
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "w") as f:
                json.dump([log.to_dict() for log in self.logs[-100:]], f, indent=2)
        except Exception as e:
            print(f"[RebootScheduler] Failed to save logs: {e}")

    def _add_log(self, action: str, status: str, details: str,
                 trigger_reason: str = None, players_affected: int = 0):
        """Add a log entry"""
        log = ActionLog(
            timestamp=datetime.now().isoformat(),
            action=action,
            status=status,
            details=details,
            trigger_reason=trigger_reason,
            players_affected=players_affected,
            source_pid=os.getpid(),
        )
        self.logs.append(log)
        self._save_logs()
        print(f"[RebootScheduler] {action}: {details} ({status})")

    def _format_duration(self, seconds: int) -> str:
        """Format seconds as human-readable duration"""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}m"

    def _queue_background_command(self, command: str):
        """Best-effort async command dispatch from sync contexts."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(minecraft_server.send_command(command))

    def _sync_maintenance_warning_state(self):
        """Reset maintenance warning markers if schedule changed externally."""
        raw = self.config.maintenance_stop_scheduled_at
        if raw != self._maintenance_schedule_cursor:
            self._maintenance_schedule_cursor = raw
            self._maintenance_warnings_sent = set()

    def _clear_maintenance_status(self):
        self.status.maintenance_scheduled_at = None
        self.status.maintenance_remaining_seconds = 0
        self.status.maintenance_remaining_formatted = ""

    def _parse_maintenance_schedule_time(self) -> Optional[datetime]:
        raw = self.config.maintenance_stop_scheduled_at
        if not raw:
            return None

        try:
            parsed = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            self._add_log(
                "maintenance_schedule_invalid",
                "failed",
                f"Invalid maintenance schedule timestamp: {raw!r}. Clearing schedule.",
            )
            self.config.maintenance_stop_scheduled_at = None
            self._save_config()
            self._maintenance_schedule_cursor = None
            self._maintenance_warnings_sent = set()
            return None

        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed

    def _update_maintenance_status_fields(self, now: datetime) -> Optional[datetime]:
        scheduled_at = self._parse_maintenance_schedule_time()
        if not scheduled_at:
            self._clear_maintenance_status()
            return None

        remaining_seconds = max(0, int((scheduled_at - now).total_seconds()))
        self.status.maintenance_scheduled_at = scheduled_at.isoformat()
        self.status.maintenance_remaining_seconds = remaining_seconds
        self.status.maintenance_remaining_formatted = self._format_duration(remaining_seconds)
        return scheduled_at

    def _clear_maintenance_schedule(self, save: bool = True):
        self.config.maintenance_stop_scheduled_at = None
        if save:
            self._save_config()
        self._maintenance_schedule_cursor = None
        self._maintenance_warnings_sent = set()
        self._clear_maintenance_status()

    @staticmethod
    def _format_warning_time(seconds: int) -> str:
        if seconds >= 3600:
            hours = seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''}"
        if seconds >= 60:
            minutes = seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        return f"{seconds} seconds"

    def _new_restart_token(self) -> int:
        """Create a new token to invalidate stale restart/countdown operations."""
        self._restart_token_seq += 1
        self._active_restart_token = self._restart_token_seq
        return self._restart_token_seq

    def _is_active_restart_token(self, token: Optional[int]) -> bool:
        return token is not None and token == self._active_restart_token

    def _clear_restart_token(self):
        self._active_restart_token = None

    def _acquire_instance_lock(self) -> bool:
        """Acquire process-wide scheduler lock (cross-process singleton)."""
        if self._instance_lock_handle is not None:
            return True

        handle: Optional[TextIO] = None
        try:
            INSTANCE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            handle = open(INSTANCE_LOCK_FILE, "a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return False

            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({
                "pid": os.getpid(),
                "acquired_at": datetime.now().isoformat(),
            }, ensure_ascii=False))
            handle.flush()
            self._instance_lock_handle = handle
            return True
        except Exception as e:
            print(f"[RebootScheduler] Failed to acquire instance lock: {e}")
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            return False

    def _release_instance_lock(self):
        """Release process-wide scheduler lock."""
        handle = self._instance_lock_handle
        self._instance_lock_handle = None
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

    async def start(self):
        """Start the scheduler background task"""
        if self._running:
            return

        if not self._acquire_instance_lock():
            # Another app process owns scheduler monitoring.
            self.status.state = SchedulerState.DISABLED if not self.config.enabled else SchedulerState.MONITORING
            self.status.next_action = "Passive mode (active in another process)"
            self.status.next_action_at = None
            self.status.error_message = None
            self._add_log(
                "scheduler_standby",
                "info",
                "Another process owns reboot scheduler lock; monitor loop not started",
            )
            return

        # Cancel any orphaned monitor task from a prior instance
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._add_log("scheduler_start", "success", "Reboot scheduler started")

    async def stop(self):
        """Stop the scheduler"""
        was_running = self._running
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        self._release_instance_lock()
        if was_running:
            self._add_log("scheduler_stop", "success", "Reboot scheduler stopped")

    def update_config(self, **kwargs) -> dict:
        """Update scheduler configuration"""
        # Pull latest disk config first to avoid stale in-memory overwrites
        # when multiple app processes are running.
        self._load_config()
        old_enabled = self.config.enabled

        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)

        self._save_config()

        # Log the change
        changes = ", ".join(f"{k}={v}" for k, v in kwargs.items())
        self._add_log("config_changed", "success", f"Configuration updated: {changes}")

        return {"success": True, "config": self.config.to_dict()}

    def get_config(self) -> dict:
        """Get current configuration"""
        self._load_config()
        return self.config.to_dict()

    def _update_realtime_status(self):
        """Update status with realtime calculations (called on API request)"""
        now = datetime.now()
        self.status.last_check = now.isoformat()
        self._sync_maintenance_warning_state()

        # Get current server status
        server_status = minecraft_server.get_server_status()
        self.status.server_running = server_status.running
        self.status.players_online = server_status.players_online if server_status.running else 0

        # Bootstrap uptime tracking on-demand.
        # This keeps admin UI metrics populated even if monitor-loop priming is delayed.
        if server_status.running and self._server_start_time is None:
            os_start_time = None
            if server_status.pid:
                os_start_time = minecraft_server._manager._get_process_start_time(server_status.pid)
            self._server_start_time = os_start_time or now

        # Calculate uptime if server is running and we have a start time
        if server_status.running and self._server_start_time:
            uptime = now - self._server_start_time
            self.status.uptime_seconds = int(uptime.total_seconds())
            self.status.uptime_formatted = self._format_duration(self.status.uptime_seconds)
            self.status.server_started_at = self._server_start_time.isoformat()

        # Calculate empty time if applicable
        if self._empty_since and self.status.players_online == 0:
            empty_time = now - self._empty_since
            self.status.empty_seconds = int(empty_time.total_seconds())
            self.status.empty_formatted = self._format_duration(self.status.empty_seconds)
            self.status.empty_since = self._empty_since.isoformat()
        elif self.status.players_online == 0 and server_status.running and self._server_start_time:
            # Display-only fallback when monitor loop hasn't set _empty_since yet.
            # Keep scheduler trigger semantics unchanged by not mutating _empty_since here.
            empty_time = now - self._server_start_time
            self.status.empty_seconds = int(empty_time.total_seconds())
            self.status.empty_formatted = self._format_duration(self.status.empty_seconds)
            self.status.empty_since = self._server_start_time.isoformat()

        # Update countdown remaining if in countdown state
        if self._countdown_target and self.status.state in [SchedulerState.COUNTDOWN_EMPTY, SchedulerState.COUNTDOWN_UPTIME]:
            remaining = (self._countdown_target - now).total_seconds()
            self.status.countdown_remaining_seconds = max(0, int(remaining))
            self.status.countdown_formatted = self._format_duration(self.status.countdown_remaining_seconds)

        self._update_maintenance_status_fields(now)

    def get_status(self) -> dict:
        """Get current scheduler status (with realtime update)"""
        self._load_config()
        self._update_realtime_status()
        return self.status.to_dict()

    def get_logs(self, limit: int = 50) -> List[dict]:
        """Get recent action logs"""
        return [log.to_dict() for log in self.logs[-limit:]][::-1]  # Newest first

    async def _monitor_loop(self):
        """Main monitoring loop - runs every 30 seconds"""
        print("[RebootScheduler] Monitor loop started")

        while self._running:
            # Orphan detection: exit if a newer scheduler instance has replaced us
            if _scheduler is not None and _scheduler is not self:
                print("[RebootScheduler] Monitor loop exiting (orphaned instance)")
                self._running = False
                return

            try:
                await self._check_and_act()
            except Exception as e:
                self.status.state = SchedulerState.ERROR
                self.status.error_message = str(e)
                self._add_log("error", "failed", f"Monitor error: {e}")

            await asyncio.sleep(30)  # Check every 30 seconds

    async def _check_and_act(self):
        """Check server status and take action if needed"""
        # Refresh config every tick to pick up changes made by other processes.
        self._load_config()
        self._sync_maintenance_warning_state()

        now = datetime.now()
        self.status.last_check = now.isoformat()

        # Get server status
        server_status = minecraft_server.get_server_status()
        self.status.server_running = server_status.running
        self.status.players_online = server_status.players_online if server_status.running else 0

        # Handle disabled state early: skip all automation actions.
        if not self.config.enabled:
            self._clear_restart_token()
            self._countdown_start = None
            self._countdown_target = None
            self._warnings_sent = set()
            self.status.state = SchedulerState.DISABLED
            self.status.next_action = None
            self.status.next_action_at = None
            self.status.countdown_reason = None
            self.status.countdown_remaining_seconds = 0
            self.status.countdown_formatted = ""
            if await self._handle_scheduled_maintenance_stop(now, backup_busy=False):
                return
            return

        # Sync grace reference with shared restart state from minecraft_server
        try:
            restart_gate = minecraft_server.get_restart_gate_status()
            shared_restart_iso = restart_gate.get("last_restart_completed_at")
            if shared_restart_iso:
                shared_restart_at = datetime.fromisoformat(shared_restart_iso)
                if (
                    self._last_restart_completed_at is None
                    or shared_restart_at > self._last_restart_completed_at
                ):
                    self._last_restart_completed_at = shared_restart_at
        except Exception:
            pass

        # ----- Zombie process auto-recovery (process_no_port) -----
        if server_status.state_reason == "process_no_port":
            if self._degraded_since is None:
                self._degraded_since = now
                self._add_log("degraded_detected", "info",
                              "Server entered process_no_port state, monitoring...")
            elif (now - self._degraded_since).total_seconds() >= self._DEGRADED_AUTO_RECOVER_SECONDS:
                elapsed = int((now - self._degraded_since).total_seconds())
                self._add_log("auto_recover", "info",
                              f"Server stuck in process_no_port for {elapsed}s, auto-recovering")
                try:
                    result = await minecraft_server.recover_server()
                    if result.get("success"):
                        self._add_log("auto_recover", "success", "Auto-recovery completed successfully")
                        self._last_restart_completed_at = datetime.now()
                    else:
                        self._add_log("auto_recover", "failed",
                                      f"Auto-recovery failed: {result.get('error')}")
                except Exception as e:
                    self._add_log("auto_recover", "failed", f"Auto-recovery exception: {e}")
                self._degraded_since = None
                return
            # Still waiting for auto-recover threshold
            remaining = self._DEGRADED_AUTO_RECOVER_SECONDS - int((now - self._degraded_since).total_seconds())
            self.status.state = SchedulerState.MONITORING
            self.status.next_action = f"Server degraded, auto-recover in {self._format_duration(remaining)}"
            return
        else:
            self._degraded_since = None

        # Check CoreProtect purge (runs independently of reboot scheduler)
        await self._check_coreprotect_purge()

        backup_busy = False
        try:
            from app.services.backup_scheduler import get_backup_scheduler
            backup = get_backup_scheduler()
            backup_state = backup.status.state.value if hasattr(backup.status.state, 'value') else str(backup.status.state)
            if backup_state not in ("disabled", "monitoring"):
                backup_busy = True
        except Exception:
            pass

        # Handle server not running
        if not server_status.running:
            self.status.state = SchedulerState.MONITORING
            self._reset_tracking()
            if await self._handle_scheduled_maintenance_stop(now, backup_busy=backup_busy):
                return
            self.status.next_action = "Waiting for server to start"
            return

        # Track server start time (use OS process start time for accuracy)
        # Keep this before grace-period return so uptime/empty metrics stay visible during grace.
        if self._server_start_time is None:
            # Try to get actual process start time from the OS
            # This survives run.py restarts since it reads from the Java process itself
            server_status_obj = minecraft_server.get_server_status()
            pid = server_status_obj.pid
            os_start_time = None
            if pid:
                os_start_time = minecraft_server._manager._get_process_start_time(pid)

            if os_start_time:
                self._server_start_time = os_start_time
                uptime_so_far = self._format_duration(int((now - os_start_time).total_seconds()))
                self._add_log(
                    "server_detected",
                    "info",
                    f"Server running detected (OS start: {os_start_time.strftime('%H:%M:%S')}, uptime: {uptime_so_far})",
                )
            else:
                self._server_start_time = now
                self._add_log(
                    "server_detected",
                    "info",
                    "Server running detected, starting tracking (OS start time unavailable)",
                )

        # Calculate uptime
        uptime = now - self._server_start_time
        self.status.uptime_seconds = int(uptime.total_seconds())
        self.status.uptime_formatted = self._format_duration(self.status.uptime_seconds)
        self.status.server_started_at = self._server_start_time.isoformat()

        # Track empty time
        if self.status.players_online == 0:
            if self._empty_since is None:
                self._empty_since = now
            empty_time = now - self._empty_since
            self.status.empty_seconds = int(empty_time.total_seconds())
            self.status.empty_formatted = self._format_duration(self.status.empty_seconds)
            self.status.empty_since = self._empty_since.isoformat()
        else:
            self._empty_since = None
            self.status.empty_seconds = 0
            self.status.empty_formatted = "0s"
            self.status.empty_since = None

        # ----- Post-restart grace period -----
        if self._last_restart_completed_at:
            grace_elapsed = (now - self._last_restart_completed_at).total_seconds()
            grace_seconds = self.config.restart_grace_minutes * 60
            if grace_elapsed < grace_seconds:
                if await self._handle_scheduled_maintenance_stop(now, backup_busy=backup_busy):
                    return
                self.status.state = SchedulerState.MONITORING
                remaining = int(grace_seconds - grace_elapsed)
                self.status.next_action = f"Post-restart grace period ({self._format_duration(remaining)} remaining)"
                return
            else:
                # Grace period expired, clear it
                self._last_restart_completed_at = None

        # Check if we're in a countdown
        if self.status.state in [SchedulerState.COUNTDOWN_EMPTY, SchedulerState.COUNTDOWN_UPTIME]:
            await self._handle_countdown(now)
            return

        if await self._handle_scheduled_maintenance_stop(now, backup_busy=backup_busy):
            return

        if backup_busy:
            self.status.state = SchedulerState.MONITORING
            self.status.next_action = "Backup in progress"
            self.status.next_action_at = None
            return

        # Check triggers
        self.status.state = SchedulerState.MONITORING

        # Trigger 1: Empty server for too long
        if self.config.empty_server_enabled and self._empty_since:
            empty_hours = self.status.empty_seconds / 3600
            if empty_hours >= self.config.empty_hours_threshold:
                await self._start_countdown("empty", f"Server empty for {self._format_duration(self.status.empty_seconds)}")
                return
            else:
                remaining = (self.config.empty_hours_threshold * 3600) - self.status.empty_seconds
                self.status.next_action = f"Empty server restart in {self._format_duration(int(remaining))}"
                self.status.next_action_at = (now + timedelta(seconds=remaining)).isoformat()

        # Trigger 2: Uptime too long (only if players online)
        if self.config.uptime_restart_enabled and self.status.players_online > 0:
            uptime_hours = self.status.uptime_seconds / 3600
            if uptime_hours >= self.config.max_uptime_hours:
                await self._start_countdown("uptime", f"Server uptime {self._format_duration(self.status.uptime_seconds)}")
                return
            else:
                remaining = (self.config.max_uptime_hours * 3600) - self.status.uptime_seconds
                if self.status.next_action is None or "uptime" not in self.status.next_action.lower():
                    self.status.next_action = f"Uptime restart in {self._format_duration(int(remaining))}"
                    self.status.next_action_at = (now + timedelta(seconds=remaining)).isoformat()

    async def _send_maintenance_warning(self, warning_seconds: int) -> bool:
        if warning_seconds <= 0:
            return True

        if not self.status.server_running or self.status.players_online <= 0:
            return False

        schedule_at = self.config.maintenance_stop_scheduled_at or "unknown"
        dedup_key = f"maintenance_warning_{schedule_at}_{warning_seconds}"
        now_ts = time.time()
        if now_ts - _last_warning_sent.get(dedup_key, 0) < _WARNING_DEDUP_WINDOW:
            return True

        time_str = self._format_warning_time(warning_seconds)
        title_cmd = 'title @a title {"text":"⚠ SERVER MAINTENANCE","color":"red","bold":true}'
        subtitle_cmd = f'title @a subtitle {{"text":"shutdown in {time_str}","color":"gold"}}'
        chat_cmd = f'say §c[Maintenance] §fServer will shut down in {time_str}. Please logout safely.'

        try:
            title_result = await minecraft_server.send_command(title_cmd)
            subtitle_result = await minecraft_server.send_command(subtitle_cmd)
            chat_result = await minecraft_server.send_command(chat_cmd)

            failed_steps = []
            if not title_result.get("success"):
                failed_steps.append(f"title: {title_result.get('error', 'unknown error')}")
            if not subtitle_result.get("success"):
                failed_steps.append(f"subtitle: {subtitle_result.get('error', 'unknown error')}")
            if not chat_result.get("success"):
                failed_steps.append(f"chat: {chat_result.get('error', 'unknown error')}")
            if failed_steps:
                raise RuntimeError("; ".join(failed_steps))

            _last_warning_sent[dedup_key] = now_ts
            self._add_log(
                "maintenance_warning_sent",
                "success",
                f"Maintenance shutdown warning sent: {time_str}",
                players_affected=self.status.players_online,
            )
            return True
        except Exception as e:
            self._add_log("maintenance_warning_sent", "failed", f"Failed to send maintenance warning: {e}")
            return False

    async def _execute_maintenance_stop(self) -> bool:
        players = self.status.players_online
        self._add_log(
            "maintenance_stop_started",
            "info",
            "Executing scheduled one-time maintenance stop",
            players_affected=players,
        )

        try:
            if players > 0:
                await minecraft_server.send_command('say §c[Maintenance] §fServer is shutting down now.')
                await asyncio.sleep(2)

            result = await minecraft_server.stop_server(force=False)
            if result.get("success"):
                self._add_log("maintenance_stop_completed", "success", "Scheduled maintenance stop completed")
                self._clear_maintenance_schedule(save=True)
                self._reset_tracking()
                self.status.state = SchedulerState.MONITORING
                self.status.error_message = None
                return True

            error = str(result.get("error", "Unknown error"))
            if "not running" in error.lower():
                self._add_log(
                    "maintenance_stop_completed",
                    "info",
                    "Scheduled maintenance reached target time, server was already offline",
                )
                self._clear_maintenance_schedule(save=True)
                self._reset_tracking()
                self.status.state = SchedulerState.MONITORING
                self.status.error_message = None
                return True

            self._add_log(
                "maintenance_stop_failed",
                "failed",
                f"Scheduled maintenance stop failed: {error}",
                players_affected=players,
            )
            self.status.error_message = error
            return False
        except Exception as e:
            self._add_log("maintenance_stop_failed", "failed", f"Scheduled maintenance stop exception: {e}")
            self.status.error_message = str(e)
            return False

    async def _handle_scheduled_maintenance_stop(self, now: datetime, backup_busy: bool) -> bool:
        scheduled_at = self._update_maintenance_status_fields(now)
        if scheduled_at is None:
            return False

        remaining_seconds = int((scheduled_at - now).total_seconds())
        clamped = max(0, remaining_seconds)
        self.status.next_action = f"Scheduled stop in {self._format_duration(clamped)}"
        self.status.next_action_at = scheduled_at.isoformat()

        if remaining_seconds > 0:
            for minute in MAINTENANCE_WARNING_MINUTES:
                marker = f"{minute}m"
                if marker in self._maintenance_warnings_sent:
                    continue
                threshold_seconds = minute * 60
                if remaining_seconds <= threshold_seconds:
                    if await self._send_maintenance_warning(threshold_seconds):
                        self._maintenance_warnings_sent.add(marker)

            for second in MAINTENANCE_WARNING_SECONDS:
                marker = f"{second}s"
                if marker in self._maintenance_warnings_sent:
                    continue
                if remaining_seconds <= second:
                    if await self._send_maintenance_warning(second):
                        self._maintenance_warnings_sent.add(marker)
            return True

        if not self.status.server_running:
            self._add_log(
                "maintenance_stop_completed",
                "info",
                "Scheduled maintenance stop reached target time, server already offline",
            )
            self._clear_maintenance_schedule(save=True)
            self.status.next_action = "Scheduled stop completed (server offline)"
            self.status.next_action_at = None
            return True

        if backup_busy:
            self.status.next_action = "Scheduled stop pending (backup in progress)"
            self.status.next_action_at = None
            return True

        success = await self._execute_maintenance_stop()
        if success:
            self.status.next_action = "Scheduled stop completed"
            self.status.next_action_at = None
        else:
            self.status.next_action = "Scheduled stop failed (retrying)"
            self.status.next_action_at = (now + timedelta(seconds=30)).isoformat()
        return True

    async def _start_countdown(self, reason: str, details: str, label: Optional[str] = None) -> bool:
        """Start countdown for restart"""
        now = datetime.now()
        token = self._new_restart_token()
        if label:
            self._active_restart_label = str(label).strip()[:48] or "Auto-Restart"
        restart_gate = minecraft_server.get_restart_gate_status()
        if not restart_gate.get("can_restart", True):
            retry_after = int(restart_gate.get("cooldown_remaining_seconds", 0) or 0)
            if restart_gate.get("in_progress"):
                blocked_reason = "Restart precheck blocked: another restart is in progress"
            elif retry_after > 0:
                blocked_reason = f"Restart precheck blocked: cooldown active ({retry_after}s remaining)"
            else:
                blocked_reason = "Restart precheck blocked: restart guard active"
            self._add_log(
                "restart_skipped",
                "info",
                blocked_reason,
                trigger_reason=reason,
                players_affected=self.status.players_online,
            )
            self._reset_tracking()
            self.status.state = SchedulerState.MONITORING
            self.status.error_message = None
            return False

        if reason == "empty":
            self.status.state = SchedulerState.COUNTDOWN_EMPTY
            self.status.countdown_reason = "Empty server threshold reached"
            # For empty server, do immediate restart (no players to warn)
            self._countdown_target = now
            self._add_log("restart_triggered", "info",
                         f"Empty server restart triggered: {details}",
                         trigger_reason=reason)
            restarted = await self._execute_restart(reason, token=token)
            return restarted
        else:
            self.status.state = SchedulerState.COUNTDOWN_UPTIME
            self.status.countdown_reason = "Uptime threshold reached"
            self._countdown_start = now
            self._countdown_target = now + timedelta(minutes=self.config.countdown_minutes)
            self._warnings_sent = set()

            self._add_log("countdown_started", "info",
                         f"Restart countdown started ({self.config.countdown_minutes}min): {details}",
                         trigger_reason=reason,
                         players_affected=self.status.players_online)

            # Send initial warning
            await self._send_warning(self.config.countdown_minutes)
            if self.config.countdown_minutes in self.config.warning_intervals:
                self._warnings_sent.add(self.config.countdown_minutes)
            return True

    async def _handle_countdown(self, now: datetime):
        """Handle countdown state - send warnings and execute restart"""
        token = self._active_restart_token
        if token is None:
            self._add_log("countdown_skipped", "info", "Skipping stale countdown (token missing)")
            self._reset_tracking()
            self.status.state = SchedulerState.MONITORING
            return

        if self._countdown_target is None:
            self.status.state = SchedulerState.MONITORING
            return

        remaining = (self._countdown_target - now).total_seconds()
        self.status.countdown_remaining_seconds = max(0, int(remaining))
        self.status.countdown_formatted = self._format_duration(self.status.countdown_remaining_seconds)

        # Check if countdown complete
        if remaining <= 0:
            await self._execute_restart(
                "empty" if self.status.state == SchedulerState.COUNTDOWN_EMPTY else "uptime",
                token=token,
            )
            return

        # Send warnings at configured intervals
        remaining_minutes = remaining / 60
        for warning_minute in self.config.warning_intervals:
            if warning_minute not in self._warnings_sent and remaining_minutes <= warning_minute:
                await self._send_warning(warning_minute)
                self._warnings_sent.add(warning_minute)

        # 30 second and 10 second warnings
        if remaining <= 30 and "30s" not in self._warnings_sent:
            await self._send_warning(0.5)  # 30 seconds
            self._warnings_sent.add("30s")
        if remaining <= 10 and "10s" not in self._warnings_sent:
            await self._send_warning(0.17)  # 10 seconds
            self._warnings_sent.add("10s")

    async def _send_warning(self, minutes: float):
        """Send in-game warning to players"""
        # Cross-instance dedup: skip if same warning was sent recently
        dedup_key = f"warning_{self._active_restart_token}_{minutes}_{self.status.countdown_reason or 'none'}"
        now_ts = time.time()
        if now_ts - _last_warning_sent.get(dedup_key, 0) < _WARNING_DEDUP_WINDOW:
            return

        if minutes >= 1:
            time_str = f"{int(minutes)} minute{'s' if minutes != 1 else ''}"
        else:
            seconds = int(minutes * 60)
            time_str = f"{seconds} seconds"

        label = self._active_restart_label or "Auto-Restart"

        # Send title (big text on screen)
        title_text = json.dumps(f"⚠ {label}", ensure_ascii=False)
        title_cmd = f'title @a title {{"text":{title_text},"color":"gold","bold":true}}'
        subtitle_cmd = f'title @a subtitle {{"text":"in {time_str}","color":"yellow"}}'

        # Send chat message
        chat_cmd = f'say §6[{label}] §eServer will restart in {time_str}. Please find a safe spot!'

        try:
            title_result = await minecraft_server.send_command(title_cmd)
            subtitle_result = await minecraft_server.send_command(subtitle_cmd)
            chat_result = await minecraft_server.send_command(chat_cmd)

            failed_steps = []
            if not title_result.get("success"):
                failed_steps.append(f"title: {title_result.get('error', 'unknown error')}")
            if not subtitle_result.get("success"):
                failed_steps.append(f"subtitle: {subtitle_result.get('error', 'unknown error')}")
            if not chat_result.get("success"):
                failed_steps.append(f"chat: {chat_result.get('error', 'unknown error')}")
            if failed_steps:
                raise RuntimeError("; ".join(failed_steps))

            _last_warning_sent[dedup_key] = now_ts
            self._add_log("warning_sent", "success",
                         f"Restart warning sent: {time_str}",
                         players_affected=self.status.players_online)
        except Exception as e:
            self._add_log("warning_sent", "failed", f"Failed to send warning: {e}")

    async def _execute_restart(self, reason: str, token: Optional[int] = None) -> bool:
        """Execute the actual server restart"""
        if token is None:
            token = self._active_restart_token
        if not self._is_active_restart_token(token):
            self._add_log("restart_skipped", "info", f"Skipping stale restart request (reason: {reason})")
            return False

        restart_gate = minecraft_server.get_restart_gate_status()
        if not restart_gate.get("can_restart", True):
            retry_after = int(restart_gate.get("cooldown_remaining_seconds", 0) or 0)
            if restart_gate.get("in_progress"):
                gate_reason = "another restart is in progress"
            elif retry_after > 0:
                gate_reason = f"cooldown active ({retry_after}s remaining)"
            else:
                gate_reason = "restart guard active"
            self._add_log(
                "restart_skipped",
                "info",
                f"Restart skipped before execution: {gate_reason}",
                trigger_reason=reason,
                players_affected=self.status.players_online,
            )
            self._reset_tracking()
            self.status.state = SchedulerState.MONITORING
            self.status.error_message = None
            return False

        self.status.state = SchedulerState.RESTARTING
        players = self.status.players_online

        self._add_log("restart_started", "info",
                     f"Executing restart (reason: {reason})",
                     trigger_reason=reason,
                     players_affected=players)

        try:
            # Send final message
            if players > 0:
                label = self._active_restart_label or "Auto-Restart"
                await minecraft_server.send_command(f'say §c[{label}] §fRestarting now! See you soon!')
                await asyncio.sleep(2)  # Give players time to see the message

            if not self._is_active_restart_token(token):
                self._add_log("restart_skipped", "info", f"Restart aborted by newer request (reason: {reason})")
                self.status.state = SchedulerState.MONITORING
                return False

            # Execute restart
            restart_source = "manual_scheduler" if reason == "manual" else "auto_scheduler"
            result = await minecraft_server.restart_server(
                ready_timeout_sec=RESTART_READY_TIMEOUT_SEC,
                require_rcon_ready=True,
                start_retries=RESTART_START_RETRIES,
                retry_delay_sec=RESTART_RETRY_DELAY_SEC,
                source=restart_source,
            )

            if result.get("success"):
                attempts = result.get("restart_start_attempt", 1)
                retry_note = f" (start retry {attempts - 1})" if attempts and attempts > 1 else ""
                self._add_log("restart_completed", "success",
                             f"Server restart completed successfully (was {reason}){retry_note}",
                             trigger_reason=reason,
                             players_affected=players)

                # Reset tracking and start grace period
                self._reset_tracking()
                self._server_start_time = datetime.now()  # Will be accurate after server starts
                self._last_restart_completed_at = datetime.now()  # Start grace period
                return True

            else:
                error = result.get("error", "Unknown error")
                error_code = result.get("error_code")
                if error_code in {"restart_in_progress", "restart_cooldown"}:
                    retry_after = result.get("retry_after_seconds")
                    retry_hint = f" (retry after {retry_after}s)" if retry_after else ""
                    self._add_log(
                        "restart_skipped",
                        "info",
                        f"Restart skipped: {error}{retry_hint}",
                        trigger_reason=reason,
                        players_affected=players,
                    )
                    self._reset_tracking()
                    self.status.state = SchedulerState.MONITORING
                    self.status.error_message = None
                    return False
                else:
                    if error_code:
                        error = f"{error} [{error_code}]"
                    self._add_log("restart_failed", "failed",
                                 f"Restart failed: {error}",
                                 trigger_reason=reason,
                                 players_affected=players)
                    self._reset_tracking()
                    self.status.state = SchedulerState.ERROR
                    self.status.error_message = error
                    return False

        except Exception as e:
            self._add_log("restart_failed", "failed", f"Restart exception: {e}",
                         trigger_reason=reason)
            self._reset_tracking()
            self.status.state = SchedulerState.ERROR
            self.status.error_message = str(e)
            return False

    def _reset_tracking(self):
        """Reset tracking state"""
        self._server_start_time = None
        self._empty_since = None
        self._countdown_start = None
        self._countdown_target = None
        self._warnings_sent = set()
        self._clear_restart_token()
        self._active_restart_label = "Auto-Restart"
        self.status.countdown_reason = None
        self.status.countdown_remaining_seconds = 0
        self.status.countdown_formatted = ""

    async def trigger_manual_restart(self, reason: str = "manual") -> dict:
        """Manually trigger a restart with countdown"""
        if not self.status.server_running:
            return {"success": False, "error": "Server is not running"}

        if self.status.state in [SchedulerState.COUNTDOWN_EMPTY,
                                  SchedulerState.COUNTDOWN_UPTIME,
                                  SchedulerState.RESTARTING]:
            return {"success": False, "error": f"Already in {self.status.state.value} state"}

        self._add_log("manual_restart", "info",
                     f"Manual restart triggered by admin",
                     trigger_reason=reason,
                     players_affected=self.status.players_online)

        if self.status.players_online > 0:
            # Start countdown with warnings
            started = await self._start_countdown("uptime", "Manual restart requested")
            if started:
                return {"success": True, "message": f"Restart countdown started ({self.config.countdown_minutes} minutes)"}
            restart_gate = minecraft_server.get_restart_gate_status()
            retry_after = int(restart_gate.get("cooldown_remaining_seconds", 0) or 0)
            if restart_gate.get("in_progress"):
                return {"success": False, "error": "Another restart is already in progress"}
            if retry_after > 0:
                return {"success": False, "error": f"Restart cooldown active. Retry after {retry_after}s"}
            return {"success": False, "error": "Restart precheck blocked by restart guard"}
        else:
            # Immediate restart for empty server
            token = self._new_restart_token()
            restarted = await self._execute_restart("manual", token=token)
            if restarted:
                return {"success": True, "message": "Restart executed (no players online)"}
            restart_gate = minecraft_server.get_restart_gate_status()
            retry_after = int(restart_gate.get("cooldown_remaining_seconds", 0) or 0)
            if restart_gate.get("in_progress"):
                return {"success": False, "error": "Another restart is already in progress"}
            if retry_after > 0:
                return {"success": False, "error": f"Restart cooldown active. Retry after {retry_after}s"}
            if self.status.error_message:
                return {"success": False, "error": self.status.error_message}
            return {"success": False, "error": "Restart was skipped by scheduler guard"}

    async def trigger_labeled_restart(self, reason: str, label: str) -> dict:
        """Trigger a guarded restart using scheduler warning UX with a custom label."""
        self._update_realtime_status()
        safe_label = str(label or "Plugin Update").strip()[:48] or "Plugin Update"
        safe_reason = str(reason or "manual").strip()[:48] or "manual"
        if not self.status.server_running:
            return {"success": False, "error": "Server is not running"}

        if self.status.state in [SchedulerState.COUNTDOWN_EMPTY,
                                  SchedulerState.COUNTDOWN_UPTIME,
                                  SchedulerState.RESTARTING]:
            return {"success": False, "error": f"Already in {self.status.state.value} state"}

        self._active_restart_label = safe_label
        self._add_log(
            "labeled_restart",
            "info",
            f"{safe_label} restart requested",
            trigger_reason=safe_reason,
            players_affected=self.status.players_online,
        )

        if self.status.players_online > 0:
            started = await self._start_countdown(safe_reason, f"{safe_label} restart requested", label=safe_label)
            if started:
                return {"success": True, "message": f"{safe_label} restart countdown started"}
            return {"success": False, "error": "Restart precheck blocked by restart guard"}

        token = self._new_restart_token()
        self._active_restart_label = safe_label
        restarted = await self._execute_restart(safe_reason, token=token)
        if restarted:
            return {"success": True, "message": f"{safe_label} restart executed"}
        if self.status.error_message:
            return {"success": False, "error": self.status.error_message}
        return {"success": False, "error": "Restart was skipped by scheduler guard"}

    def cancel_countdown(self) -> dict:
        """Cancel an active countdown"""
        if self.status.state not in [SchedulerState.COUNTDOWN_EMPTY, SchedulerState.COUNTDOWN_UPTIME]:
            return {"success": False, "error": "No countdown active"}

        self._add_log("countdown_cancelled", "info",
                     f"Countdown cancelled by admin",
                     players_affected=self.status.players_online)

        self._countdown_start = None
        self._countdown_target = None
        self._warnings_sent = set()
        self._clear_restart_token()
        self._active_restart_label = "Auto-Restart"
        self.status.state = SchedulerState.MONITORING
        self.status.countdown_reason = None
        self.status.countdown_remaining_seconds = 0

        # Notify players
        self._queue_background_command('say §a[Auto-Restart] §fRestart has been cancelled!')

        return {"success": True, "message": "Countdown cancelled"}

    def schedule_maintenance_stop(self, scheduled_at: str) -> dict:
        """Schedule a one-time server stop at an absolute datetime."""
        block = minecraft_settings.get_active_profile_operation_block("schedule maintenance stop")
        if block:
            return block

        self._load_config()
        self._sync_maintenance_warning_state()

        if not isinstance(scheduled_at, str) or not scheduled_at.strip():
            return {"success": False, "error": "scheduled_at is required"}

        if self.status.state in [SchedulerState.COUNTDOWN_EMPTY, SchedulerState.COUNTDOWN_UPTIME, SchedulerState.RESTARTING]:
            return {"success": False, "error": f"Cannot schedule stop while scheduler is {self.status.state.value}"}

        try:
            target = datetime.fromisoformat(scheduled_at.strip())
        except (TypeError, ValueError):
            return {"success": False, "error": "Invalid datetime format. Use ISO 8601."}

        if target.tzinfo is not None:
            target = target.astimezone().replace(tzinfo=None)

        if target <= datetime.now() + timedelta(seconds=30):
            return {"success": False, "error": "Schedule time must be at least 30 seconds in the future"}

        self.config.maintenance_stop_scheduled_at = target.isoformat()
        self._save_config()
        self._maintenance_schedule_cursor = self.config.maintenance_stop_scheduled_at
        self._maintenance_warnings_sent = set()
        self._update_maintenance_status_fields(datetime.now())

        self._add_log(
            "maintenance_scheduled",
            "success",
            f"One-time maintenance stop scheduled at {target.isoformat()}",
        )
        return {
            "success": True,
            "message": "One-time maintenance stop scheduled",
            "scheduled_at": target.isoformat(),
            "config": self.config.to_dict(),
        }

    def cancel_maintenance_stop(self) -> dict:
        """Cancel a scheduled one-time maintenance stop."""
        self._load_config()
        if not self.config.maintenance_stop_scheduled_at:
            return {"success": False, "error": "No scheduled maintenance stop"}

        cancelled_at = self.config.maintenance_stop_scheduled_at
        self._clear_maintenance_schedule(save=True)
        self._add_log(
            "maintenance_cancelled",
            "info",
            f"Scheduled maintenance stop cancelled (was {cancelled_at})",
        )
        self._queue_background_command('say §a[Maintenance] §fScheduled shutdown has been cancelled.')
        return {
            "success": True,
            "message": "Scheduled maintenance stop cancelled",
            "cancelled_at": cancelled_at,
            "config": self.config.to_dict(),
        }

    # =========================================================================
    # CoreProtect Purge Methods
    # =========================================================================

    def _should_run_purge(self) -> bool:
        """Check if CoreProtect purge should run"""
        if not self.config.coreprotect_purge_enabled:
            return False

        if not self.status.server_running:
            return False

        now = datetime.now()

        # Check if it's the configured hour
        if now.hour != self.config.coreprotect_purge_hour:
            return False

        # Check if we already ran today
        if self.config.coreprotect_last_purge:
            try:
                last_purge = datetime.fromisoformat(self.config.coreprotect_last_purge)
                if last_purge.date() == now.date():
                    return False  # Already ran today
            except (ValueError, TypeError):
                pass

        return True

    def _get_next_purge_time(self) -> Optional[str]:
        """Calculate when the next purge will run"""
        if not self.config.coreprotect_purge_enabled:
            return None

        now = datetime.now()
        next_purge = now.replace(
            hour=self.config.coreprotect_purge_hour,
            minute=0,
            second=0,
            microsecond=0
        )

        # If we've passed today's purge time, schedule for tomorrow
        if now.hour >= self.config.coreprotect_purge_hour:
            next_purge += timedelta(days=1)

        return next_purge.isoformat()

    async def _check_coreprotect_purge(self):
        """Check and execute CoreProtect purge if needed"""
        if self._should_run_purge():
            await self.execute_coreprotect_purge()

        # Update next purge time in status
        self.status.coreprotect_next_purge = self._get_next_purge_time()
        self.status.coreprotect_last_purge = self.config.coreprotect_last_purge

    async def execute_coreprotect_purge(self, manual: bool = False) -> dict:
        """
        Execute CoreProtect log purge.

        CoreProtect requires confirmation, so we send:
        1. /co purge t:30d
        2. Wait for response
        3. /co purge t:30d confirm
        """
        if not self.status.server_running:
            return {"success": False, "error": "Server is not running"}

        if self.status.coreprotect_purge_running:
            return {"success": False, "error": "Purge already in progress"}

        self.status.coreprotect_purge_running = True
        retention_days = self.config.coreprotect_retention_days

        self._add_log(
            "coreprotect_purge_started",
            "info",
            f"CoreProtect purge started: deleting logs older than {retention_days} days" +
            (" (manual)" if manual else " (scheduled)")
        )

        try:
            # Step 1: Send initial purge command
            purge_cmd = f"co purge t:{retention_days}d"
            result1 = await minecraft_server.send_command(purge_cmd)

            if not result1.get("success"):
                raise Exception(f"Initial purge command failed: {result1.get('error')}")

            # Wait a moment for CoreProtect to process
            await asyncio.sleep(2)

            # Step 2: Send confirmation command
            confirm_cmd = f"co purge t:{retention_days}d confirm"
            result2 = await minecraft_server.send_command(confirm_cmd)

            if not result2.get("success"):
                raise Exception(f"Purge confirmation failed: {result2.get('error')}")

            # Update last purge time
            now = datetime.now()
            purge_iso = now.isoformat()
            self._save_coreprotect_last_purge(purge_iso)
            self.status.coreprotect_last_purge = purge_iso

            self._add_log(
                "coreprotect_purge_completed",
                "success",
                f"CoreProtect purge completed: deleted logs older than {retention_days} days"
            )

            return {
                "success": True,
                "message": f"Purge completed: deleted logs older than {retention_days} days",
                "retention_days": retention_days,
                "purged_at": now.isoformat()
            }

        except Exception as e:
            self._add_log(
                "coreprotect_purge_failed",
                "failed",
                f"CoreProtect purge failed: {str(e)}"
            )
            return {"success": False, "error": str(e)}

        finally:
            self.status.coreprotect_purge_running = False

    def get_coreprotect_status(self) -> dict:
        """Get CoreProtect purge status"""
        return {
            "enabled": self.config.coreprotect_purge_enabled,
            "retention_days": self.config.coreprotect_retention_days,
            "purge_hour": self.config.coreprotect_purge_hour,
            "last_purge": self.config.coreprotect_last_purge,
            "next_purge": self._get_next_purge_time(),
            "purge_running": self.status.coreprotect_purge_running
        }


# Global singleton instance
_scheduler: Optional[RebootScheduler] = None


def get_scheduler() -> RebootScheduler:
    """Get the global scheduler instance"""
    global _scheduler
    if _scheduler is None:
        _scheduler = RebootScheduler()
    return _scheduler


async def start_scheduler():
    """Start the scheduler (call from app lifespan)"""
    global _scheduler
    # Stop any existing scheduler to prevent orphaned monitor loops
    if _scheduler is not None:
        try:
            await _scheduler.stop()
        except Exception as e:
            print(f"[RebootScheduler] Failed to stop existing scheduler instance: {e}")
    scheduler = get_scheduler()
    await scheduler.start()


async def stop_scheduler():
    """Stop the scheduler (call from app lifespan)"""
    scheduler = get_scheduler()
    await scheduler.stop()
