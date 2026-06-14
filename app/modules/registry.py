"""Fail-closed runtime registry for the public Minecraft admin extract."""

from __future__ import annotations

import logging
import os
from collections import OrderedDict

from fastapi import FastAPI

from app.modules.contracts import ModuleContract, ModuleStatus
from app.modules.minecraft_admin.module import module as minecraft_admin_module
from app.modules.minecraft_runtime.module import module as minecraft_runtime_module

logger = logging.getLogger(__name__)

ALLOWED_MODULES_IN_ORDER: tuple[ModuleContract, ...] = (
    minecraft_admin_module,
    minecraft_runtime_module,
)
DEFAULT_ENABLED_MODULES = frozenset({"minecraft_admin"})


class ModuleRegistry:
    """Holds the explicitly allowed admin-only module set."""

    def __init__(self, all_modules: tuple[ModuleContract, ...], enabled_slugs: set[str]):
        ordered = OrderedDict((module.slug, module) for module in all_modules)
        self._all_modules = ordered
        self._enabled_slugs = {slug for slug in enabled_slugs if slug in ordered}

    @classmethod
    def from_environment(cls) -> "ModuleRegistry":
        raw_enabled = os.getenv("ENABLED_MODULES", "").strip()
        all_slugs = {module.slug for module in ALLOWED_MODULES_IN_ORDER}

        if not raw_enabled:
            return cls(ALLOWED_MODULES_IN_ORDER, set(DEFAULT_ENABLED_MODULES))

        requested = {item.strip().lower() for item in raw_enabled.split(",") if item.strip()}
        if requested.intersection({"*", "all"}):
            logger.warning("Ignoring broad ENABLED_MODULES request in public admin extract.")
            requested = set(DEFAULT_ENABLED_MODULES)

        unknown = sorted(requested - all_slugs)
        if unknown:
            logger.warning("Ignoring unknown modules from ENABLED_MODULES: %s", ", ".join(unknown))

        enabled = requested.intersection(all_slugs) or set(DEFAULT_ENABLED_MODULES)
        return cls(ALLOWED_MODULES_IN_ORDER, enabled)

    @property
    def all_modules(self) -> tuple[ModuleContract, ...]:
        return tuple(self._all_modules.values())

    @property
    def enabled_modules(self) -> tuple[ModuleContract, ...]:
        return tuple(module for module in self._all_modules.values() if module.slug in self._enabled_slugs)

    @property
    def enabled_slugs(self) -> set[str]:
        return set(self._enabled_slugs)

    def is_enabled(self, slug: str) -> bool:
        return slug in self._enabled_slugs

    def resolve_status(self, module: ModuleContract) -> ModuleStatus:
        if module.healthcheck is None:
            return module.nav.status if module.nav is not None else "online"
        try:
            return module.healthcheck()
        except Exception:
            logger.exception("Module healthcheck failed: %s", module.slug)
            return "offline"

    def missing_dependencies(self, module: ModuleContract) -> list[str]:
        if module.slug not in self._enabled_slugs:
            return []
        return [
            dependency
            for dependency in module.depends_on
            if dependency in self._all_modules and dependency not in self._enabled_slugs
        ]

    def mount_enabled(self, app: FastAPI) -> None:
        for module in self.enabled_modules:
            for router in module.routers:
                app.include_router(router)

    async def startup_enabled(self) -> None:
        for module in self.enabled_modules:
            for hook in module.startup:
                try:
                    await hook()
                except Exception:
                    logger.exception("Module startup hook failed: %s.%s", module.slug, getattr(hook, "__name__", hook.__class__.__name__))

    async def shutdown_enabled(self) -> None:
        for module in reversed(self.enabled_modules):
            for hook in module.shutdown:
                try:
                    await hook()
                except Exception:
                    logger.exception("Module shutdown hook failed: %s.%s", module.slug, getattr(hook, "__name__", hook.__class__.__name__))

    def to_snapshot(self) -> list[dict]:
        return [
            {
                "slug": module.slug,
                "display_name": module.display_name,
                "enabled": module.slug in self._enabled_slugs,
                "status": self.resolve_status(module) if module.slug in self._enabled_slugs else "disabled",
                "route_prefixes": list(module.route_prefixes),
                "favicon_set": module.favicon_set,
                "depends_on": list(module.depends_on),
                "missing_dependencies": self.missing_dependencies(module),
                "has_nav": module.nav is not None,
            }
            for module in self.all_modules
        ]


_registry: ModuleRegistry | None = None


def init_registry() -> ModuleRegistry:
    global _registry
    _registry = ModuleRegistry.from_environment()
    return _registry


def get_registry() -> ModuleRegistry:
    global _registry
    if _registry is None:
        _registry = ModuleRegistry.from_environment()
    return _registry
