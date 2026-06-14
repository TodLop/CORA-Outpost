# app/services/minecraft_server.py
"""
Minecraft Server Process Management Service

Handles:
- Starting/stopping/restarting the server
- Process status monitoring
- Log streaming
- Command execution (RCON or stdin)
- Graceful shutdown
"""

import asyncio
import fcntl
import gzip
import json
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
import math
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, List, TextIO
from collections import deque

from app.core.config import DATA_DIR
from app.services import minecraft_settings
from app.services import whitelist_autocomplete_cache
from app.services.rcon import (
    RCONClient, RCONConfig, get_rcon_config, load_server_properties,
    strip_minecraft_colors,
)

logger = logging.getLogger(__name__)

# Legacy aliases kept for older callers and tests that monkeypatch SERVER_DIR.
SERVER_DIR = minecraft_settings.get_default_server_directory()
SERVER_PROPERTIES = SERVER_DIR / "server.properties"
START_SCRIPT = SERVER_DIR / "start.sh"
LOGS_DIR = SERVER_DIR / "logs"
LATEST_LOG = LOGS_DIR / "latest.log"
CONSOLE_HISTORY_FILE = LOGS_DIR / "cora_console_history.jsonl"
PID_FILE = SERVER_DIR / "server.pid"


def get_server_directory() -> Path:
    configured = minecraft_settings.get_server_directory()
    default = minecraft_settings.get_default_server_directory()
    legacy_dir = Path(SERVER_DIR)
    if legacy_dir != default and legacy_dir != configured:
        return legacy_dir
    return configured


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


def get_runtime_state_dir() -> Path:
    return minecraft_settings.get_active_profile_state_dir()


def get_pid_file() -> Path:
    return get_runtime_state_dir() / "server.pid"


def get_restart_state_file() -> Path:
    return get_runtime_state_dir() / "minecraft_restart_state.json"


def get_restart_lock_file() -> Path:
    return get_runtime_state_dir() / "minecraft_restart.lock"


def _operation_block(action: str, *, requires_rcon: bool = False) -> Optional[dict]:
    return minecraft_settings.get_active_profile_operation_block(
        action,
        requires_rcon=requires_rcon,
    )


def reset_path_dependent_runtime_state() -> None:
    """Clear cached runtime state after the configured server directory changes."""
    _manager.status_cache = None
    _manager.status_cache_time = 0
    _manager.online_players_cache = None
    _manager.online_players_cache_time = 0
    _manager.log_buffer.clear()
    _manager.last_log_position = 0
    _manager.last_log_inode = None
    whitelist_autocomplete_cache.invalidate()

# Log messages to filter out (noise)
LOG_FILTER_PATTERNS = [
    "Thread RCON Client",
    "Rcon issued server command: /list",
]

# Status cache TTL
STATUS_CACHE_TTL = 5.0
DEFAULT_LOG_LINE_LIMIT = 500
MAX_LOG_LINE_LIMIT = 5000
LOG_TAIL_CHUNK_BYTES = 64 * 1024
MAX_COMPRESSED_LOG_BYTES = 20 * 1024 * 1024
DEFAULT_READY_TIMEOUT_SEC = 120
READY_POLL_INTERVAL_SEC = 1.0
PROCESS_BOOT_GRACE_SEC = 20
RESTART_START_RETRIES = 2
RESTART_RETRY_DELAY_SEC = 3
RESTART_COOLDOWN_SECONDS = 120
RESTART_STATE_FILE = DATA_DIR / "minecraft_restart_state.json"
RESTART_LOCK_FILE = DATA_DIR / "minecraft_restart.lock"


@dataclass
class ServerStatus:
    """Server status information"""
    running: bool = False
    process_running: bool = False
    game_port_listening: bool = False
    rcon_port_listening: bool = False
    healthy: bool = False
    state_reason: str = "stopped"
    pid: Optional[int] = None
    uptime_seconds: Optional[int] = None
    started_at: Optional[str] = None
    players_online: int = 0
    max_players: int = 20
    version: Optional[str] = None
    memory_used: Optional[str] = None
    players: Optional[list[str]] = None
    stale: bool = False
    last_updated: Optional[float] = None


class ServerManager:
    """Encapsulates all mutable server state (replaces module-level globals)."""

    def __init__(self):
        self.log_buffer: deque = deque(maxlen=500)
        self.log_subscribers: List[Callable] = []
        self.process_lock = asyncio.Lock()
        self.restart_guard_lock = asyncio.Lock()
        self.log_reader_task: Optional[asyncio.Task] = None
        self.last_log_position: int = 0
        self.last_log_inode: Optional[int] = None

        # Status cache
        self.status_cache: Optional[ServerStatus] = None
        self.status_cache_time: float = 0
        self.status_cache_lock = threading.Lock()
        self.online_players_cache: Optional[dict] = None
        self.online_players_cache_time: float = 0
        self.online_players_refresh_lock = threading.Lock()

        # Deduplication
        self.last_message: str = ""
        self.last_message_time: float = 0.0

        # Restart deduplication guard
        self.restart_in_progress: bool = False
        self.last_restart_completed_at: Optional[datetime] = None
        self.last_restart_source: str = ""
        self._last_restart_lock_error: Optional[str] = None
        self._load_restart_state()

    def _restart_cooldown_remaining_seconds(self, now: datetime) -> int:
        self._load_restart_state()
        if self.last_restart_completed_at is None:
            return 0
        elapsed = (now - self.last_restart_completed_at).total_seconds()
        remaining = RESTART_COOLDOWN_SECONDS - elapsed
        return max(0, int(math.ceil(remaining)))

    def _load_restart_state(self) -> None:
        restart_state_file = get_restart_state_file()
        if not restart_state_file.exists():
            return

        try:
            with open(restart_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Failed to load restart state: %s", e)
            return

        raw_ts = data.get("last_restart_completed_at")
        parsed_ts = None
        if isinstance(raw_ts, str) and raw_ts:
            try:
                parsed_ts = datetime.fromisoformat(raw_ts)
            except ValueError:
                logger.warning("Invalid restart timestamp in state file: %s", raw_ts)

        if parsed_ts is None:
            return

        if self.last_restart_completed_at is None or parsed_ts > self.last_restart_completed_at:
            self.last_restart_completed_at = parsed_ts
            self.last_restart_source = str(data.get("last_restart_source", "unknown"))

    def _save_restart_state(self) -> None:
        if self.last_restart_completed_at is None:
            return

        restart_state_file = get_restart_state_file()
        try:
            restart_state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(restart_state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "last_restart_completed_at": self.last_restart_completed_at.isoformat(),
                        "last_restart_source": self.last_restart_source,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.warning("Failed to persist restart state: %s", e)

    def _acquire_restart_file_lock(self) -> Optional[TextIO]:
        restart_lock_file = get_restart_lock_file()
        try:
            restart_lock_file.parent.mkdir(parents=True, exist_ok=True)
            handle = open(restart_lock_file, "a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._last_restart_lock_error = None
                return handle
            except BlockingIOError:
                self._last_restart_lock_error = None
                handle.close()
                return None
        except Exception as e:
            logger.warning("Failed to acquire restart file lock: %s", e)
            self._last_restart_lock_error = str(e)
            return None

    @staticmethod
    def _release_restart_file_lock(handle: Optional[TextIO]) -> None:
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

    def get_restart_gate_status(self) -> dict:
        now = datetime.now()
        cooldown_remaining = self._restart_cooldown_remaining_seconds(now)
        in_progress = self.restart_in_progress
        return {
            "can_restart": (not in_progress) and cooldown_remaining == 0,
            "in_progress": in_progress,
            "cooldown_remaining_seconds": cooldown_remaining,
            "last_restart_completed_at": (
                self.last_restart_completed_at.isoformat()
                if self.last_restart_completed_at is not None
                else None
            ),
            "last_restart_source": self.last_restart_source,
            "lock_error": self._last_restart_lock_error,
        }

    # ------------------------------------------------------------------
    # Log filtering & persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _should_filter_log(message: str) -> bool:
        for pattern in LOG_FILTER_PATTERNS:
            if pattern in message:
                return True
        return False

    def _save_console_history(self):
        try:
            logs_dir = get_logs_dir()
            console_history_file = get_console_history_file()
            logs_dir.mkdir(parents=True, exist_ok=True)
            with open(console_history_file, 'w', encoding='utf-8') as f:
                for entry in self.log_buffer:
                    f.write(json.dumps(entry) + '\n')
            logger.info(f"Saved {len(self.log_buffer)} log entries to {console_history_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to save console history: {e}")
            return False

    def _load_console_history(self):
        console_history_file = get_console_history_file()
        if not console_history_file.exists():
            logger.info("No console history file found, starting fresh")
            return False
        try:
            loaded_count = 0
            with open(console_history_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        self.log_buffer.append(entry)
                        loaded_count += 1
                    except json.JSONDecodeError:
                        continue
            logger.info(f"Loaded {loaded_count} log entries from history")
            return True
        except Exception as e:
            logger.error(f"Failed to load console history: {e}")
            return False

    # ------------------------------------------------------------------
    # Process detection (sync — called via asyncio.to_thread when needed)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_pid_file() -> Optional[int]:
        pid_file = get_pid_file()
        if pid_file.exists():
            try:
                return int(pid_file.read_text().strip())
            except (ValueError, IOError):
                pass
        return None

    @staticmethod
    def _write_pid_file(pid: int) -> None:
        pid_file = get_pid_file()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(pid))

    @staticmethod
    def _delete_pid_file() -> None:
        pid_file = get_pid_file()
        if pid_file.exists():
            pid_file.unlink()

    @staticmethod
    def _is_minecraft_process(pid: int) -> bool:
        """Check if a process with given PID is a Paper Minecraft server."""
        try:
            os.kill(pid, 0)
        except OSError:
            return False

        try:
            if Path(f"/proc/{pid}/cmdline").exists():
                with open(f"/proc/{pid}/cmdline", "r") as f:
                    cmdline = f.read()
                    return "java" in cmdline.lower() and "paper" in cmdline.lower()
            else:
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    cmdline = result.stdout.lower()
                    return "java" in cmdline and "paper" in cmdline
        except Exception as e:
            logger.error(f"Error checking process {pid}: {e}")

        return False

    @staticmethod
    def _get_process_cwd(pid: int) -> Optional[Path]:
        """Return the process working directory when the OS exposes it."""
        proc_cwd = Path(f"/proc/{pid}/cwd")
        try:
            if proc_cwd.exists():
                return Path(os.readlink(proc_cwd))
        except OSError:
            pass

        try:
            result = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("n") and len(line) > 1:
                        return Path(line[1:])
        except Exception as e:
            logger.debug("Failed to read cwd for process %s: %s", pid, e)

        return None

    @staticmethod
    def _same_directory(left: Path, right: Path) -> bool:
        try:
            return left.samefile(right)
        except OSError:
            return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)

    def _is_configured_minecraft_process(self, pid: int) -> bool:
        """Check whether PID belongs to the currently configured server folder."""
        if not self._is_minecraft_process(pid):
            return False

        process_cwd = self._get_process_cwd(pid)
        if process_cwd is None:
            logger.warning("Could not determine cwd for Minecraft PID %s", pid)
            return False

        server_dir = get_server_directory()
        if self._same_directory(process_cwd, server_dir):
            return True

        logger.info(
            "Ignoring Minecraft PID %s because cwd %s does not match configured server directory %s",
            pid,
            process_cwd,
            server_dir,
        )
        return False

    def _find_minecraft_pid(self) -> Optional[int]:
        try:
            result = subprocess.run(
                ["pgrep", "-f", "java.*paper.*\\.jar"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                for raw_pid in result.stdout.strip().split():
                    try:
                        pid = int(raw_pid)
                    except ValueError:
                        continue
                    if self._is_configured_minecraft_process(pid):
                        return pid
        except Exception:
            pass
        return None

    @staticmethod
    def _get_process_start_time(pid: int) -> Optional[datetime]:
        """Get the actual start time of a process from the OS.

        Uses ``ps -o lstart=`` which returns a string like
        ``Wed Feb 19 19:25:17 2026``.  This is independent of the Python
        app lifecycle, so run.py restarts don't affect the value.
        """
        try:
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # macOS format: "Wed Feb 19 19:25:17 2026"
                raw = result.stdout.strip()
                return datetime.strptime(raw, "%a %b %d %H:%M:%S %Y")
        except Exception as e:
            logger.warning(f"Failed to get process start time for PID {pid}: {e}")
        return None

    def _get_process_snapshot_sync(self) -> tuple[bool, Optional[int], bool]:
        """
        Return process snapshot as (process_running, pid, stale_pid_detected).
        Also heals stale PID files when detected.
        """
        pid = self._read_pid_file()
        if pid and self._is_configured_minecraft_process(pid):
            return True, pid, False

        stale_pid_detected = False
        if pid:
            stale_pid_detected = True
            logger.warning(
                "Stale PID file detected (PID %s is not the configured Minecraft server), cleaning up",
                pid,
            )
            self._delete_pid_file()

        found_pid = self._find_minecraft_pid()
        if found_pid:
            if pid != found_pid:
                logger.info(f"Found Minecraft process via pgrep: PID {found_pid}")
            self._write_pid_file(found_pid)
            return True, found_pid, stale_pid_detected

        return False, None, stale_pid_detected

    @staticmethod
    def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
        """Check whether a local TCP port accepts connections."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                return sock.connect_ex((host, int(port))) == 0
        except OSError:
            return False

    @staticmethod
    def _get_game_port(props: dict | None = None) -> int:
        props = props if props is not None else load_server_properties()
        try:
            return int(props.get("server-port", "25565"))
        except (TypeError, ValueError):
            return 25565

    def _is_server_running_sync(self) -> bool:
        """Sync version — shells out to ps/pgrep. Use is_server_running() for async."""
        running, _, _ = self._get_process_snapshot_sync()
        return running

    def _get_server_pid_sync(self) -> Optional[int]:
        _, pid, _ = self._get_process_snapshot_sync()
        return pid

    # ------------------------------------------------------------------
    # Async wrappers (run blocking I/O in thread pool)
    # ------------------------------------------------------------------

    async def is_server_running_async(self) -> bool:
        return await asyncio.to_thread(self._is_server_running_sync)

    async def get_server_pid_async(self) -> Optional[int]:
        return await asyncio.to_thread(self._get_server_pid_sync)

    def _probe_rcon_ready_once(self) -> tuple[bool, str]:
        """Best-effort readiness check: RCON connect + simple command."""
        rcon_config = get_rcon_config()
        if not rcon_config.enabled or not rcon_config.password:
            return False, "rcon_not_configured"

        client = RCONClient(rcon_config.host, rcon_config.port, rcon_config.password)
        try:
            if not client.connect():
                return False, "rcon_connect_failed"
            client.send_command("list")
            return True, "ready"
        except Exception as e:
            return False, f"rcon_error: {e}"
        finally:
            client.disconnect()

    @staticmethod
    def _parse_list_response(response: str, fallback_max_players: int) -> dict:
        clean_response = strip_minecraft_colors(response or "")
        players: list[str] = []
        if ":" in clean_response:
            players_part = clean_response.split(":", 1)[1].strip()
            if players_part:
                players = [
                    ServerManager._extract_username_from_display_name(p.strip())
                    for p in players_part.split(",")
                    if p.strip()
                ]

        players_online = len(players)
        max_players = fallback_max_players

        korean_match = re.search(
            r"최대\s*(\d+)\s*명.*?(\d+)\s*명의?\s*플레이어",
            clean_response,
            flags=re.DOTALL,
        )
        english_match = re.search(
            r"\bThere are\s+(\d+)\s+(?:of\s+(?:a\s+max\s+of\s+)?|/)\s*(\d+)\s+players?\s+online\b",
            clean_response,
            flags=re.IGNORECASE,
        )
        if korean_match:
            max_players = int(korean_match.group(1))
            players_online = int(korean_match.group(2))
        elif english_match:
            players_online = int(english_match.group(1))
            max_players = int(english_match.group(2))

        return {
            "players": players,
            "players_online": players_online,
            "max_players": max_players,
            "response": clean_response,
        }

    @staticmethod
    def _extract_username_from_display_name(display_name: str) -> str:
        match = re.match(r"(?:\[.*?\]\s*)?([a-zA-Z0-9_]{3,16})$", display_name.strip())
        return match.group(1) if match else display_name.strip()

    def get_online_players_snapshot(self, force_refresh: bool = False) -> dict:
        """Return a short-lived shared snapshot for online player/status reads."""
        now = time.time()
        cache = self.online_players_cache
        if (
            not force_refresh
            and cache is not None
            and (now - self.online_players_cache_time) < STATUS_CACHE_TTL
        ):
            return dict(cache)

        props = load_server_properties()
        fallback_max_players = int(props.get("max-players", "20"))
        process_running, pid, stale_pid_detected = self._get_process_snapshot_sync()
        game_port = self._get_game_port(props)
        game_port_listening = self._is_port_listening(game_port)
        healthy = process_running and game_port_listening

        if not process_running:
            snapshot = {
                "running": False,
                "process_running": False,
                "healthy": False,
                "state_reason": "stopped",
                "pid": pid,
                "players": [],
                "players_online": 0,
                "max_players": fallback_max_players,
                "last_updated": now,
                "stale": False,
                "error": None,
            }
            self.online_players_cache = snapshot
            self.online_players_cache_time = now
            return dict(snapshot)

        if not healthy:
            reason = "stale_pid" if stale_pid_detected else "starting"
            snapshot = {
                "running": True,
                "process_running": process_running,
                "healthy": False,
                "state_reason": reason,
                "pid": pid,
                "players": [],
                "players_online": 0,
                "max_players": fallback_max_players,
                "last_updated": now,
                "stale": False,
                "error": None,
            }
            self.online_players_cache = snapshot
            self.online_players_cache_time = now
            return dict(snapshot)

        if not self.online_players_refresh_lock.acquire(blocking=False):
            if cache is not None:
                stale_snapshot = dict(cache)
                stale_snapshot["stale"] = True
                return stale_snapshot
            self.online_players_refresh_lock.acquire()

        try:
            now = time.time()
            cache = self.online_players_cache
            if (
                not force_refresh
                and cache is not None
                and (now - self.online_players_cache_time) < STATUS_CACHE_TTL
            ):
                return dict(cache)

            rcon_config = get_rcon_config()
            if not rcon_config.enabled or not rcon_config.password:
                raise RuntimeError("RCON is not configured")

            rcon = RCONClient(rcon_config.host, rcon_config.port, rcon_config.password)
            try:
                if not rcon.connect():
                    raise RuntimeError("Failed to connect to RCON")
                parsed = self._parse_list_response(
                    rcon.send_command("list"),
                    fallback_max_players,
                )
            finally:
                rcon.disconnect()

            snapshot = {
                "running": True,
                "process_running": True,
                "healthy": True,
                "state_reason": "ok",
                "pid": pid,
                "players": parsed["players"],
                "players_online": parsed["players_online"],
                "max_players": parsed["max_players"],
                "response": parsed["response"],
                "last_updated": now,
                "stale": False,
                "error": None,
            }
            self.online_players_cache = snapshot
            self.online_players_cache_time = now
            return dict(snapshot)
        except Exception as exc:
            logger.warning("Online player snapshot refresh failed: %s", exc)
            if cache is not None:
                stale_snapshot = dict(cache)
                stale_snapshot["stale"] = True
                stale_snapshot["error"] = str(exc)
                return stale_snapshot
            return {
                "running": True,
                "process_running": True,
                "healthy": True,
                "state_reason": "ok",
                "pid": pid,
                "players": [],
                "players_online": 0,
                "max_players": fallback_max_players,
                "last_updated": now,
                "stale": True,
                "error": str(exc),
            }
        finally:
            self.online_players_refresh_lock.release()

    async def _wait_for_server_ready(self, timeout_sec: int, require_rcon_ready: bool) -> dict:
        """Wait until the process is alive and (optionally) RCON responds."""
        timeout_sec = max(1, int(timeout_sec))
        deadline = time.monotonic() + timeout_sec
        started_at = time.monotonic()
        checks = {
            "process_alive": False,
            "rcon_ready": False,
            "last_rcon_error": None,
            "elapsed_seconds": 0,
            "timeout_seconds": timeout_sec,
        }

        while time.monotonic() < deadline:
            if not self._is_server_running_sync():
                elapsed = time.monotonic() - started_at
                checks["elapsed_seconds"] = int(elapsed)
                if elapsed < PROCESS_BOOT_GRACE_SEC:
                    await asyncio.sleep(READY_POLL_INTERVAL_SEC)
                    continue
                self._delete_pid_file()
                return {
                    "success": False,
                    "error_code": "process_exited_early",
                    "error": "Server process exited before readiness checks completed",
                    "ready_checks": checks,
                }

            checks["process_alive"] = True
            checks["elapsed_seconds"] = int(time.monotonic() - started_at)

            if not require_rcon_ready:
                return {"success": True, "ready_checks": checks}

            rcon_ready, rcon_status = await asyncio.to_thread(self._probe_rcon_ready_once)
            if rcon_ready:
                checks["rcon_ready"] = True
                checks["last_rcon_error"] = None
                return {"success": True, "ready_checks": checks}

            checks["last_rcon_error"] = rcon_status
            await asyncio.sleep(READY_POLL_INTERVAL_SEC)

        checks["elapsed_seconds"] = int(time.monotonic() - started_at)
        return {
            "success": False,
            "error_code": "rcon_not_ready_timeout",
            "error": (
                f"Server started but did not become ready within {timeout_sec}s "
                f"(last_rcon_error={checks['last_rcon_error']})"
            ),
            "ready_checks": checks,
        }

    # ------------------------------------------------------------------
    # Server control
    # ------------------------------------------------------------------

    def invalidate_status_cache(self) -> None:
        with self.status_cache_lock:
            self.status_cache = None
            self.status_cache_time = 0

    async def start_server(
        self,
        wait_for_ready: bool = False,
        ready_timeout_sec: int = DEFAULT_READY_TIMEOUT_SEC,
        require_rcon_ready: bool = True,
    ) -> dict:
        """Start the Minecraft server as a detached process"""
        block = _operation_block("start server")
        if block:
            return block

        async with self.process_lock:
            self.invalidate_status_cache()
            if self._is_server_running_sync():
                return {"success": False, "error": "Server is already running"}

            server_dir = get_server_directory()
            start_script = get_start_script_path()
            latest_log = get_latest_log_path()

            if not start_script.exists():
                return {"success": False, "error": "start.sh not found"}

            try:
                # Cancel existing log tailer if any
                if self.log_reader_task and not self.log_reader_task.done():
                    self.log_reader_task.cancel()
                    try:
                        await asyncio.wait_for(self.log_reader_task, timeout=2.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass

                self.log_buffer.clear()

                # Start reading from CURRENT end of log file (skip old content)
                if latest_log.exists():
                    self.last_log_position = latest_log.stat().st_size
                    self.last_log_inode = latest_log.stat().st_ino
                else:
                    self.last_log_position = 0
                    self.last_log_inode = None

                separator_entry = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": "[CORA] =============================================="
                }
                start_entry = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": "[CORA] Starting Minecraft server..."
                }
                self.log_buffer.append(separator_entry)
                self.log_buffer.append(start_entry)

                for callback in self.log_subscribers:
                    try:
                        await callback(start_entry)
                    except Exception:
                        pass

                process = subprocess.Popen(
                    ["sh", str(start_script)],
                    cwd=str(server_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

                self._write_pid_file(process.pid)
                self.invalidate_status_cache()
                self.log_reader_task = asyncio.create_task(self._tail_log_file())

                result = {
                    "success": True,
                    "pid": process.pid,
                    "ready": False,
                    "message": "Server starting (detached mode)..."
                }

                if not wait_for_ready:
                    return result

                ready_result = await self._wait_for_server_ready(
                    timeout_sec=ready_timeout_sec,
                    require_rcon_ready=require_rcon_ready,
                )
                if not ready_result.get("success"):
                    return {
                        "success": False,
                        "pid": process.pid,
                        "ready": False,
                        "error": ready_result.get("error", "Server failed readiness checks"),
                        "error_code": ready_result.get("error_code"),
                        "ready_checks": ready_result.get("ready_checks"),
                    }

                result["ready"] = True
                result["message"] = "Server started and passed readiness checks"
                result["ready_checks"] = ready_result.get("ready_checks", {})
                return result

            except Exception as e:
                return {"success": False, "error": str(e)}

    async def _tail_log_file(self):
        """Background task to tail the server log file"""
        logger.info("Log file tailer started")
        self.last_message = ""
        self.last_message_time = 0.0

        try:
            while True:
                if not self._is_server_running_sync():
                    logger.info("Server stopped, tailer exiting")
                    stop_entry = {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "message": "[CORA] Server process stopped"
                    }
                    self.log_buffer.append(stop_entry)
                    for callback in self.log_subscribers:
                        try:
                            await callback(stop_entry)
                        except Exception:
                            pass
                    break

                latest_log = get_latest_log_path()
                if latest_log.exists():
                    try:
                        current_inode = latest_log.stat().st_ino
                        current_size = latest_log.stat().st_size

                        if self.last_log_inode is not None and (
                            current_inode != self.last_log_inode or current_size < self.last_log_position
                        ):
                            logger.warning(
                                f"Log file rotated (inode: {self.last_log_inode} -> {current_inode}, "
                                f"size: {self.last_log_position} -> {current_size})"
                            )
                            self.last_log_position = 0
                            rotation_entry = {
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "message": "[CORA] Log file rotated - new server session"
                            }
                            self.log_buffer.append(rotation_entry)
                            for callback in self.log_subscribers:
                                try:
                                    await callback(rotation_entry)
                                except Exception:
                                    pass

                        self.last_log_inode = current_inode

                        with open(latest_log, 'r', encoding='utf-8', errors='ignore') as f:
                            f.seek(self.last_log_position)
                            new_lines = f.readlines()
                            self.last_log_position = f.tell()

                            for line in new_lines:
                                raw_message = line.rstrip()
                                if not raw_message:
                                    continue

                                current_time = time.time()

                                if raw_message == self.last_message and (current_time - self.last_message_time) < 0.1:
                                    continue

                                self.last_message = raw_message
                                self.last_message_time = current_time

                                message = strip_minecraft_colors(raw_message)

                                time_match = re.match(r'\[(\d{2}:\d{2}:\d{2})', message)
                                timestamp = time_match.group(1) if time_match else datetime.now().strftime("%H:%M:%S")

                                log_entry = {"time": timestamp, "message": message}
                                self.log_buffer.append(log_entry)

                                if not self._should_filter_log(message):
                                    for callback in self.log_subscribers:
                                        try:
                                            await callback(log_entry)
                                        except Exception:
                                            pass

                    except Exception as e:
                        logger.error(f"Error reading log file: {e}")

                await asyncio.sleep(0.3)

        except asyncio.CancelledError:
            logger.info("Log tailer cancelled")
        except Exception as e:
            logger.error(f"Log tailer error: {e}")
        finally:
            logger.info("Log file tailer stopped")

    async def stop_server(self, force: bool = False) -> dict:
        """Stop the Minecraft server gracefully (detached mode)"""
        block = _operation_block("stop server")
        if block:
            return block

        async with self.process_lock:
            self.invalidate_status_cache()
            if not self._is_server_running_sync():
                return {"success": False, "error": "Server is not running"}

            pid = self._get_server_pid_sync()

            stop_entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": "[CORA] Stopping Minecraft server..."
            }
            self.log_buffer.append(stop_entry)
            for callback in self.log_subscribers:
                try:
                    await callback(stop_entry)
                except Exception:
                    pass

            self._save_console_history()

            try:
                rcon_config = get_rcon_config()
                if rcon_config.enabled and rcon_config.password:
                    try:
                        rcon = RCONClient(rcon_config.host, rcon_config.port, rcon_config.password)
                        if rcon.connect():
                            rcon.send_command("stop")
                            rcon.disconnect()

                            for i in range(30):
                                await asyncio.sleep(1)
                                if not self._is_server_running_sync():
                                    self._delete_pid_file()
                                    self.invalidate_status_cache()
                                    return {"success": True, "method": "rcon", "message": "Server stopped via RCON"}
                    except Exception as e:
                        logger.warning(f"RCON stop failed: {e}")

                if pid:
                    logger.info(f"Sending SIGTERM to PID {pid}")
                    os.kill(pid, signal.SIGTERM)

                    for i in range(15):
                        await asyncio.sleep(1)
                        if not self._is_server_running_sync():
                            self._delete_pid_file()
                            self.invalidate_status_cache()
                            return {"success": True, "method": "sigterm", "message": "Server stopped via SIGTERM"}

                if force and pid:
                    logger.warning(f"Force killing PID {pid}")
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

                    await asyncio.sleep(1)
                    self._delete_pid_file()
                    self.invalidate_status_cache()
                    return {"success": True, "method": "sigkill", "message": "Server force-killed"}

                return {"success": False, "error": "Could not stop server gracefully. Try force=true."}

            except Exception as e:
                return {"success": False, "error": str(e)}

    async def restart_server(
        self,
        ready_timeout_sec: int = DEFAULT_READY_TIMEOUT_SEC,
        require_rcon_ready: bool = True,
        start_retries: int = RESTART_START_RETRIES,
        retry_delay_sec: int = RESTART_RETRY_DELAY_SEC,
        source: str = "unknown",
    ) -> dict:
        """Restart the Minecraft server"""
        block = _operation_block("restart server")
        if block:
            return block

        restart_lock_handle: Optional[TextIO] = None
        async with self.restart_guard_lock:
            now = datetime.now()
            if self.restart_in_progress:
                return {
                    "success": False,
                    "error": "Restart already in progress",
                    "error_code": "restart_in_progress",
                }

            restart_lock_handle = self._acquire_restart_file_lock()
            if restart_lock_handle is None:
                if self._last_restart_lock_error:
                    return {
                        "success": False,
                        "error": f"Restart lock unavailable: {self._last_restart_lock_error}",
                        "error_code": "restart_lock_error",
                    }
                return {
                    "success": False,
                    "error": "Restart already in progress (another worker)",
                    "error_code": "restart_in_progress",
                }

            cooldown_remaining = self._restart_cooldown_remaining_seconds(now)
            if cooldown_remaining > 0:
                self._release_restart_file_lock(restart_lock_handle)
                restart_lock_handle = None
                return {
                    "success": False,
                    "error": f"Restart cooldown active. Retry after {cooldown_remaining}s",
                    "error_code": "restart_cooldown",
                    "retry_after_seconds": cooldown_remaining,
                    "last_restart_source": self.last_restart_source,
                }

            self.restart_in_progress = True

        restart_success = False
        try:
            restart_entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": f"[CORA] Restarting Minecraft server... (source={source})"
            }
            self.log_buffer.append(restart_entry)
            for callback in self.log_subscribers:
                try:
                    await callback(restart_entry)
                except Exception:
                    pass

            stop_result = await self.stop_server()

            if not stop_result["success"] and "not running" not in stop_result.get("error", ""):
                return {"success": False, "error": f"Failed to stop: {stop_result.get('error')}"}

            await asyncio.sleep(3)

            recent_entries = list(self.log_buffer)[-5:] if len(self.log_buffer) > 5 else list(self.log_buffer)
            self.log_buffer.clear()
            for entry in recent_entries:
                self.log_buffer.append(entry)

            max_attempts = max(1, int(start_retries) + 1)
            delay_sec = max(0, int(retry_delay_sec))
            last_start_result = None

            for attempt in range(1, max_attempts + 1):
                start_result = await self.start_server(
                    wait_for_ready=True,
                    ready_timeout_sec=ready_timeout_sec,
                    require_rcon_ready=require_rcon_ready,
                )
                start_result["restart_start_attempt"] = attempt
                last_start_result = start_result

                if start_result.get("success"):
                    if attempt > 1:
                        start_result["message"] = (
                            f"{start_result.get('message', 'Server restart completed')} "
                            f"(start retry {attempt - 1})"
                        )
                    restart_success = True
                    return start_result

                error_code = start_result.get("error_code")
                retryable = error_code == "process_exited_early"
                if attempt < max_attempts and retryable:
                    logger.warning(
                        "Restart start attempt %s/%s failed (%s), retrying in %ss",
                        attempt,
                        max_attempts,
                        start_result.get("error", "unknown"),
                        delay_sec,
                    )
                    if delay_sec > 0:
                        await asyncio.sleep(delay_sec)
                    continue

                break

            assert last_start_result is not None
            return {
                "success": False,
                "error": (
                    f"Failed to start after {last_start_result.get('restart_start_attempt', 1)} "
                    f"attempt(s): {last_start_result.get('error', 'Unknown error')}"
                ),
                "error_code": last_start_result.get("error_code"),
                "ready_checks": last_start_result.get("ready_checks"),
                "restart_start_attempt": last_start_result.get("restart_start_attempt", 1),
            }
        finally:
            async with self.restart_guard_lock:
                self.restart_in_progress = False
                if restart_success:
                    self.invalidate_status_cache()
                    self.last_restart_completed_at = datetime.now()
                    self.last_restart_source = source
                    self._save_restart_state()
            self._release_restart_file_lock(restart_lock_handle)

    async def send_command(self, command: str) -> dict:
        """Send a command to the server via RCON"""
        block = _operation_block("send RCON command", requires_rcon=True)
        if block:
            return block

        if not self._is_server_running_sync():
            return {"success": False, "error": "Server is not running"}

        rcon_config = get_rcon_config()
        if not rcon_config.enabled or not rcon_config.password:
            return {"success": False, "error": "RCON is not enabled. Enable it in server.properties and restart the server."}

        try:
            rcon = RCONClient(rcon_config.host, rcon_config.port, rcon_config.password)
            try:
                if not rcon.connect():
                    return {"success": False, "error": "Failed to connect to RCON"}
                response = rcon.send_command(command)
                clean_response = strip_minecraft_colors(response)
                self.invalidate_status_cache()
                return {"success": True, "response": clean_response, "method": "rcon"}
            finally:
                rcon.disconnect()
        except Exception as e:
            return {"success": False, "error": f"RCON error: {e}"}

    def get_server_status(self, force_refresh: bool = False) -> ServerStatus:
        """Get comprehensive server status with a short-lived shared snapshot."""
        now = time.time()
        if (
            not force_refresh
            and self.status_cache is not None
            and (now - self.status_cache_time) < STATUS_CACHE_TTL
        ):
            return replace(self.status_cache)

        with self.status_cache_lock:
            now = time.time()
            if (
                not force_refresh
                and self.status_cache is not None
                and (now - self.status_cache_time) < STATUS_CACHE_TTL
            ):
                return replace(self.status_cache)

            status = self._compute_server_status(now)
            self.status_cache = replace(status)
            self.status_cache_time = now
            return status

    def _compute_server_status(self, now: Optional[float] = None) -> ServerStatus:
        """Compute server status without consulting the full-status cache."""
        now = now or time.time()
        status = ServerStatus()
        props = load_server_properties()
        rcon_config = get_rcon_config()
        process_running, pid, stale_pid_detected = self._get_process_snapshot_sync()
        status.running = process_running  # Backwards-compatible alias
        status.process_running = process_running
        status.pid = pid
        status.game_port_listening = self._is_port_listening(self._get_game_port(props))
        status.rcon_port_listening = (
            self._is_port_listening(rcon_config.port) if rcon_config.enabled else False
        )
        status.healthy = status.process_running and status.game_port_listening

        if status.healthy:
            status.state_reason = "ok"
        elif stale_pid_detected:
            status.state_reason = "stale_pid"
        elif status.process_running and not status.game_port_listening:
            status.state_reason = "process_no_port"
        elif not status.process_running and status.game_port_listening:
            status.state_reason = "port_busy_no_process"
        elif status.process_running:
            status.state_reason = "starting"
        else:
            status.state_reason = "stopped"

        if status.process_running:
            snapshot = self.get_online_players_snapshot()
            status.players_online = snapshot.get("players_online", 0)
            status.max_players = snapshot.get("max_players", 20)
            status.players = snapshot.get("players", [])
            status.stale = bool(snapshot.get("stale", False))
            status.last_updated = snapshot.get("last_updated") or now
        else:
            self.online_players_cache = None
            self.online_players_cache_time = 0
            status.stale = False
            status.last_updated = now

        if status.max_players == 20:
            status.max_players = int(props.get("max-players", "20"))

        return status

    async def recover_server(
        self,
        ready_timeout_sec: int = DEFAULT_READY_TIMEOUT_SEC,
        require_rcon_ready: bool = True,
        start_retries: int = RESTART_START_RETRIES,
        retry_delay_sec: int = RESTART_RETRY_DELAY_SEC,
    ) -> dict:
        """
        Emergency recovery flow for "UI says running but server is unavailable":
        force-stop (if process exists) -> stale PID cleanup -> start with readiness checks.
        """
        block = _operation_block("recover server")
        if block:
            return block

        steps: list[dict] = []
        before = self.get_server_status()
        steps.append({
            "step": "precheck",
            "process_running": before.process_running,
            "healthy": before.healthy,
            "state_reason": before.state_reason,
            "pid": before.pid,
        })

        if before.healthy:
            return {
                "success": True,
                "message": "Server already healthy",
                "steps": steps,
                "server": {
                    "running": before.running,
                    "process_running": before.process_running,
                    "healthy": before.healthy,
                    "state_reason": before.state_reason,
                    "pid": before.pid,
                    "game_port_listening": before.game_port_listening,
                    "rcon_port_listening": before.rcon_port_listening,
                },
            }

        if before.process_running:
            stop_result = await self.stop_server(force=True)
            steps.append({
                "step": "force_stop",
                "success": bool(stop_result.get("success")),
                "error": stop_result.get("error"),
                "method": stop_result.get("method"),
            })
            if not stop_result.get("success") and "not running" not in str(stop_result.get("error", "")):
                return {
                    "success": False,
                    "error": f"Recovery failed to stop existing process: {stop_result.get('error')}",
                    "steps": steps,
                }
            await asyncio.sleep(2)

        stale_pid_removed = False
        pid_from_file = self._read_pid_file()
        if pid_from_file and not self._is_configured_minecraft_process(pid_from_file):
            self._delete_pid_file()
            stale_pid_removed = True
        steps.append({
            "step": "pid_cleanup",
            "stale_pid_removed": stale_pid_removed,
        })

        start_result = await self.restart_server(
            ready_timeout_sec=ready_timeout_sec,
            require_rcon_ready=require_rcon_ready,
            start_retries=start_retries,
            retry_delay_sec=retry_delay_sec,
        ) if before.process_running else await self.start_server(
            wait_for_ready=True,
            ready_timeout_sec=ready_timeout_sec,
            require_rcon_ready=require_rcon_ready,
        )

        steps.append({
            "step": "start",
            "success": bool(start_result.get("success")),
            "error": start_result.get("error"),
            "error_code": start_result.get("error_code"),
            "attempt": start_result.get("restart_start_attempt", 1),
        })

        if not start_result.get("success"):
            return {
                "success": False,
                "error": start_result.get("error", "Recovery failed to start server"),
                "error_code": start_result.get("error_code"),
                "steps": steps,
            }

        after = self.get_server_status()
        steps.append({
            "step": "postcheck",
            "healthy": after.healthy,
            "state_reason": after.state_reason,
            "process_running": after.process_running,
        })

        if not after.healthy:
            return {
                "success": False,
                "error": f"Recovery start returned success, but server is not healthy ({after.state_reason})",
                "steps": steps,
                "server": {
                    "running": after.running,
                    "process_running": after.process_running,
                    "healthy": after.healthy,
                    "state_reason": after.state_reason,
                    "pid": after.pid,
                    "game_port_listening": after.game_port_listening,
                    "rcon_port_listening": after.rcon_port_listening,
                },
            }

        return {
            "success": True,
            "message": "Server recovered successfully",
            "steps": steps,
            "server": {
                "running": after.running,
                "process_running": after.process_running,
                "healthy": after.healthy,
                "state_reason": after.state_reason,
                "pid": after.pid,
                "game_port_listening": after.game_port_listening,
                "rcon_port_listening": after.rcon_port_listening,
            },
        }

    def get_recent_logs(self, lines: int = 100, filtered: bool = True, offset: int = 0) -> list:
        """Get log entries with pagination support."""
        if filtered:
            all_logs = [log for log in self.log_buffer if not self._should_filter_log(log.get("message", ""))]
        else:
            all_logs = list(self.log_buffer)

        if offset > 0:
            if offset >= len(all_logs):
                return []
            older_logs = all_logs[:-offset]
            return older_logs[-lines:]
        else:
            return all_logs[-lines:]

    def subscribe_to_logs(self, callback: Callable):
        self.log_subscribers.append(callback)

    def unsubscribe_from_logs(self, callback: Callable):
        if callback in self.log_subscribers:
            self.log_subscribers.remove(callback)

    async def ensure_log_tailer_running(self):
        """Start log tailer if server is running (call on app startup)"""
        if self._is_server_running_sync():
            logger.info("Server already running, starting log tailer...")

            self.log_buffer.clear()
            latest_log = get_latest_log_path()

            if latest_log.exists():
                try:
                    for entry in read_latest_log(lines=100, filtered=False):
                        self.log_buffer.append(entry)
                    logger.info(f"Loaded {len(self.log_buffer)} recent logs from latest.log")
                except Exception as e:
                    logger.error(f"Failed to load recent logs: {e}")

            restart_marker = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": "[CORA] Web app restarted - reconnecting to server..."
            }
            self.log_buffer.append(restart_marker)

            if latest_log.exists():
                self.last_log_position = latest_log.stat().st_size

            if self.log_reader_task is None or self.log_reader_task.done():
                self.log_reader_task = asyncio.create_task(self._tail_log_file())
            return True
        return False


# =============================================================================
# Singleton + backwards-compatible public API
# =============================================================================

_manager = ServerManager()


def enable_rcon(password: str) -> bool:
    """Enable RCON in server.properties (requires server restart)"""
    if not password:
        return False
    server_properties = get_server_properties_path()
    if not server_properties.exists():
        return False

    with open(server_properties, "r") as f:
        content = f.read()

    replacements = {
        r"enable-rcon=\w+": "enable-rcon=true",
        r"rcon\.password=.*": f"rcon.password={password}",
    }

    for pattern, replacement in replacements.items():
        content = re.sub(pattern, replacement, content)

    with open(server_properties, "w") as f:
        f.write(content)

    return True


def update_start_script(new_jar_filename: str) -> bool:
    """Update start.sh with new JAR filename"""
    if not re.match(r'^paper-[\d.]+-\d+\.jar$', new_jar_filename):
        logger.warning(f"Rejected invalid JAR filename: {new_jar_filename}")
        return False

    start_script = get_start_script_path()
    if not start_script.exists():
        return False

    try:
        with open(start_script, "r") as f:
            content = f.read()

        new_content = re.sub(r"paper-[\d\.\-]+\.jar", new_jar_filename, content)

        with open(start_script, "w") as f:
            f.write(new_content)

        logger.info(f"Updated start.sh to use {new_jar_filename}")
        return True

    except Exception as e:
        logger.error(f"Failed to update start.sh: {e}")
        return False


def clamp_log_line_limit(lines: int, *, default: int = DEFAULT_LOG_LINE_LIMIT) -> int:
    try:
        parsed = int(lines)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, MAX_LOG_LINE_LIMIT))


def _format_log_line(line: str) -> Optional[dict]:
    clean_line = strip_minecraft_colors(line.rstrip())
    if not clean_line:
        return None
    time_match = re.match(r'\[(\d{2}:\d{2}:\d{2})', clean_line)
    timestamp = time_match.group(1) if time_match else ""
    return {"time": timestamp, "message": clean_line}


def _tail_text_lines(path: Path, lines: int) -> tuple[list[str], bool]:
    limit = clamp_log_line_limit(lines)
    if not path.exists():
        return [], False

    chunks: list[bytes] = []
    line_count = 0
    position = path.stat().st_size

    with open(path, "rb") as f:
        while position > 0 and line_count <= limit:
            read_size = min(LOG_TAIL_CHUNK_BYTES, position)
            position -= read_size
            f.seek(position)
            chunk = f.read(read_size)
            chunks.append(chunk)
            line_count += chunk.count(b"\n")

    content = b"".join(reversed(chunks)).decode("utf-8", errors="ignore")
    raw_lines = content.splitlines()
    return raw_lines[-limit:], position > 0 or len(raw_lines) > limit


def read_log_file_tail(log_path: Path, lines: int = DEFAULT_LOG_LINE_LIMIT) -> dict:
    """Read a bounded tail from a plain or gzip-compressed Minecraft log file."""
    limit = clamp_log_line_limit(lines)
    logs: deque = deque(maxlen=limit)
    source_lines = 0
    truncated = False

    if not log_path.exists():
        return {
            "logs": [],
            "count": 0,
            "line_limit": limit,
            "truncated": False,
            "source_lines": 0,
        }

    if log_path.name.endswith(".gz"):
        compressed_size = log_path.stat().st_size
        if compressed_size > MAX_COMPRESSED_LOG_BYTES:
            return {
                "logs": [],
                "count": 0,
                "line_limit": limit,
                "truncated": True,
                "source_lines": 0,
                "error": (
                    f"Compressed log archive is too large for web tailing "
                    f"({compressed_size} bytes > {MAX_COMPRESSED_LOG_BYTES} bytes)"
                ),
                "compressed_size": compressed_size,
                "max_compressed_bytes": MAX_COMPRESSED_LOG_BYTES,
            }

        with gzip.open(log_path, "rt", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                source_lines += 1
                entry = _format_log_line(raw_line)
                if entry is not None:
                    logs.append(entry)
        truncated = source_lines > limit
    else:
        raw_lines, truncated = _tail_text_lines(log_path, limit)
        source_lines = len(raw_lines)
        for raw_line in raw_lines:
            entry = _format_log_line(raw_line)
            if entry is not None:
                logs.append(entry)

    return {
        "logs": list(logs),
        "count": len(logs),
        "line_limit": limit,
        "truncated": truncated,
        "source_lines": source_lines,
    }


def read_latest_log(lines: int = 100, filtered: bool = True) -> list:
    """Read the latest.log file directly."""
    logs = []
    latest_log = get_latest_log_path()
    if latest_log.exists():
        try:
            raw_lines, _ = _tail_text_lines(latest_log, lines)
            for line in raw_lines:
                entry = _format_log_line(line)
                if entry is None:
                    continue
                if filtered and _manager._should_filter_log(entry["message"]):
                    continue
                logs.append(entry)
        except Exception as e:
            logs.append({"time": "", "message": f"Error reading log: {e}"})
    return logs


# --- Delegate to singleton (preserves existing call sites) ---

def is_server_running() -> bool:
    return _manager._is_server_running_sync()

def get_server_pid() -> Optional[int]:
    return _manager._get_server_pid_sync()

async def start_server(
    wait_for_ready: bool = False,
    ready_timeout_sec: int = DEFAULT_READY_TIMEOUT_SEC,
    require_rcon_ready: bool = True,
) -> dict:
    block = _operation_block("start server")
    if block:
        return block

    return await _manager.start_server(
        wait_for_ready=wait_for_ready,
        ready_timeout_sec=ready_timeout_sec,
        require_rcon_ready=require_rcon_ready,
    )

async def stop_server(force: bool = False) -> dict:
    block = _operation_block("stop server")
    if block:
        return block

    return await _manager.stop_server(force=force)

async def restart_server(
    ready_timeout_sec: int = DEFAULT_READY_TIMEOUT_SEC,
    require_rcon_ready: bool = True,
    start_retries: int = RESTART_START_RETRIES,
    retry_delay_sec: int = RESTART_RETRY_DELAY_SEC,
    source: str = "unknown",
) -> dict:
    block = _operation_block("restart server")
    if block:
        return block

    return await _manager.restart_server(
        ready_timeout_sec=ready_timeout_sec,
        require_rcon_ready=require_rcon_ready,
        start_retries=start_retries,
        retry_delay_sec=retry_delay_sec,
        source=source,
    )

async def recover_server(
    ready_timeout_sec: int = DEFAULT_READY_TIMEOUT_SEC,
    require_rcon_ready: bool = True,
    start_retries: int = RESTART_START_RETRIES,
    retry_delay_sec: int = RESTART_RETRY_DELAY_SEC,
) -> dict:
    block = _operation_block("recover server")
    if block:
        return block

    return await _manager.recover_server(
        ready_timeout_sec=ready_timeout_sec,
        require_rcon_ready=require_rcon_ready,
        start_retries=start_retries,
        retry_delay_sec=retry_delay_sec,
    )

async def send_command(command: str) -> dict:
    block = _operation_block("send RCON command", requires_rcon=True)
    if block:
        return block

    return await _manager.send_command(command)

def get_server_status(force_refresh: bool = False) -> ServerStatus:
    return _manager.get_server_status(force_refresh=force_refresh)

def get_online_players_snapshot(force_refresh: bool = False) -> dict:
    return _manager.get_online_players_snapshot(force_refresh=force_refresh)

def get_restart_gate_status() -> dict:
    return _manager.get_restart_gate_status()

def get_recent_logs(lines: int = 100, filtered: bool = True, offset: int = 0) -> list:
    return _manager.get_recent_logs(lines, filtered=filtered, offset=offset)

def subscribe_to_logs(callback: Callable):
    _manager.subscribe_to_logs(callback)

def unsubscribe_from_logs(callback: Callable):
    _manager.unsubscribe_from_logs(callback)

async def ensure_log_tailer_running():
    return await _manager.ensure_log_tailer_running()
