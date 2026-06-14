#!/usr/bin/env python3
"""Fail-fast hygiene checks for the public Minecraft admin extraction."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BLOCKED_PATH_PARTS = {
    ".git",
    ".omx",
    ".claude",
    ".sisyphus",
    ".ruff_cache",
    ".pytest_cache",
    ".playwright-cli",
    "__pycache__",
    "config_files",
    "data",
    "logs",
    "dist",
    "backups",
    "custom_plugins",
    "apps",
}
BLOCKED_FILE_NAMES = {
    ".env",
    "credentials.json",
    "client_secret_web.json",
    "token.json",
    "server.log",
}
BLOCKED_PATH_PATTERNS = (
    re.compile(r"(^|/)080-incident-"),
    re.compile(r"(^|/)terminal(\.py|/|$)"),
    re.compile(r"(^|/)minecraft_market(/|$)"),
    re.compile(r"(^|/)player_stats(/|$)"),
    re.compile(r"(^|/)economy(/|$)"),
    re.compile(r"(^|/)portfolio_private(/|$)"),
)
BLOCKED_TEXT_PATTERNS = {
    "example.invalid": "live domain",
    "example-host": "live host marker",
    "nearoutpost.example.invalid": "live Minecraft host",
    "owner@example.com": "live owner email",
    "staff@example.com": "live staff email",
    "ExampleStaff": "live staff Minecraft ID",
    "ExamplePlayer": "live Minecraft ID",
    "ExamplePlayerA": "live-looking player fixture",
    "ExamplePlayerB": "live-looking player fixture",
    "ExamplePlayerC": "live-looking player fixture",
    ".example_user": "live proxy Minecraft ID",
    "/path/to/user": "local absolute path",
    "discord.example.com/invite": "live Discord invite",
}
BLOCKED_CODE_PATTERNS = {
    "from app.routers import finance": "finance router import",
    "admin_economy": "economy admin router",
    "admin_donations": "donation admin router",
    "economy:forfeit": "excluded economy permission",
    "donations:manage": "excluded donation permission",
    "economy-forfeiture": "excluded economy route",
    "economy/forfeiture": "excluded economy route",
    "canEconomyForfeit": "excluded economy UI state",
    "bulkForfeit": "excluded economy UI state",
    "forfeitInactiveAssets": "excluded economy action",
    "ServerAccount": "excluded economy plugin operation",
    "/sa forfeit": "excluded economy command",
    "minecraft_market_bridge": "excluded market capability",
    "portfolio_private": "portfolio module",
    "minecraft_wrapped": "wrapped module",
    "nearoutpost_public": "public site module",
    "player_stats": "player records module",
    "terminal_manager": "terminal service usage",
    "pty.openpty": "PTY shell execution",
    "secrets.token_urlsafe(16)": "RCON password generation",
}
TEXT_SUFFIXES = {
    ".py",
    ".html",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".js",
    ".css",
    ".example",
}


def is_text(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in {"requirements.txt", ".env.example", ".gitignore"}


def iter_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if listed.returncode != 0:
        print(listed.stderr, file=sys.stderr)
        return []

    files: list[Path] = []
    for rel in listed.stdout.splitlines():
        path = ROOT / rel
        if path.is_file():
            files.append(path)
    return files


def should_enforce_local_only_remote_policy() -> bool:
    """GitHub Actions checkouts always have an origin remote."""
    return os.getenv("GITHUB_ACTIONS", "").strip().lower() != "true"


def main() -> int:
    errors: list[str] = []
    files = iter_files()

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        parts = set(path.relative_to(ROOT).parts)
        if parts & BLOCKED_PATH_PARTS:
            errors.append(f"blocked path included: {rel}")
        if path.name in BLOCKED_FILE_NAMES:
            errors.append(f"blocked file included: {rel}")
        for pattern in BLOCKED_PATH_PATTERNS:
            if pattern.search(rel):
                errors.append(f"blocked path pattern included: {rel}")

        if not is_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")

        if rel in {"scripts/check_public_extract.py", "tests/test_public_extract_scope.py"}:
            continue
        for needle, label in BLOCKED_TEXT_PATTERNS.items():
            if needle in text:
                errors.append(f"{label} found in {rel}: {needle}")
        for needle, label in BLOCKED_CODE_PATTERNS.items():
            if needle in text:
                errors.append(f"{label} found in {rel}: {needle}")

    env_example = ROOT / ".env.example"
    if not env_example.exists():
        errors.append(".env.example is missing")
    else:
        env_text = env_example.read_text(encoding="utf-8")
        for required in (
            "SECRET_KEY=replace-with-a-long-random-secret",
            "ENABLED_MODULES=minecraft_admin",
            "OWNER_EMAIL=owner@example.com",
        ):
            if required not in env_text:
                errors.append(f".env.example missing safe placeholder: {required}")

    remote_output = subprocess.run(
        ["git", "remote", "-v"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if should_enforce_local_only_remote_policy() and remote_output.stdout.strip():
        errors.append("git remotes are configured; public extraction must start local-only")

    if errors:
        print("Public extract check failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"Public extract check passed. Checked {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
