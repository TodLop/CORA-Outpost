"""Minecraft admin module definition for the public extraction."""

from app.core.deployment_identity import get_public_site_identity
from app.modules.contracts import ModuleContract, ModuleNav, ModuleStatus
from app.routers.admin import router as admin_router
from app.routers.backend_docs import router as backend_docs_router
from app.routers.plugin_docs import router as plugin_docs_router
from app.routers.staff import router as staff_router


_PUBLIC_IDENTITY = get_public_site_identity()


def minecraft_healthcheck() -> ModuleStatus:
    from app.services.minecraft_server import get_server_status
    status = get_server_status()
    return "online" if status.healthy else "offline"


module = ModuleContract(
    slug="minecraft_admin",
    display_name="Minecraft Admin",
    enabled_by_default=True,
    route_prefixes=(
        "/minecraft/admin",
        "/minecraft/staff",
        "/minecraft/plugins",
        "/minecraft/backend-docs",
    ),
    routers=(
        admin_router,
        staff_router,
        plugin_docs_router,
        backend_docs_router,
    ),
    favicon_set="minecraft_admin",
    nav=ModuleNav(
        slug="minecraft_admin",
        name="Minecraft Admin",
        description=(
            "Minecraft Paper server operations dashboard, staff panel, plugin docs, "
            "and backend runbook docs."
        ),
        icon="gamepad-2",
        color="green",
        status="online",
        url="/minecraft/admin",
        version=_PUBLIC_IDENTITY.nearoutpost_server_address,
        access="admin",
        external=False,
        order=40,
    ),
    depends_on=("session", "auth", "rbac"),
    healthcheck=minecraft_healthcheck,
)
