# app/services/backup_scheduler.py
"""
Minecraft Server Backup Automation Service

Automates full server backups on a configurable schedule:
1. Stop server with player warnings (countdown)
2. Compress server directory to tar.gz
3. Upload to Google Drive (service account auth)
4. Restart server
5. Clean up local archive after successful upload
6. Auto-prune old backups in Drive (keep last N)

State machine:
DISABLED → MONITORING → COUNTDOWN → STOPPING_SERVER → COMPRESSING → UPLOADING → RESTARTING → MONITORING
"""

import asyncio
import json
import logging
import os
import platform
import subprocess
import time
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List
from enum import Enum

from app.core.config import DATA_DIR, BACKUP_TEMP_DIR, CONFIG_FILES_DIR
from app.services import minecraft_server, minecraft_settings

logger = logging.getLogger(__name__)

# Configuration and log file paths
CONFIG_FILE = DATA_DIR / "backup_scheduler_config.json"
LOG_FILE = DATA_DIR / "backup_scheduler_log.json"
SERVICE_ACCOUNT_FILE = CONFIG_FILES_DIR / "service_account_backup.json"


class BackupState(str, Enum):
    """Current state of the backup scheduler"""
    DISABLED = "disabled"
    MONITORING = "monitoring"
    COUNTDOWN = "countdown"
    STOPPING_SERVER = "stopping_server"
    COMPRESSING = "compressing"
    UPLOADING = "uploading"
    RESTARTING = "restarting"
    ERROR = "error"


@dataclass
class BackupConfig:
    """Backup scheduler configuration (persisted to JSON)"""
    enabled: bool = False
    backup_interval_days: int = 7
    backup_hour: int = 5
    backup_minute: int = 0
    countdown_minutes: int = 5
    warning_intervals: List[int] = field(default_factory=lambda: [5, 3, 1])
    drive_folder_id: str = ""
    keep_drive_backups: int = 10
    last_backup_time: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BackupConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class BackupStatus:
    """Runtime backup status"""
    state: BackupState = BackupState.DISABLED
    current_operation: str = ""
    progress_percent: int = 0
    countdown_remaining_seconds: int = 0
    next_backup_at: Optional[str] = None
    last_backup_size_mb: float = 0
    last_backup_duration_seconds: int = 0
    last_backup_drive_url: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass
class BackupLog:
    """Log entry for backup actions"""
    timestamp: str
    action: str
    status: str  # "success", "failed", "info"
    details: str

    def to_dict(self) -> dict:
        return asdict(self)


class BackupScheduler:
    """Manages automatic server backups to Google Drive"""

    def __init__(self):
        self.config = BackupConfig()
        self.status = BackupStatus()
        self.logs: List[BackupLog] = []

        # Countdown tracking
        self._countdown_start: Optional[datetime] = None
        self._countdown_target: Optional[datetime] = None
        self._warnings_sent: set = set()

        # Background task
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

        # Google Drive service (lazy init)
        self._drive_service = None
        self._last_connection_test = None  # None = not tested, True/False = result

        # Load saved state
        self._load_config()
        self._load_logs()

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    self.config = BackupConfig.from_dict(data)
            except Exception as e:
                logger.warning("[BackupScheduler] Failed to load config: %s", e)

    def _save_config(self):
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config.to_dict(), f, indent=2)
        except Exception as e:
            logger.warning("[BackupScheduler] Failed to save config: %s", e)

    def _load_logs(self):
        if LOG_FILE.exists():
            try:
                with open(LOG_FILE, "r") as f:
                    data = json.load(f)
                    self.logs = [BackupLog(**log) for log in data[-100:]]
            except Exception as e:
                logger.warning("[BackupScheduler] Failed to load logs: %s", e)

    def _save_logs(self):
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "w") as f:
                json.dump([log.to_dict() for log in self.logs[-100:]], f, indent=2)
        except Exception as e:
            logger.warning("[BackupScheduler] Failed to save logs: %s", e)

    def _add_log(self, action: str, status: str, details: str):
        log = BackupLog(
            timestamp=datetime.now().isoformat(),
            action=action,
            status=status,
            details=details,
        )
        self.logs.append(log)
        self._save_logs()
        logger.info("[BackupScheduler] %s: %s (%s)", action, details, status)

    # =========================================================================
    # Google Drive
    # =========================================================================

    def _get_drive_service(self):
        """Initialize Google Drive service with service account credentials"""
        if self._drive_service is not None:
            return self._drive_service

        if not SERVICE_ACCOUNT_FILE.exists():
            raise FileNotFoundError(
                f"Service account key not found: {SERVICE_ACCOUNT_FILE}\n"
                "Create one in Google Cloud Console and save it there."
            )

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            str(SERVICE_ACCOUNT_FILE),
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        self._drive_service = build("drive", "v3", credentials=credentials)
        return self._drive_service

    async def _upload_to_drive(self, file_path: Path, folder_id: str) -> dict:
        """Upload a file to Google Drive (runs in executor to avoid blocking)"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._upload_to_drive_sync, file_path, folder_id)

    def _upload_to_drive_sync(self, file_path: Path, folder_id: str) -> dict:
        """Synchronous Drive upload with resumable media"""
        from googleapiclient.http import MediaFileUpload

        service = self._get_drive_service()
        file_metadata = {
            "name": file_path.name,
            "parents": [folder_id],
        }

        # Detect mimetype based on file extension
        mimetype = "application/zip" if file_path.suffix == ".zip" else "application/gzip"

        media = MediaFileUpload(
            str(file_path),
            mimetype=mimetype,
            resumable=True,
            chunksize=50 * 1024 * 1024,  # 50 MB chunks
        )

        request = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink, size",
            supportsAllDrives=True,  # Support files shared with service account
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                self.status.progress_percent = int(60 + status.progress() * 35)  # 60-95%

        return response

    async def _prune_old_backups(self, folder_id: str, keep: int):
        """Delete oldest backups in Drive folder, keeping only the last N"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._prune_old_backups_sync, folder_id, keep)

    def _prune_old_backups_sync(self, folder_id: str, keep: int):
        service = self._get_drive_service()

        # List files in folder sorted by creation time
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            orderBy="createdTime asc",
            fields="files(id, name, createdTime)",
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        files = results.get("files", [])
        if len(files) <= keep:
            return

        # Delete oldest files
        to_delete = files[:len(files) - keep]
        for f in to_delete:
            try:
                service.files().delete(
                    fileId=f["id"],
                    supportsAllDrives=True
                ).execute()
                logger.info("[BackupScheduler] Pruned old backup: %s", f["name"])
            except Exception as e:
                logger.warning("[BackupScheduler] Failed to delete %s: %s", f["name"], e)

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self):
        if self._running:
            return
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._add_log("scheduler_start", "success", "Backup scheduler started")

    async def stop(self):
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._add_log("scheduler_stop", "success", "Backup scheduler stopped")

    # =========================================================================
    # Config & Status API
    # =========================================================================

    def update_config(self, **kwargs) -> dict:
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        self._save_config()

        changes = ", ".join(f"{k}={v}" for k, v in kwargs.items())
        self._add_log("config_changed", "success", f"Configuration updated: {changes}")
        return {"success": True, "config": self.config.to_dict()}

    def get_config(self) -> dict:
        return self.config.to_dict()

    def get_status(self) -> dict:
        self._update_next_backup_time()
        return self.status.to_dict()

    def get_setup_status(self) -> dict:
        """Check whether the backup system prerequisites are met"""
        service_account_exists = SERVICE_ACCOUNT_FILE.exists()
        service_account_email = None

        if service_account_exists:
            try:
                with open(SERVICE_ACCOUNT_FILE, "r") as f:
                    sa_data = json.load(f)
                    service_account_email = sa_data.get("client_email")
            except Exception:
                pass

        drive_folder_id_set = bool(self.config.drive_folder_id)

        return {
            "service_account_exists": service_account_exists,
            "service_account_email": service_account_email,
            "drive_folder_id_set": drive_folder_id_set,
            "drive_connection_ok": self._last_connection_test,
            "setup_complete": service_account_exists and service_account_email is not None and drive_folder_id_set,
        }

    def test_drive_connection(self) -> dict:
        """Test Google Drive connectivity: load service account + list files in folder"""
        if not SERVICE_ACCOUNT_FILE.exists():
            self._last_connection_test = False
            return {"success": False, "error": "Service account file not found"}

        if not self.config.drive_folder_id:
            self._last_connection_test = False
            return {"success": False, "error": "Drive folder ID not configured"}

        try:
            # Force re-init to pick up any new service account file
            self._drive_service = None
            service = self._get_drive_service()

            # Try listing files in the configured folder (proves auth + folder access)
            results = service.files().list(
                q=f"'{self.config.drive_folder_id}' in parents and trashed=false",
                fields="files(id, name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()

            file_count = len(results.get("files", []))
            self._last_connection_test = True
            self._add_log("connection_test", "success", "Drive connection test passed")
            return {
                "success": True,
                "message": f"Connected! Found {file_count} file(s) in folder.",
            }
        except Exception as e:
            self._last_connection_test = False
            self._add_log("connection_test", "failed", f"Drive connection test failed: {e}")
            return {"success": False, "error": str(e)}

    def get_logs(self, limit: int = 50) -> List[dict]:
        return [log.to_dict() for log in self.logs[-limit:]][::-1]

    def _update_next_backup_time(self):
        """Calculate when the next backup will run"""
        if not self.config.enabled or not self.config.drive_folder_id:
            self.status.next_backup_at = None
            return

        now = datetime.now()

        # Calculate next eligible backup time
        if self.config.last_backup_time:
            try:
                last = datetime.fromisoformat(self.config.last_backup_time)
                next_date = last + timedelta(days=self.config.backup_interval_days)
                next_backup = next_date.replace(
                    hour=self.config.backup_hour,
                    minute=self.config.backup_minute,
                    second=0, microsecond=0,
                )
                if next_backup < now:
                    # Overdue — next check cycle will trigger it
                    next_backup = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            except (ValueError, TypeError):
                next_backup = now
        else:
            # Never backed up — next run at configured time
            next_backup = now.replace(
                hour=self.config.backup_hour,
                minute=self.config.backup_minute,
                second=0, microsecond=0,
            )
            if next_backup < now:
                next_backup += timedelta(days=1)

        self.status.next_backup_at = next_backup.isoformat()

    # =========================================================================
    # Monitor Loop
    # =========================================================================

    async def _monitor_loop(self):
        logger.info("[BackupScheduler] Monitor loop started")
        while self._running:
            try:
                await self._check_and_act()
            except Exception as e:
                self.status.state = BackupState.ERROR
                self.status.error_message = str(e)
                self._add_log("error", "failed", f"Monitor error: {e}")

            await asyncio.sleep(60)

    async def _check_and_act(self):
        now = datetime.now()

        if not self.config.enabled:
            self.status.state = BackupState.DISABLED
            self.status.current_operation = ""
            return

        # If we're in a countdown, handle it
        if self.status.state == BackupState.COUNTDOWN:
            await self._handle_countdown(now)
            return

        # If we're in any active backup state or error state, don't interfere
        if self.status.state in (
            BackupState.STOPPING_SERVER, BackupState.COMPRESSING,
            BackupState.UPLOADING, BackupState.RESTARTING,
            BackupState.ERROR,
        ):
            return

        self.status.state = BackupState.MONITORING
        self.status.current_operation = "Monitoring schedule"
        self._update_next_backup_time()

        # Check if reboot scheduler is doing something — skip if so
        try:
            from app.services.reboot_scheduler import get_scheduler
            reboot = get_scheduler()
            reboot_state = reboot.status.state.value if hasattr(reboot.status.state, 'value') else str(reboot.status.state)
            if reboot_state not in ("disabled", "monitoring"):
                return
        except Exception:
            pass

        # Check schedule
        if not self.config.drive_folder_id:
            return

        if not self._is_backup_due(now):
            return

        # Time to back up
        await self._start_countdown()

    def _is_backup_due(self, now: datetime) -> bool:
        """Check if it's time for a backup"""
        # Must be at or past the configured hour:minute
        if now.hour < self.config.backup_hour:
            return False
        if now.hour == self.config.backup_hour and now.minute < self.config.backup_minute:
            return False

        # Check interval since last backup
        if self.config.last_backup_time:
            try:
                last = datetime.fromisoformat(self.config.last_backup_time)
                days_since = (now - last).total_seconds() / 86400
                if days_since < self.config.backup_interval_days:
                    return False
            except (ValueError, TypeError):
                pass  # Invalid date, allow backup

        return True

    # =========================================================================
    # Countdown
    # =========================================================================

    async def _start_countdown(self):
        """Start countdown with player warnings"""
        now = datetime.now()
        server_status = minecraft_server.get_server_status()

        if not server_status.running:
            # Server not running — skip countdown, go straight to compression
            self._add_log("backup_triggered", "info", "Server not running, starting backup directly")
            await self._execute_backup(server_was_running=False)
            return

        if server_status.players_online == 0:
            # No players — skip countdown
            self._add_log("backup_triggered", "info", "No players online, starting backup immediately")
            await self._execute_backup(server_was_running=True)
            return

        # Players online — start countdown
        self.status.state = BackupState.COUNTDOWN
        self._countdown_start = now
        self._countdown_target = now + timedelta(minutes=self.config.countdown_minutes)
        self._warnings_sent = set()

        self._add_log(
            "countdown_started", "info",
            f"Backup countdown started ({self.config.countdown_minutes}min), "
            f"{server_status.players_online} players online"
        )

        await self._send_warning(self.config.countdown_minutes)

    async def _handle_countdown(self, now: datetime):
        if self._countdown_target is None:
            self.status.state = BackupState.MONITORING
            return

        remaining = (self._countdown_target - now).total_seconds()
        self.status.countdown_remaining_seconds = max(0, int(remaining))
        self.status.current_operation = f"Backup countdown: {self._format_duration(self.status.countdown_remaining_seconds)}"

        if remaining <= 0:
            await self._execute_backup(server_was_running=True)
            return

        # Minute warnings
        remaining_minutes = remaining / 60
        for warning_minute in self.config.warning_intervals:
            if warning_minute not in self._warnings_sent and remaining_minutes <= warning_minute:
                await self._send_warning(warning_minute)
                self._warnings_sent.add(warning_minute)

        # 30s and 10s warnings
        if remaining <= 30 and "30s" not in self._warnings_sent:
            await self._send_warning(0.5)
            self._warnings_sent.add("30s")
        if remaining <= 10 and "10s" not in self._warnings_sent:
            await self._send_warning(0.17)
            self._warnings_sent.add("10s")

    async def _send_warning(self, minutes: float):
        if minutes >= 1:
            time_str = f"{int(minutes)} minute{'s' if minutes != 1 else ''}"
        else:
            seconds = int(minutes * 60)
            time_str = f"{seconds} seconds"

        title_cmd = f'title @a title {{"text":"☁ SERVER BACKUP","color":"aqua","bold":true}}'
        subtitle_cmd = f'title @a subtitle {{"text":"shutting down in {time_str}","color":"yellow"}}'
        chat_cmd = f'say §b[Auto-Backup] §eServer will shut down for backup in {time_str}. Please find a safe spot!'

        try:
            await minecraft_server.send_command(title_cmd)
            await minecraft_server.send_command(subtitle_cmd)
            await minecraft_server.send_command(chat_cmd)
            self._add_log("warning_sent", "success", f"Backup warning sent: {time_str}")
        except Exception as e:
            self._add_log("warning_sent", "failed", f"Failed to send warning: {e}")

    def cancel_countdown(self) -> dict:
        if self.status.state != BackupState.COUNTDOWN:
            return {"success": False, "error": "No countdown active"}

        self._add_log("countdown_cancelled", "info", "Backup countdown cancelled by admin")
        self._reset_countdown()
        self.status.state = BackupState.MONITORING

        asyncio.create_task(
            minecraft_server.send_command('say §a[Auto-Backup] §fBackup has been cancelled!')
        )
        return {"success": True, "message": "Backup countdown cancelled"}

    def _reset_countdown(self):
        self._countdown_start = None
        self._countdown_target = None
        self._warnings_sent = set()
        self.status.countdown_remaining_seconds = 0

    def _get_server_version(self) -> str:
        """Get current Paper server version from versions.json"""
        try:
            from app.services.minecraft_updater import load_versions
            versions_data = load_versions()
            paper_info = versions_data.get("plugins", {}).get("paper", {})
            version = paper_info.get("current_version", "unknown")
            return version.split("-")[0] if "-" in version else version
        except Exception as e:
            logger.warning("[BackupScheduler] Failed to load server version: %s", e)
            return "unknown"

    # =========================================================================
    # Backup Execution Pipeline
    # =========================================================================

    async def _execute_backup(self, server_was_running: bool):
        """Full backup pipeline: stop → compress → upload → restart"""
        self._reset_countdown()
        backup_start = time.time()
        local_archive: Optional[Path] = None

        try:
            # --- Stop server ---
            if server_was_running:
                self.status.state = BackupState.STOPPING_SERVER
                self.status.current_operation = "Stopping server..."
                self.status.progress_percent = 5

                # Final message to players
                try:
                    await minecraft_server.send_command(
                        'say §c[Auto-Backup] §fShutting down now for backup. See you soon!'
                    )
                    await asyncio.sleep(2)
                except Exception:
                    pass

                result = await minecraft_server.stop_server()
                if not result.get("success"):
                    raise RuntimeError(f"Failed to stop server: {result.get('error')}")

                # Wait for clean shutdown
                for _ in range(60):
                    await asyncio.sleep(1)
                    if not minecraft_server.is_server_running():
                        break
                else:
                    raise RuntimeError("Server did not stop within 60 seconds")

                self._add_log("server_stopped", "success", "Server stopped for backup")
                self.status.progress_percent = 15

            # --- Compress ---
            self.status.state = BackupState.COMPRESSING
            self.status.current_operation = "Compressing server directory..."
            self.status.progress_percent = 20

            # Generate filename: server-folder 1.21.11(2026-2-8).zip
            server_version = self._get_server_version()
            server_dir = minecraft_settings.get_server_directory()

            # Platform-specific date formatting (remove leading zeros)
            if platform.system() == "Windows":
                date_str = datetime.now().strftime("%Y-%#m-%#d")
            else:
                date_str = datetime.now().strftime("%Y-%-m-%-d")

            archive_name = f"{server_dir.name} {server_version}({date_str}).zip"
            BACKUP_TEMP_DIR.mkdir(parents=True, exist_ok=True)
            local_archive = BACKUP_TEMP_DIR / archive_name

            # Create zip archive using Python zipfile module
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._create_zip_archive,
                local_archive,
                server_dir
            )

            archive_size_mb = local_archive.stat().st_size / (1024 * 1024)
            self._add_log(
                "compressed", "success",
                f"Archive created: {archive_name} ({archive_size_mb:.1f} MB)"
            )
            self.status.progress_percent = 55

            # --- Upload ---
            self.status.state = BackupState.UPLOADING
            self.status.current_operation = f"Uploading {archive_size_mb:.0f} MB to Google Drive..."
            self.status.progress_percent = 60

            drive_response = await self._upload_to_drive(local_archive, self.config.drive_folder_id)
            drive_url = drive_response.get("webViewLink", "")
            drive_size = int(drive_response.get("size", 0)) / (1024 * 1024)

            self._add_log(
                "uploaded", "success",
                f"Uploaded to Drive: {drive_response.get('name')} ({drive_size:.1f} MB)"
            )
            self.status.progress_percent = 95

            # Store results
            self.status.last_backup_size_mb = archive_size_mb
            self.status.last_backup_drive_url = drive_url

            # Update last backup time
            self.config.last_backup_time = datetime.now().isoformat()
            self._save_config()

            # --- Clean up local archive ---
            try:
                local_archive.unlink()
                local_archive = None
            except Exception as e:
                logger.warning("[BackupScheduler] Failed to delete local archive: %s", e)

            # --- Prune old backups ---
            if self.config.keep_drive_backups > 0:
                try:
                    self.status.current_operation = "Pruning old backups..."
                    await self._prune_old_backups(
                        self.config.drive_folder_id,
                        self.config.keep_drive_backups,
                    )
                except Exception as e:
                    self._add_log("prune_failed", "failed", f"Failed to prune old backups: {e}")

        except Exception as e:
            self._add_log("backup_failed", "failed", f"Backup failed: {e}")
            self.status.error_message = str(e)
            # Keep local archive on failure for manual upload
            if local_archive and local_archive.exists():
                self._add_log(
                    "archive_kept", "info",
                    f"Local archive kept for manual upload: {local_archive}"
                )

        finally:
            # --- Always restart server if it was running ---
            if server_was_running:
                self.status.state = BackupState.RESTARTING
                self.status.current_operation = "Restarting server..."
                self.status.progress_percent = 97

                try:
                    start_result = await minecraft_server.start_server()
                    if start_result.get("success"):
                        self._add_log("server_restarted", "success", "Server restarted after backup")
                    else:
                        self._add_log(
                            "restart_failed", "failed",
                            f"Server restart failed: {start_result.get('error')}"
                        )
                except Exception as e:
                    self._add_log("restart_failed", "failed", f"Server restart exception: {e}")

            # Finalize
            duration = int(time.time() - backup_start)
            self.status.last_backup_duration_seconds = duration
            self.status.progress_percent = 100
            self.status.current_operation = ""

            if self.status.error_message:
                self.status.state = BackupState.ERROR
            else:
                self.status.state = BackupState.MONITORING
                self._add_log(
                    "backup_completed", "success",
                    f"Backup completed in {self._format_duration(duration)}"
                )
                self.status.error_message = None

            # --- Send email notification ---
            try:
                from app.services.gmail_sender import send_backup_notification
                from app.core.auth import ADMIN_EMAILS

                email_sent = await send_backup_notification(
                    recipients=list(ADMIN_EMAILS),
                    success=(self.status.error_message is None),
                    backup_time=datetime.now().isoformat(),
                    file_size_mb=self.status.last_backup_size_mb,
                    duration_seconds=duration,
                    filename=archive_name,
                    drive_url=self.status.last_backup_drive_url,
                    error_message=self.status.error_message
                )

                if email_sent:
                    self._add_log("email_sent", "success", f"Notification sent to {len(ADMIN_EMAILS)} admins")
                else:
                    self._add_log("email_failed", "failed", "Failed to send email notification")
            except Exception as e:
                self._add_log("email_failed", "failed", f"Email notification error: {e}")

    def _create_zip_archive(self, archive_path: Path, source_dir: Path):
        """
        Create ZIP archive of server directory (runs in executor thread)

        Args:
            archive_path: Destination .zip file path
            source_dir: Server directory to compress
        """
        total_files = sum(1 for _ in source_dir.rglob('*') if _.is_file())
        processed = 0

        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in source_dir.rglob('*'):
                if file_path.is_file():
                    # Calculate relative path for archive
                    arcname = file_path.relative_to(source_dir.parent)

                    try:
                        zipf.write(file_path, arcname)
                        processed += 1

                        # Update progress (20-55% range for compression)
                        if total_files > 0:
                            progress = 20 + int((processed / total_files) * 35)
                            self.status.progress_percent = min(progress, 55)
                    except Exception as e:
                        logger.warning("[BackupScheduler] Failed to add file %s: %s", file_path, e)

    # =========================================================================
    # Manual Trigger
    # =========================================================================

    async def trigger_manual_backup(self) -> dict:
        """Manually trigger a backup"""
        block = minecraft_settings.get_active_profile_operation_block("start backup")
        if block:
            return block

        if self.status.state not in (BackupState.DISABLED, BackupState.MONITORING, BackupState.ERROR):
            return {"success": False, "error": f"Cannot start backup in {self.status.state.value} state"}

        if not self.config.drive_folder_id:
            return {"success": False, "error": "Drive folder ID not configured"}

        # Check service account file exists
        if not SERVICE_ACCOUNT_FILE.exists():
            return {"success": False, "error": "Service account key file not found"}

        self._add_log("manual_backup", "info", "Manual backup triggered by admin")

        server_status = minecraft_server.get_server_status()

        if server_status.running and server_status.players_online > 0:
            # Start countdown
            await self._start_countdown()
            return {
                "success": True,
                "message": f"Backup countdown started ({self.config.countdown_minutes} minutes)"
            }
        else:
            # Immediate backup
            asyncio.create_task(self._execute_backup(server_was_running=server_status.running))
            return {"success": True, "message": "Backup started (no players online)"}

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}m"


# =============================================================================
# Singleton
# =============================================================================

_scheduler: Optional[BackupScheduler] = None


def get_backup_scheduler() -> BackupScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackupScheduler()
    return _scheduler


async def start_scheduler():
    scheduler = get_backup_scheduler()
    await scheduler.start()


async def stop_scheduler():
    scheduler = get_backup_scheduler()
    await scheduler.stop()
