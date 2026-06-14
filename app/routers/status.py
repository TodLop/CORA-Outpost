"""Minimal health/status router for the public admin extract."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import APP_VERSION

router = APIRouter()


@router.get("/status")
async def status_page():
    return JSONResponse({"status": "ok", "app": "CORA Minecraft Admin", "version": APP_VERSION})


@router.get("/api/status/health")
async def health():
    return JSONResponse({"status": "ok"})
