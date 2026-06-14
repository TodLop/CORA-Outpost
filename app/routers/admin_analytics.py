# app/routers/admin_analytics.py
"""
Admin Analytics Sub-Router — Server performance monitoring dashboard.

Provides historical metrics API, live WebSocket stream, and the dashboard page.
Registered as a sub-router under admin.py (inherits /minecraft/admin prefix).
"""

import asyncio
import json as json_module
import logging
import os
import time
from base64 import b64decode

from fastapi import APIRouter, Request, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import TimestampSigner

from app.core.config import TEMPLATES_DIR
from app.core.minecraft_access import require_minecraft_admin, is_minecraft_admin_email
from app.services import metrics_db
from app.services import server_metrics

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Time range presets (label → seconds)
RANGE_PRESETS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
    "90d": 90 * 24 * 3600,
    "1y": 365 * 24 * 3600,
}


def _format_resolution(seconds: int | None) -> str | None:
    """Format second-based resolution to short human-readable label."""
    if not seconds or seconds <= 0:
        return None
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _cpu_metadata() -> dict:
    """CPU semantics metadata shared by analytics responses."""
    logical_cores = server_metrics.get_cpu_logical_core_count()
    return {
        "cpu_percent_semantics": (
            "cpu_percent/cpu_percent_raw are raw process CPU percent "
            "(100 == one fully utilized logical core); "
            "cpu_percent_normalized is raw/logical_cores "
            "(100 == full host CPU capacity)."
        ),
        "cpu_logical_cores": logical_cores,
        "cpu_process_percent_max": server_metrics.get_process_cpu_percent_max(logical_cores),
    }


def _annotate_metric_cpu_fields(metric: dict, logical_cores: int) -> dict:
    """Add explicit CPU raw/normalized fields while preserving cpu_percent."""
    annotated = dict(metric)
    cpu_raw = annotated.get("cpu_percent")
    annotated["cpu_percent_raw"] = cpu_raw
    annotated["cpu_percent_normalized"] = (
        server_metrics.normalize_process_cpu_percent(cpu_raw, logical_cores=logical_cores)
        if cpu_raw is not None
        else None
    )
    return annotated


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_dashboard(request: Request, user_info: dict = Depends(require_minecraft_admin)):
    """Render the server analytics dashboard page."""
    return templates.TemplateResponse("admin/analytics.html", {
        "request": request,
        "user_info": user_info,
        "is_admin": True,
    })


@router.get("/api/analytics/metrics")
async def get_analytics_metrics(
    range: str = "1h",
    user_info: dict = Depends(require_minecraft_admin),
):
    """Get historical CPU/RAM metrics for the requested time range."""
    range_sec = RANGE_PRESETS.get(range, 3600)
    end = time.time()
    start = end - range_sec

    result = metrics_db.query_metrics_with_metadata(start, end)
    cpu_meta = _cpu_metadata()
    metrics = [
        _annotate_metric_cpu_fields(metric, cpu_meta["cpu_logical_cores"])
        for metric in result["metrics"]
    ]
    resolution_seconds = result.get("resolution_seconds")
    source_table = result.get("table")

    window = {
        "start": start,
        "end": end,
        "data_start": metrics[0]["timestamp"] if metrics else None,
        "data_end": metrics[-1]["timestamp"] if metrics else None,
        "timezone": "UTC",
    }
    metadata = {
        "table": source_table,
        "source": source_table,
        "resolution": _format_resolution(resolution_seconds),
        "resolution_seconds": resolution_seconds,
        "window": window,
        **cpu_meta,
    }

    return JSONResponse({
        "status": "ok",
        "range": range,
        "count": len(metrics),
        "table": source_table,
        "source_table": source_table,
        "resolution_seconds": resolution_seconds,
        "window": window,
        "metadata": metadata,
        "metrics": metrics,
    })


@router.get("/api/analytics/tps")
async def get_analytics_tps(
    range: str = "1h",
    user_info: dict = Depends(require_minecraft_admin),
):
    """Get historical TPS/MSPT data for the requested time range."""
    range_sec = RANGE_PRESETS.get(range, 3600)
    end = time.time()
    start = end - range_sec

    result = metrics_db.query_tps_mspt_with_metadata(start, end)
    points = result["metrics"]
    resolution_seconds = result.get("resolution_seconds")
    source_table = result.get("table")

    window = {
        "start": start,
        "end": end,
        "data_start": points[0]["timestamp"] if points else None,
        "data_end": points[-1]["timestamp"] if points else None,
        "timezone": "UTC",
    }
    metadata = {
        "table": source_table,
        "source": source_table,
        "resolution": _format_resolution(resolution_seconds),
        "resolution_seconds": resolution_seconds,
        "window": window,
    }

    return JSONResponse({
        "status": "ok",
        "range": range,
        "count": len(points),
        "table": source_table,
        "source_table": source_table,
        "resolution_seconds": resolution_seconds,
        "window": window,
        "metadata": metadata,
        "tps_metrics": points,
    })


@router.get("/api/analytics/disk")
async def get_analytics_disk(
    range: str = "30d",
    user_info: dict = Depends(require_minecraft_admin),
):
    """Get historical disk size data."""
    range_sec = RANGE_PRESETS.get(range, 30 * 24 * 3600)
    end = time.time()
    start = end - range_sec

    disk_data = metrics_db.query_disk_size(start, end)
    window = {
        "start": start,
        "end": end,
        "data_start": disk_data[0]["timestamp"] if disk_data else None,
        "data_end": disk_data[-1]["timestamp"] if disk_data else None,
        "timezone": "UTC",
    }
    resolution_seconds = int(getattr(server_metrics, "DISK_INTERVAL", 30 * 60))
    metadata = {
        "table": "disk_size",
        "source": "disk_size",
        "resolution": _format_resolution(resolution_seconds),
        "resolution_seconds": resolution_seconds,
        "window": window,
    }

    return JSONResponse({
        "status": "ok",
        "range": range,
        "count": len(disk_data),
        "table": "disk_size",
        "source_table": "disk_size",
        "resolution_seconds": resolution_seconds,
        "window": window,
        "metadata": metadata,
        "disk": disk_data,
    })


@router.get("/api/analytics/current")
async def get_analytics_current(user_info: dict = Depends(require_minecraft_admin)):
    """Get the latest metric snapshot."""
    latest = metrics_db.get_latest_metric()
    cpu_meta = _cpu_metadata()
    if latest:
        latest = _annotate_metric_cpu_fields(latest, cpu_meta["cpu_logical_cores"])
    disk = metrics_db.get_latest_disk_size()

    return JSONResponse({
        "status": "ok",
        "metric": latest,
        "disk_mb": disk,
        "metadata": cpu_meta,
    })


@router.websocket("/ws/minecraft/metrics")
async def websocket_metrics(websocket: WebSocket):
    """WebSocket endpoint for live metrics streaming (admin only)."""
    # WebSocket auth: manually parse session cookie (SessionMiddleware doesn't populate .session on WS)
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        await websocket.close(code=4003, reason="Server config error")
        return

    cookie_header = websocket.headers.get("cookie", "")
    session_cookie = None
    for cookie in cookie_header.split(";"):
        cookie = cookie.strip()
        if cookie.startswith("session="):
            session_cookie = cookie[len("session="):]
            break

    if not session_cookie:
        await websocket.close(code=4003, reason="No session cookie")
        return

    try:
        signer = TimestampSigner(secret_key)
        data = signer.unsign(session_cookie.encode("utf-8"), max_age=14 * 24 * 60 * 60)
        session_data = json_module.loads(b64decode(data))
    except Exception:
        await websocket.close(code=4003, reason="Invalid session")
        return

    user_info = session_data.get("user_info")
    if not user_info or not is_minecraft_admin_email(user_info.get("email", "")):
        await websocket.close(code=4003, reason="Forbidden")
        return

    await websocket.accept()

    metric_queue: asyncio.Queue = asyncio.Queue()

    async def on_metric(data: dict):
        await metric_queue.put(data)

    server_metrics.subscribe_to_metrics(on_metric)

    try:
        while True:
            try:
                metric = await asyncio.wait_for(metric_queue.get(), timeout=30.0)
                await websocket.send_json(metric)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Metrics WebSocket error", exc_info=True)
    finally:
        server_metrics.unsubscribe_from_metrics(on_metric)
