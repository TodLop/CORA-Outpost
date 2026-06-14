"""Deployment-specific identity defaults and environment overrides."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlparse

from dotenv import load_dotenv


load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)


DEFAULT_MAIN_BASE_URL = "http://localhost:8000"
DEFAULT_NEAROUTPOST_BASE_URL = "http://localhost:8000"
DEFAULT_NEAROUTPOST_HOST_MARKERS = ("localhost",)
DEFAULT_NEAROUTPOST_SERVER_ADDRESS = "minecraft.example.com"
DEFAULT_NEAROUTPOST_SUPPORT_EMAIL = "admin@example.com"
DEFAULT_NEAROUTPOST_SITE_NAME = "Minecraft Admin"
DEFAULT_NEAROUTPOST_SITE_SHORT_NAME = "Minecraft Admin"
DEFAULT_NEAROUTPOST_SITE_KOREAN_NAME = "Minecraft Admin"
DEFAULT_NEAROUTPOST_DISCORD_INVITE_URL = "https://discord.example.com/invite"
DEFAULT_NEAROUTPOST_SERVER_LAUNCH_LABEL = "Example"
DEFAULT_NEAROUTPOST_COPYRIGHT_YEAR = "2026"
DEFAULT_PUBLISHER_NAME = "Example Operator"
DEFAULT_PUBLISHER_URL = "http://localhost:8000"
DEFAULT_OWNER_EMAIL = "owner@example.com"
DEFAULT_STAFF_EMAILS = ("staff@example.com",)
DEFAULT_STAFF_MINECRAFT_IDS = MappingProxyType(
    {
        "staff@example.com": "ExampleStaff",
    }
)
DEFAULT_LOCAL_TRUSTED_HOSTS = ("localhost", "127.0.0.1")


@dataclass(frozen=True)
class PublicSiteIdentity:
    main_base_url: str
    public_https_host_suffixes: tuple[str, ...]
    nearoutpost_base_url: str
    nearoutpost_host_markers: tuple[str, ...]
    nearoutpost_server_address: str
    support_email: str
    site_name: str
    site_short_name: str
    site_korean_name: str
    discord_invite_url: str
    server_launch_label: str
    copyright_year: str
    publisher_name: str
    publisher_url: str
    trusted_hosts: tuple[str, ...]


@dataclass(frozen=True)
class AccessBootstrapIdentity:
    owner_email: str
    staff_emails: frozenset[str]
    staff_minecraft_ids: dict[str, str]


def _clean_base_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _url_host(value: str) -> str:
    parsed = urlparse(_clean_base_url(value))
    return (parsed.netloc or parsed.path).split(":", 1)[0].lower()


def _csv_values(raw_value: str | None, default_values: tuple[str, ...]) -> tuple[str, ...]:
    values = raw_value.split(",") if raw_value is not None else default_values
    cleaned = tuple(str(value).strip() for value in values if str(value).strip())
    return cleaned or default_values


def _email_values(raw_value: str | None, default_values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(value.lower() for value in _csv_values(raw_value, default_values))


def _env_base_url(name: str, default: str) -> str:
    return _clean_base_url(os.getenv(name, default)) or default


def _env_text(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def _parse_staff_minecraft_ids(raw_value: str | None) -> dict[str, str]:
    if raw_value is None or not raw_value.strip():
        return dict(DEFAULT_STAFF_MINECRAFT_IDS)

    raw_value = raw_value.strip()
    if raw_value.startswith("{"):
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            return dict(DEFAULT_STAFF_MINECRAFT_IDS)
        if isinstance(payload, dict):
            return {
                str(email).strip().lower(): str(minecraft_name).strip()
                for email, minecraft_name in payload.items()
                if str(email).strip() and str(minecraft_name).strip()
            }
        return dict(DEFAULT_STAFF_MINECRAFT_IDS)

    parsed: dict[str, str] = {}
    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue
        separator = "=" if "=" in item else ":"
        if separator not in item:
            continue
        email, minecraft_name = item.split(separator, 1)
        email = email.strip().lower()
        minecraft_name = minecraft_name.strip()
        if email and minecraft_name:
            parsed[email] = minecraft_name
    return parsed or dict(DEFAULT_STAFF_MINECRAFT_IDS)


def _default_public_https_suffixes(main_base_url: str) -> tuple[str, ...]:
    host = _url_host(main_base_url)
    return (host,) if host else ("localhost",)


def _host_matches_trusted_hosts(host: str, trusted_hosts: tuple[str, ...]) -> bool:
    host = host.lower()
    for trusted_host in trusted_hosts:
        trusted_host = trusted_host.lower()
        if trusted_host.startswith("*.") and host.endswith(trusted_host[1:]):
            return True
        if host == trusted_host:
            return True
    return False


def _default_trusted_hosts(main_base_url: str, nearoutpost_base_url: str) -> tuple[str, ...]:
    host = _url_host(main_base_url)
    if not host:
        return (*DEFAULT_LOCAL_TRUSTED_HOSTS,)
    trusted_hosts = (host, f"*.{host}")
    nearoutpost_host = _url_host(nearoutpost_base_url)
    if nearoutpost_host and not _host_matches_trusted_hosts(nearoutpost_host, trusted_hosts):
        trusted_hosts = (*trusted_hosts, nearoutpost_host)
    return (*trusted_hosts, *DEFAULT_LOCAL_TRUSTED_HOSTS)


@lru_cache(maxsize=1)
def get_public_site_identity() -> PublicSiteIdentity:
    main_base_url = _env_base_url("CORA_MAIN_BASE_URL", DEFAULT_MAIN_BASE_URL)
    nearoutpost_base_url = _env_base_url("NEAROUTPOST_BASE_URL", DEFAULT_NEAROUTPOST_BASE_URL)
    return PublicSiteIdentity(
        main_base_url=main_base_url,
        public_https_host_suffixes=_csv_values(
            os.getenv("CORA_PUBLIC_HTTPS_HOST_SUFFIXES"),
            _default_public_https_suffixes(main_base_url),
        ),
        nearoutpost_base_url=nearoutpost_base_url,
        nearoutpost_host_markers=_csv_values(
            os.getenv("NEAROUTPOST_HOST_MARKERS"),
            DEFAULT_NEAROUTPOST_HOST_MARKERS,
        ),
        nearoutpost_server_address=os.getenv(
            "NEAROUTPOST_SERVER_ADDRESS",
            DEFAULT_NEAROUTPOST_SERVER_ADDRESS,
        ).strip()
        or DEFAULT_NEAROUTPOST_SERVER_ADDRESS,
        support_email=_env_text("NEAROUTPOST_SUPPORT_EMAIL", DEFAULT_NEAROUTPOST_SUPPORT_EMAIL),
        site_name=_env_text("NEAROUTPOST_SITE_NAME", DEFAULT_NEAROUTPOST_SITE_NAME),
        site_short_name=_env_text("NEAROUTPOST_SITE_SHORT_NAME", DEFAULT_NEAROUTPOST_SITE_SHORT_NAME),
        site_korean_name=_env_text("NEAROUTPOST_SITE_KOREAN_NAME", DEFAULT_NEAROUTPOST_SITE_KOREAN_NAME),
        discord_invite_url=_env_text("NEAROUTPOST_DISCORD_INVITE_URL", DEFAULT_NEAROUTPOST_DISCORD_INVITE_URL),
        server_launch_label=_env_text("NEAROUTPOST_SERVER_LAUNCH_LABEL", DEFAULT_NEAROUTPOST_SERVER_LAUNCH_LABEL),
        copyright_year=_env_text("NEAROUTPOST_COPYRIGHT_YEAR", DEFAULT_NEAROUTPOST_COPYRIGHT_YEAR),
        publisher_name=_env_text("CORA_PUBLISHER_NAME", DEFAULT_PUBLISHER_NAME),
        publisher_url=_env_base_url("CORA_PUBLISHER_URL", DEFAULT_PUBLISHER_URL),
        trusted_hosts=_csv_values(
            os.getenv("CORA_TRUSTED_HOSTS"),
            _default_trusted_hosts(main_base_url, nearoutpost_base_url),
        ),
    )


@lru_cache(maxsize=1)
def get_access_bootstrap_identity() -> AccessBootstrapIdentity:
    owner_email = os.getenv("OWNER_EMAIL", DEFAULT_OWNER_EMAIL).strip().lower() or DEFAULT_OWNER_EMAIL
    return AccessBootstrapIdentity(
        owner_email=owner_email,
        staff_emails=_email_values(os.getenv("STAFF_EMAILS"), DEFAULT_STAFF_EMAILS),
        staff_minecraft_ids=_parse_staff_minecraft_ids(os.getenv("STAFF_MINECRAFT_IDS")),
    )


def reset_deployment_identity_cache() -> None:
    get_public_site_identity.cache_clear()
    get_access_bootstrap_identity.cache_clear()


def public_site_template_payload() -> dict[str, str]:
    identity = get_public_site_identity()
    return {
        "main_base_url": identity.main_base_url,
        "base_url": identity.nearoutpost_base_url,
        "site_name": identity.site_name,
        "site_short_name": identity.site_short_name,
        "site_korean_name": identity.site_korean_name,
        "server_address": identity.nearoutpost_server_address,
        "support_email": identity.support_email,
        "discord_invite_url": identity.discord_invite_url,
        "server_launch_label": identity.server_launch_label,
        "copyright_year": identity.copyright_year,
        "copyright_notice": f"© {identity.copyright_year} {identity.site_name}. ALL RIGHTS RESERVED.",
        "eula_disclaimer_ko": f"{identity.site_name}는 마인크래프트 EULA를 준수합니다.",
        "eula_disclaimer_en": f"{identity.site_name} follows the Minecraft EULA.",
        "affiliation_disclaimer_ko": "Mojang Studios와 제휴되지 않았습니다.",
        "affiliation_disclaimer_en": "Not affiliated with Mojang Studios.",
    }


def nearoutpost_url(path: str = "/") -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{get_public_site_identity().nearoutpost_base_url}{path}"


def is_nearoutpost_host(host: str) -> bool:
    normalized = (host or "").split(":", 1)[0].lower()
    if not normalized:
        return False
    identity = get_public_site_identity()
    if normalized == _url_host(identity.nearoutpost_base_url):
        return True
    return any(marker.lower() in normalized for marker in identity.nearoutpost_host_markers)


def should_force_https_for_public_host(host: str) -> bool:
    normalized = (host or "").split(":", 1)[0].lower()
    if not normalized:
        return False
    for suffix in get_public_site_identity().public_https_host_suffixes:
        suffix = suffix.lower()
        if normalized == suffix or normalized.endswith(f".{suffix}"):
            return True
    return False
