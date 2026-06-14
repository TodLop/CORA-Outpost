# app/services/server_metrics.py
"""
Minecraft Server Metrics Collector

Collects CPU%, RAM, disk size on scheduled intervals and broadcasts
live data to WebSocket subscribers.

Lifecycle: start_scheduler() / stop_scheduler() — called from app lifespan.
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Callable, List, Optional

import psutil

from app.services import minecraft_server, minecraft_settings
from app.services import metrics_db

logger = logging.getLogger(__name__)

# Canonical collection intervals (seconds). These feed analytics storage and
# must not depend on browser visibility or live WebSocket subscribers.
CANONICAL_METRICS_INTERVAL = 3
CANONICAL_TPS_INTERVAL = 10  # Paper /tps + /mspt polling

# Backward-compatible aliases used by tests/admin metadata.
METRICS_INTERVAL = CANONICAL_METRICS_INTERVAL
TPS_INTERVAL = CANONICAL_TPS_INTERVAL
DISK_INTERVAL = 30 * 60   # 30 minutes
DOWNSAMPLE_INTERVAL = 3600  # 1 hour

# Paper /mspt first bucket (5s avg/min/max), supports optional icon and newlines.
_MSPT_5S_RE = re.compile(
    r'from last 5s,\s*10s,\s*1m:\s*(?:[^\d\s]\s*)?'
    r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)',
    flags=re.IGNORECASE,
)
_MSPT_TRIPLE_FALLBACK_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)'
)
_MINECRAFT_FORMATTING_RE = re.compile(r'(?:\x1b\[[0-9;]*m|[§&][0-9A-FK-ORa-fk-or])')

# Latest TPS/MSPT values (updated by _tps_loop, consumed by _metrics_loop)
_latest_tps: Optional[float] = None
_latest_mspt: Optional[float] = None
_latest_live_metric: Optional[dict] = None

# Subscriber list for live metric broadcasts
_metric_subscribers: List[Callable] = []


def subscribe_to_metrics(callback: Callable):
    _metric_subscribers.append(callback)


def unsubscribe_from_metrics(callback: Callable):
    if callback in _metric_subscribers:
        _metric_subscribers.remove(callback)


async def _broadcast_metric(data: dict):
    """Send metric to all subscribers (WebSocket handlers)."""
    for callback in list(_metric_subscribers):
        try:
            await callback(data)
        except Exception:
            logger.debug("Failed to broadcast to subscriber, removing")
            try:
                _metric_subscribers.remove(callback)
            except ValueError:
                pass


def _rounded_optional(value: Optional[float], digits: int) -> Optional[float]:
    return round(value, digits) if value is not None else None


def _build_live_metric_payload(
    *,
    timestamp: float,
    cpu_raw: float,
    cpu_normalized: float,
    ram_mb: float,
    players: int,
    tps: Optional[float],
    mspt: Optional[float],
) -> dict:
    """Build the live WebSocket payload from a canonical metric sample."""
    return {
        "type": "metric",
        "timestamp": timestamp,
        "cpu_percent": round(cpu_raw, 1),  # backward-compatible raw value
        "cpu_percent_raw": round(cpu_raw, 1),
        "cpu_percent_normalized": round(cpu_normalized, 1),
        "ram_mb": round(ram_mb, 1),
        "players": players,
        "tps": _rounded_optional(tps, 2),
        "mspt": _rounded_optional(mspt, 2),
        "collection_role": "canonical",
        "live_source": "in_memory",
    }


def _remember_latest_live_metric(payload: dict):
    global _latest_live_metric
    _latest_live_metric = dict(payload)


def _refresh_latest_live_tps_mspt():
    if _latest_live_metric is None:
        return
    _latest_live_metric["tps"] = _rounded_optional(_latest_tps, 2)
    _latest_live_metric["mspt"] = _rounded_optional(_latest_mspt, 2)


def _record_canonical_metric(
    *,
    cpu_raw: float,
    ram_mb: float,
    players: int,
    tps: Optional[float],
    mspt: Optional[float],
):
    """Write the canonical analytics sample. This is independent of live UI state."""
    metrics_db.insert_raw_metric(cpu_raw, ram_mb, players, tps=tps, mspt=mspt)


async def _publish_live_metric(payload: dict):
    _remember_latest_live_metric(payload)
    await _broadcast_metric(payload)


async def _record_and_publish_metric_sample(
    *,
    cpu_raw: float,
    cpu_normalized: float,
    ram_mb: float,
    players: int,
):
    """Persist a canonical sample, then publish the corresponding live snapshot."""
    tps = _latest_tps
    mspt = _latest_mspt
    _record_canonical_metric(
        cpu_raw=cpu_raw,
        ram_mb=ram_mb,
        players=players,
        tps=tps,
        mspt=mspt,
    )
    payload = _build_live_metric_payload(
        timestamp=time.time(),
        cpu_raw=cpu_raw,
        cpu_normalized=cpu_normalized,
        ram_mb=ram_mb,
        players=players,
        tps=tps,
        mspt=mspt,
    )
    await _publish_live_metric(payload)


def _get_java_process() -> Optional[psutil.Process]:
    """Find the Minecraft server's Java process by PID from minecraft_server module."""
    status = minecraft_server.get_server_status()
    if not status.running or not status.pid:
        return None
    try:
        proc = psutil.Process(status.pid)
        if proc.is_running():
            return proc
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def get_cpu_logical_core_count() -> int:
    """Return the host logical core count used for CPU normalization."""
    return max(1, int(psutil.cpu_count(logical=True) or 1))


def get_process_cpu_percent_max(logical_cores: Optional[int] = None) -> float:
    """
    Max process CPU percent in psutil "raw" units.

    In raw units, 100 means one fully utilized logical core.
    """
    cores = logical_cores or get_cpu_logical_core_count()
    return float(cores * 100)


def normalize_process_cpu_percent(
    cpu_percent_raw: Optional[float],
    logical_cores: Optional[int] = None,
) -> Optional[float]:
    """
    Normalize raw process CPU percent to total-host-capacity percent.

    Example: on an 8-core host, raw=400 means normalized=50.
    """
    if cpu_percent_raw is None:
        return None
    cores = logical_cores or get_cpu_logical_core_count()
    return float(cpu_percent_raw) / max(1, cores)


def sample_process_cpu_percent(
    proc: psutil.Process,
    logical_cores: Optional[int] = None,
) -> tuple[float, float]:
    """Read process CPU and return (raw, normalized)."""
    raw = float(proc.cpu_percent(interval=None))
    normalized = normalize_process_cpu_percent(raw, logical_cores=logical_cores)
    return raw, (normalized if normalized is not None else 0.0)


# ─── Scheduler Tasks ──────────────────────────────────────────────

_metrics_task: Optional[asyncio.Task] = None
_tps_task: Optional[asyncio.Task] = None
_disk_task: Optional[asyncio.Task] = None
_downsample_task: Optional[asyncio.Task] = None


def _parse_tps(text: str) -> Optional[float]:
    """
    Parse Paper's /tps output for the 1m TPS value.

    Expected format:
        TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0
    """
    if not text:
        return None

    normalized = _MINECRAFT_FORMATTING_RE.sub('', text)
    match = re.search(r'TPS from last\s+([^:]+):\s*([^\n]+)', normalized, flags=re.IGNORECASE)
    if match:
        bucket_labels = [label.strip().lower() for label in match.group(1).split(',')]
        values = [float(value) for value in re.findall(r'\*?(\d+(?:\.\d+)?)', match.group(2))]
        if values:
            label_count = min(len(bucket_labels), len(values))
            for idx in range(label_count):
                if '1m' in bucket_labels[idx]:
                    return values[idx]
            return values[0]

    fallback = re.search(r'\bTPS\s*:\s*\*?(\d+(?:\.\d+)?)', normalized, flags=re.IGNORECASE)
    if fallback:
        return float(fallback.group(1))
    return None


def _parse_mspt(text: str) -> Optional[float]:
    """
    Parse Paper's /mspt output for the avg MSPT from the 5s bucket.

    Expected format:
        Server tick times (avg/min/max) from last 5s, 10s, 1m:
        ◴ 11.6/6.6/18.8, 11.8/4.8/76.2, 12.0/4.8/88.8

    Returns the avg (first value) from the 5s bucket.
    """
    if not text:
        return None

    normalized = _MINECRAFT_FORMATTING_RE.sub('', text)
    match = _MSPT_5S_RE.search(normalized)
    if not match:
        # Fallback for minor format drift while still extracting first avg/min/max triple.
        match = _MSPT_TRIPLE_FALLBACK_RE.search(normalized)
    if match:
        return float(match.group(1))

    mspt_line = re.search(r'MSPT(?:\s+from\s+last\s+[^:]+)?:\s*([^\n]+)', normalized, flags=re.IGNORECASE)
    if mspt_line:
        values = re.findall(r'(\d+(?:\.\d+)?)', mspt_line.group(1))
        if values:
            return float(values[0])
    return None


async def _metrics_loop():
    """Collect canonical CPU/RAM samples every CANONICAL_METRICS_INTERVAL seconds."""
    proc: Optional[psutil.Process] = None
    prev_pid: Optional[int] = None
    logical_cores = get_cpu_logical_core_count()

    while True:
        try:
            status = minecraft_server.get_server_status()

            if not status.running or not status.pid:
                proc = None
                prev_pid = None
                await asyncio.sleep(CANONICAL_METRICS_INTERVAL)
                continue

            # Re-acquire process handle if PID changed (server restarted)
            if status.pid != prev_pid:
                try:
                    proc = psutil.Process(status.pid)
                    # Prime the cpu_percent counter (first call always returns 0)
                    proc.cpu_percent(interval=None)
                    prev_pid = status.pid
                    await asyncio.sleep(CANONICAL_METRICS_INTERVAL)
                    continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    proc = None
                    prev_pid = None
                    await asyncio.sleep(CANONICAL_METRICS_INTERVAL)
                    continue

            if proc is None:
                await asyncio.sleep(CANONICAL_METRICS_INTERVAL)
                continue

            try:
                cpu_raw, cpu_normalized = sample_process_cpu_percent(
                    proc, logical_cores=logical_cores
                )
                mem_info = proc.memory_info()
                ram_mb = mem_info.rss / (1024 * 1024)
                players = status.players_online or 0

                await _record_and_publish_metric_sample(
                    cpu_raw=cpu_raw,
                    cpu_normalized=cpu_normalized,
                    ram_mb=ram_mb,
                    players=players,
                )

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                proc = None
                prev_pid = None

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Error in metrics collection loop", exc_info=True)

        await asyncio.sleep(CANONICAL_METRICS_INTERVAL)


async def _tps_loop():
    """Collect TPS/MSPT from Paper's built-in commands via RCON."""
    global _latest_tps, _latest_mspt

    while True:
        try:
            status = minecraft_server.get_server_status()
            if status.running:
                # TPS
                tps_result = await minecraft_server.send_command("tps")
                if tps_result.get("success"):
                    tps = _parse_tps(tps_result["response"])
                    if tps is not None:
                        _latest_tps = tps
                        _refresh_latest_live_tps_mspt()
                    else:
                        logger.warning("Failed to parse TPS from: %s", tps_result["response"])
                else:
                    _latest_tps = None
                    _refresh_latest_live_tps_mspt()

                # MSPT
                mspt_result = await minecraft_server.send_command("mspt")
                if mspt_result.get("success"):
                    mspt = _parse_mspt(mspt_result["response"])
                    if mspt is not None:
                        _latest_mspt = mspt
                        _refresh_latest_live_tps_mspt()
                    else:
                        logger.warning("Failed to parse MSPT from: %s", mspt_result["response"])
                else:
                    _latest_mspt = None
                    _refresh_latest_live_tps_mspt()
            else:
                _latest_tps = None
                _latest_mspt = None
                _refresh_latest_live_tps_mspt()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Error in TPS collection loop", exc_info=True)
            _latest_tps = None
            _latest_mspt = None
            _refresh_latest_live_tps_mspt()

        await asyncio.sleep(CANONICAL_TPS_INTERVAL)


async def _disk_loop():
    """Measure Minecraft server directory size every DISK_INTERVAL."""
    while True:
        try:
            server_path = minecraft_settings.get_server_directory()
            if server_path.exists():
                # Run in thread to avoid blocking the event loop
                total_bytes = await asyncio.to_thread(_calculate_dir_size, server_path)
                size_mb = total_bytes / (1024 * 1024)
                metrics_db.insert_disk_size(size_mb)
                logger.debug("Disk size recorded: %.1f MB", size_mb)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Error in disk size collection", exc_info=True)

        await asyncio.sleep(DISK_INTERVAL)


def _calculate_dir_size(path: Path) -> int:
    """Calculate total size of a directory (runs in thread)."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


async def _downsample_loop():
    """Run downsampling immediately, then every DOWNSAMPLE_INTERVAL."""
    while True:
        try:
            await asyncio.to_thread(metrics_db.downsample)
            logger.debug("Metrics downsampling completed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Error in downsample loop", exc_info=True)
        await asyncio.sleep(DOWNSAMPLE_INTERVAL)


# ─── Lifecycle ────────────────────────────────────────────────────

async def start_scheduler():
    """Start all metrics collection tasks."""
    global _metrics_task, _tps_task, _disk_task, _downsample_task

    # Initialize the database
    metrics_db.init_db()

    _metrics_task = asyncio.create_task(_metrics_loop())
    _tps_task = asyncio.create_task(_tps_loop())
    _disk_task = asyncio.create_task(_disk_loop())
    _downsample_task = asyncio.create_task(_downsample_loop())

    logger.info("Server metrics collector started")


async def stop_scheduler():
    """Stop all metrics collection tasks."""
    global _metrics_task, _tps_task, _disk_task, _downsample_task

    for task, name in [(_metrics_task, "metrics"), (_tps_task, "tps"), (_disk_task, "disk"), (_downsample_task, "downsample")]:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    _metrics_task = None
    _tps_task = None
    _disk_task = None
    _downsample_task = None

    logger.info("Server metrics collector stopped")
