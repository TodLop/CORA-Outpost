import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import APP_VERSION, ENV_FILE, STATIC_DIR, TEMPLATES_DIR
from app.core.deployment_identity import get_public_site_identity
from app.core.favicon import resolve_favicon_payload

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await app.state.module_registry.startup_enabled()
    except Exception:
        logger.error("Failed to run module startup hooks", exc_info=True)

    try:
        from app.services import permissions as permissions_service

        permissions_service.migrate_from_v1()
    except Exception:
        logger.error("Failed to run RBAC migration", exc_info=True)

    yield

    try:
        await app.state.module_registry.shutdown_enabled()
    except Exception:
        logger.error("Failed to run module shutdown hooks", exc_info=True)

    logger.info("App shutting down")


def create_app():
    load_dotenv(dotenv_path=ENV_FILE)

    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        raise ValueError("ERROR: SECRET_KEY is missing. Copy .env.example to .env and set SECRET_KEY.")

    app = FastAPI(
        title="CORA Minecraft Admin",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.exception_handler(StarletteHTTPException)
    async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
        accept_header = request.headers.get("accept", "")
        is_browser_request = "text/html" in accept_header
        if exc.status_code == 404 and is_browser_request:
            return templates.TemplateResponse("error.html", {"request": request}, status_code=404)
        if exc.status_code == 403 and is_browser_request and request.url.path.startswith("/minecraft/plugins"):
            return templates.TemplateResponse(
                "plugins/access_denied.html",
                {"request": request, "user_info": getattr(request.state, "user", {})},
                status_code=403,
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_middleware(SessionMiddleware, secret_key=secret_key)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(get_public_site_identity().trusted_hosts),
    )

    @app.middleware("http")
    async def favicon_middleware(request: Request, call_next):
        registry = getattr(request.app.state, "module_registry", None)
        request.state.favicon = resolve_favicon_payload(request, registry=registry)
        return await call_next(request)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    else:
        logger.warning("Static directory not found at %s", STATIC_DIR)

    from app.modules.registry import init_registry
    from app.routers import auth, status

    module_registry = init_registry()
    app.state.module_registry = module_registry
    module_registry.mount_enabled(app)

    app.include_router(auth.router, tags=["Authentication"])
    app.include_router(status.router, tags=["Status"])

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return RedirectResponse(url="/minecraft/admin")

    return app
