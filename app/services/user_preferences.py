"""
User Preferences Service

Stores per-user UI convenience preferences in a dedicated JSON file.
Applies to both staff and admin panel users.
"""

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

from app.core.config import DATA_DIR

logger = logging.getLogger(__name__)

PREFERENCES_FILE = DATA_DIR / "user_preferences.json"
_file_lock = threading.Lock()

DEFAULT_PREFERENCES: Dict[str, Any] = {
    "language": "ko",
    "theme": "dark",
    "font_scale": "md",
    "high_contrast": False,
    "reduced_motion": False,
    "toast_duration_ms": 4000,
    "server_name": "MINECRAFT SERVER",
    "server_name_color": "#4ade80",
}

_LANG_VALUES = frozenset({"ko", "en"})
_THEME_VALUES = frozenset({"dark", "light", "system"})
_FONT_SCALE_VALUES = frozenset({"sm", "md", "lg"})
_TOAST_MIN_MS = 1500
_TOAST_MAX_MS = 10000
_SERVER_NAME_MAX_LENGTH = 48
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class PreferenceValidationError(Exception):
    """Raised when preference payload validation fails."""

    def __init__(self, errors: Dict[str, str]):
        super().__init__("Invalid preference payload")
        self.errors = errors


def _base_payload() -> dict:
    return {
        "version": 1,
        "defaults": dict(DEFAULT_PREFERENCES),
        "users": {},
    }


def _load_payload() -> dict:
    if not PREFERENCES_FILE.exists():
        return _base_payload()
    try:
        with open(PREFERENCES_FILE, "r", encoding="utf-8") as fp:
            loaded = json.load(fp)
            if not isinstance(loaded, dict):
                return _base_payload()
            loaded.setdefault("version", 1)
            loaded.setdefault("defaults", dict(DEFAULT_PREFERENCES))
            loaded.setdefault("users", {})
            if not isinstance(loaded["defaults"], dict):
                loaded["defaults"] = dict(DEFAULT_PREFERENCES)
            if not isinstance(loaded["users"], dict):
                loaded["users"] = {}
            return loaded
    except (json.JSONDecodeError, IOError) as exc:
        logger.error("Failed to load user preferences: %s", exc)
        return _base_payload()


def _save_payload(payload: dict) -> bool:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(PREFERENCES_FILE, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, ensure_ascii=False)
        return True
    except IOError as exc:
        logger.error("Failed to save user preferences: %s", exc)
        return False


def _validate_partial_preferences(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    clean: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for key, value in raw.items():
        if key not in DEFAULT_PREFERENCES:
            errors[key] = "Unknown preference key"
            continue

        if key == "language":
            if not isinstance(value, str) or value not in _LANG_VALUES:
                errors[key] = f"Must be one of: {', '.join(sorted(_LANG_VALUES))}"
            else:
                clean[key] = value
            continue

        if key == "theme":
            if not isinstance(value, str) or value not in _THEME_VALUES:
                errors[key] = f"Must be one of: {', '.join(sorted(_THEME_VALUES))}"
            else:
                clean[key] = value
            continue

        if key == "font_scale":
            if not isinstance(value, str) or value not in _FONT_SCALE_VALUES:
                errors[key] = f"Must be one of: {', '.join(sorted(_FONT_SCALE_VALUES))}"
            else:
                clean[key] = value
            continue

        if key in {"high_contrast", "reduced_motion"}:
            if not isinstance(value, bool):
                errors[key] = "Must be boolean"
            else:
                clean[key] = value
            continue

        if key == "toast_duration_ms":
            if (
                not isinstance(value, int)
                or value < _TOAST_MIN_MS
                or value > _TOAST_MAX_MS
            ):
                errors[key] = f"Must be integer between {_TOAST_MIN_MS} and {_TOAST_MAX_MS}"
            else:
                clean[key] = value
            continue

        if key == "server_name":
            if not isinstance(value, str):
                errors[key] = "Must be text"
            else:
                stripped_value = value.strip()
                if not stripped_value:
                    errors[key] = "Must not be empty"
                elif len(stripped_value) > _SERVER_NAME_MAX_LENGTH:
                    errors[key] = f"Must be {_SERVER_NAME_MAX_LENGTH} characters or fewer"
                else:
                    clean[key] = stripped_value
            continue

        if key == "server_name_color":
            if not isinstance(value, str) or not _HEX_COLOR_RE.fullmatch(value):
                errors[key] = "Must be a #RRGGBB color"
            else:
                clean[key] = value.lower()
            continue

    return clean, errors


def _extract_defaults(payload: dict) -> Dict[str, Any]:
    raw_defaults = payload.get("defaults", {})
    if not isinstance(raw_defaults, dict):
        return dict(DEFAULT_PREFERENCES)

    clean_defaults, _ = _validate_partial_preferences(raw_defaults)
    merged = dict(DEFAULT_PREFERENCES)
    merged.update(clean_defaults)
    return merged


def get_defaults() -> Dict[str, Any]:
    with _file_lock:
        payload = _load_payload()
        return _extract_defaults(payload)


def get_preferences(email: str) -> Dict[str, Any]:
    normalized_email = (email or "").strip().lower()
    with _file_lock:
        payload = _load_payload()
        defaults = _extract_defaults(payload)
        users = payload.get("users", {})
        user_row = users.get(normalized_email, {}) if isinstance(users, dict) else {}

        user_values = {
            key: user_row[key]
            for key in DEFAULT_PREFERENCES
            if isinstance(user_row, dict) and key in user_row
        }
        clean_user_values, _ = _validate_partial_preferences(user_values)

        merged = dict(defaults)
        merged.update(clean_user_values)
        return merged


def set_preferences(email: str, patch: Dict[str, Any], updated_by: str = "self") -> Dict[str, Any]:
    if not isinstance(patch, dict):
        raise PreferenceValidationError({"preferences": "Payload must be an object"})

    clean_patch, errors = _validate_partial_preferences(patch)
    if errors:
        raise PreferenceValidationError(errors)

    normalized_email = (email or "").strip().lower()
    with _file_lock:
        payload = _load_payload()
        payload.setdefault("version", 1)
        payload.setdefault("defaults", dict(DEFAULT_PREFERENCES))
        payload.setdefault("users", {})

        defaults = _extract_defaults(payload)
        current = dict(defaults)

        existing = payload["users"].get(normalized_email, {})
        if isinstance(existing, dict):
            existing_values = {
                key: existing[key]
                for key in DEFAULT_PREFERENCES
                if key in existing
            }
            clean_existing, _ = _validate_partial_preferences(existing_values)
            current.update(clean_existing)

        current.update(clean_patch)

        payload["users"][normalized_email] = {
            **{key: current[key] for key in DEFAULT_PREFERENCES},
            "updated_at": datetime.now().isoformat(),
            "updated_by": updated_by or "self",
        }

        if not _save_payload(payload):
            raise IOError("Failed to persist preferences")

        return {key: current[key] for key in DEFAULT_PREFERENCES}
