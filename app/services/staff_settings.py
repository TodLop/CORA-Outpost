# app/services/staff_settings.py
"""
Staff Settings Service

Manages per-staff feature visibility settings.
Admins can configure which features are visible to specific staff members.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict, field
import threading

from app.core.config import DATA_DIR

logger = logging.getLogger(__name__)

# Staff settings file path
STAFF_SETTINGS_FILE = DATA_DIR / "staff_settings.json"

# Thread lock for file operations
_file_lock = threading.Lock()

# Available features that can be toggled per staff
TOGGLEABLE_FEATURES = frozenset([
    "plugin_installation",  # Plugin docs & installation info
    "server_restart",       # Server restart capability
    "whitelist_remove",     # Remove players from whitelist (=permanent ban)
])


@dataclass
class StaffFeatureSettings:
    """Represents feature settings for a single staff member"""
    email: str
    hidden_features: List[str] = field(default_factory=list)  # Features to hide
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


def _load_settings() -> dict:
    """Load staff settings from JSON file"""
    if not STAFF_SETTINGS_FILE.exists():
        return {"staff": {}}

    try:
        with open(STAFF_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Error loading settings file: %s", e)
        return {"staff": {}}


def _save_settings(data: dict) -> bool:
    """Save staff settings to JSON file"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(STAFF_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        logger.error("Error saving settings file: %s", e)
        return False


def get_staff_settings(staff_email: str) -> StaffFeatureSettings:
    """
    Get feature settings for a specific staff member.

    Args:
        staff_email: Staff member's email address

    Returns:
        StaffFeatureSettings object (returns defaults if not configured)
    """
    with _file_lock:
        data = _load_settings()

    staff_data = data.get("staff", {}).get(staff_email.lower())
    if staff_data:
        return StaffFeatureSettings(**staff_data)

    # Return default (no features hidden)
    return StaffFeatureSettings(email=staff_email.lower())


def get_all_staff_settings() -> List[StaffFeatureSettings]:
    """
    Get settings for all configured staff members.

    Returns:
        List of StaffFeatureSettings objects
    """
    with _file_lock:
        data = _load_settings()

    settings = []
    for email, staff_data in data.get("staff", {}).items():
        settings.append(StaffFeatureSettings(**staff_data))

    return settings


def update_staff_settings(
    staff_email: str,
    hidden_features: List[str],
    admin_email: str
) -> Optional[StaffFeatureSettings]:
    """
    Update feature visibility settings for a staff member.

    Args:
        staff_email: Staff member's email address
        hidden_features: List of feature IDs to hide
        admin_email: Admin making the change

    Returns:
        Updated StaffFeatureSettings if successful, None otherwise
    """
    # Validate features
    clean_features = [f for f in hidden_features if f in TOGGLEABLE_FEATURES]

    with _file_lock:
        data = _load_settings()

        if "staff" not in data:
            data["staff"] = {}

        data["staff"][staff_email.lower()] = {
            "email": staff_email.lower(),
            "hidden_features": clean_features,
            "updated_at": datetime.now().isoformat(),
            "updated_by": admin_email
        }

        if _save_settings(data):
            return StaffFeatureSettings(**data["staff"][staff_email.lower()])

    return None


def toggle_feature_for_staff(
    staff_email: str,
    feature: str,
    visible: bool,
    admin_email: str
) -> Optional[StaffFeatureSettings]:
    """
    Toggle a single feature visibility for a staff member.

    Args:
        staff_email: Staff member's email address
        feature: Feature ID to toggle
        visible: True to show, False to hide
        admin_email: Admin making the change

    Returns:
        Updated StaffFeatureSettings if successful, None otherwise
    """
    if feature not in TOGGLEABLE_FEATURES:
        return None

    current = get_staff_settings(staff_email)
    hidden = set(current.hidden_features)

    if visible:
        hidden.discard(feature)
    else:
        hidden.add(feature)

    return update_staff_settings(staff_email, list(hidden), admin_email)


def is_feature_visible(staff_email: str, feature: str) -> bool:
    """
    Check if a feature is visible for a staff member.

    Args:
        staff_email: Staff member's email address
        feature: Feature ID to check

    Returns:
        True if feature is visible (not in hidden list), False otherwise
    """
    settings = get_staff_settings(staff_email)
    return feature not in settings.hidden_features


def delete_staff_settings(staff_email: str) -> bool:
    """
    Remove custom settings for a staff member (revert to defaults).

    Args:
        staff_email: Staff member's email address

    Returns:
        True if deleted, False if not found
    """
    with _file_lock:
        data = _load_settings()

        if staff_email.lower() in data.get("staff", {}):
            del data["staff"][staff_email.lower()]
            return _save_settings(data)

    return False


def get_available_features() -> Dict[str, str]:
    """
    Get all available toggleable features with descriptions.

    Returns:
        Dict mapping feature ID to description
    """
    return {
        "plugin_installation": "Plugin Documentation & Installation Info",
        "server_restart": "Server Restart Capability",
        "whitelist_remove": "Remove Players from Whitelist (Permanent Ban)",
    }
