#!/usr/bin/env python3
"""Fail-fast hygiene checks for the public Minecraft admin extraction."""

from __future__ import annotations

import os
import hashlib
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
GENERIC_SENSITIVE_TEXT_PATTERNS = {
    re.compile(r"/Users/[A-Za-z0-9._-]+"): "local absolute path",
}
HASHED_BLOCKED_TEXT_PATTERNS = {
    "b1bcbb4c971cb44715380c0b487a2d7b20ae97aaaaf923729426ce87a810e637": "live domain",
    "9e2db14f5c4e90c99776726c301f46ab24c68534c9529e3f63b4f60367a4cccf": "live host marker",
    "a3edcb290dc0388567978681ee683ebd443ae0422b7dc75bc178a6603131f951": "live Minecraft host",
    "66230b24a42106bb693362f34b0dc406e60c3de76aa9f043048ea09569cc68cc": "live owner email",
    "ce94ac67cd5b6875e9b0903b9b295caaa53e75fc68504e40fdc2effa35012ef2": "live staff email",
    "fc378a192f44bbfb1205c9266f882c437f3317f68fcb41b1f29350acc901eb1f": "live staff Minecraft ID",
    "21c92434c590abc936ecc14436943f375ba04ec660941922d35da148483a90cf": "live Minecraft ID",
    "e2b7e11982e107fa6af75c450053842b0e78f86793a806df533db1925651a594": "live-looking player fixture",
    "0255fcd2a877dc813b27f7eb3eaa57157831acb550828ada2a92b520fb883f32": "live-looking player fixture",
    "7f2959db8a1693aebfde9dcc8e43094bf6488d46dc433d560850e5c492f545e6": "live-looking player fixture",
    "3f2283c8d5866960e8e5fffbb72bf007b2635a9059f14e9bd8e6fc8f6617f123": "live proxy Minecraft ID",
    "2964ff775e37816b807d1e036a3146901efc58fbb98e01c249ea2e578e7ea7e7": "local absolute path",
    "4849248b6ccb64682c98af1cf460e0d0208afdd6aba28d109db69b179b963459": "live Discord invite",
}
HASH_CANDIDATE_PATTERNS = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?:https?://)?discord\.gg/[A-Za-z0-9_-]+"),
    re.compile(r"/Users/[A-Za-z0-9._/-]+"),
    re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?"),
    re.compile(r"(?<![A-Za-z0-9_])[.]?[A-Za-z0-9_][A-Za-z0-9_.-]{2,63}(?![A-Za-z0-9_])"),
)
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


def hashed_text_candidates(text: str) -> set[str]:
    candidates: set[str] = set()
    for pattern in HASH_CANDIDATE_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0).strip(".,;:()[]{}<>\"'")
            if not value:
                continue
            candidates.add(value)
            if value.startswith(("http://", "https://")):
                candidates.add(value.split("://", 1)[1])
    return candidates


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
        for pattern, label in GENERIC_SENSITIVE_TEXT_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{label} found in {rel}")
        for candidate in hashed_text_candidates(text):
            digest = hashlib.sha256(candidate.encode()).hexdigest()
            if digest in HASHED_BLOCKED_TEXT_PATTERNS:
                label = HASHED_BLOCKED_TEXT_PATTERNS[digest]
                errors.append(f"{label} found in {rel}")
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
