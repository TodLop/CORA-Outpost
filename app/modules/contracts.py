"""Module contracts for route registration and hub navigation."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Callable, Literal

from fastapi import APIRouter

AccessLevel = Literal["public", "auth", "staff", "admin", "owner"]
ModuleStatus = Literal["online", "offline", "coming-soon"]
AsyncHook = Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class ModuleNav:
    """Navigation metadata used by the landing page."""

    slug: str
    name: str
    description: str
    icon: str
    color: str
    status: ModuleStatus
    url: str
    version: str
    access: AccessLevel = "public"
    external: bool = False
    locked_text: str = "Restricted"
    order: int = 100

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "status": self.status,
            "url": self.url,
            "version": self.version,
            "access": self.access,
            "external": self.external,
            "locked_text": self.locked_text,
            "order": self.order,
        }


@dataclass(frozen=True)
class ModuleContract:
    """Contract for one independently toggleable module."""

    slug: str
    display_name: str
    enabled_by_default: bool
    route_prefixes: tuple[str, ...]
    routers: tuple[APIRouter, ...]
    favicon_set: str = "default"
    nav: ModuleNav | None = None
    depends_on: tuple[str, ...] = ()
    healthcheck: Callable[[], ModuleStatus] | None = None
    startup: tuple[AsyncHook, ...] = ()
    shutdown: tuple[AsyncHook, ...] = ()
