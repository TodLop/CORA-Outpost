"""Resolve request-aware favicon assets per module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.modules.registry import ModuleRegistry

DEFAULT_FAVICON_SET = "default"
STATIC_FAVICON_ROOT = Path(__file__).resolve().parents[1] / "static" / "favicon"
REQUIRED_FAVICON_FILES = (
    "favicon-16x16.png",
    "favicon-32x32.png",
    "apple-touch-icon.png",
    "favicon.ico",
    "site.webmanifest",
)

FAVICON_THEME_COLOR_FALLBACK = "#475569"

# Legacy fallback map for modules that do not define favicon_set.
MODULE_FAVICON_BY_SLUG: dict[str, str] = {
    "codex": "codex",
    "minecraft_admin": "minecraft_admin",
    "minecraft_runtime": "minecraft_admin",
}

# Optional path overrides for non-module routes.
PATH_FAVICON_OVERRIDES: tuple[tuple[str, str], ...] = ()

_THEME_COLOR_CACHE: dict[str, str] = {}


def _path_matches_prefix(path: str, prefix: str) -> bool:
    if prefix == "/":
        return path == "/"

    normalized = prefix.rstrip("/") or "/"
    return path == normalized or path.startswith(f"{normalized}/")


def _favicon_override_from_path(path: str) -> str | None:
    for prefix, favicon_set in PATH_FAVICON_OVERRIDES:
        if _path_matches_prefix(path, prefix):
            return favicon_set
    return None


def _route_prefix_pairs(registry: ModuleRegistry | None) -> tuple[tuple[str, str], ...]:
    if not registry:
        return ()

    pairs: list[tuple[str, str]] = []
    for module in registry.all_modules:
        for prefix in module.route_prefixes:
            if prefix.startswith("/"):
                pairs.append((prefix, module.slug))

    return tuple(sorted(pairs, key=lambda item: len(item[0]), reverse=True))


def _module_slug_from_path(path: str, registry: ModuleRegistry | None) -> str | None:
    for prefix, module_slug in _route_prefix_pairs(registry):
        if _path_matches_prefix(path, prefix):
            return module_slug
    return None


def _favicon_set_exists(favicon_set: str) -> bool:
    base = STATIC_FAVICON_ROOT / favicon_set
    if not base.is_dir():
        return False

    for filename in REQUIRED_FAVICON_FILES:
        if not (base / filename).is_file():
            return False

    return True


def _normalize_favicon_set(favicon_set: str) -> str:
    if _favicon_set_exists(favicon_set):
        return favicon_set

    if _favicon_set_exists(DEFAULT_FAVICON_SET):
        return DEFAULT_FAVICON_SET

    return favicon_set


def _favicon_set_for_slug(slug: str | None, registry: ModuleRegistry | None) -> str:
    if not slug:
        return DEFAULT_FAVICON_SET

    if registry:
        for module in registry.all_modules:
            if module.slug == slug and module.favicon_set:
                return module.favicon_set

    if slug in MODULE_FAVICON_BY_SLUG:
        return MODULE_FAVICON_BY_SLUG[slug]

    # Final convenience fallback: if a slug-named favicon folder exists,
    # it can be used without code changes.
    return slug


def _theme_color_for_set(favicon_set: str) -> str:
    cached = _THEME_COLOR_CACHE.get(favicon_set)
    if cached:
        return cached

    manifest_path = STATIC_FAVICON_ROOT / favicon_set / "site.webmanifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        color = FAVICON_THEME_COLOR_FALLBACK
    else:
        theme_color = manifest.get("theme_color")
        color = theme_color if isinstance(theme_color, str) and theme_color.strip() else FAVICON_THEME_COLOR_FALLBACK

    _THEME_COLOR_CACHE[favicon_set] = color
    return color


def _build_favicon_payload(favicon_set: str) -> dict[str, str]:
    normalized = _normalize_favicon_set(favicon_set)
    base = f"/static/favicon/{normalized}"
    return {
        "set": normalized,
        "png16": f"{base}/favicon-16x16.png",
        "png32": f"{base}/favicon-32x32.png",
        "apple_touch": f"{base}/apple-touch-icon.png",
        "ico": f"{base}/favicon.ico",
        "manifest": f"{base}/site.webmanifest",
        "theme_color": _theme_color_for_set(normalized),
    }


def resolve_favicon_payload(request: Request, registry: ModuleRegistry | None = None) -> dict[str, str]:
    """Return favicon metadata for the incoming request."""
    path = request.url.path or "/"

    module_slug = _module_slug_from_path(path, registry)
    favicon_set = _favicon_set_for_slug(module_slug, registry)

    path_override = _favicon_override_from_path(path)
    if path_override:
        favicon_set = path_override

    return _build_favicon_payload(favicon_set)
