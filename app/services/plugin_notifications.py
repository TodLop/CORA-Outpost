# app/services/plugin_notifications.py
"""
Plugin Notification Service

Handles in-app notifications for plugin documentation updates:
- Doc updates
- New comments
- New commands/settings added

Uses JSON file storage with thread-safe operations.
"""

import json
import logging
import threading
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from app.core.config import DATA_DIR

logger = logging.getLogger(__name__)

# File path
NOTIFICATIONS_FILE = DATA_DIR / "plugin_notifications.json"

# Thread lock for file operations
_file_lock = threading.Lock()

# Notification types
NOTIFICATION_TYPES = {
    "doc_update": "Updated documentation for {plugin_name}",
    "comment_added": "New comment on {plugin_name}",
    "command_added": "New command added to {plugin_name}",
    "setting_added": "New key setting added to {plugin_name}"
}


def _load_notifications() -> dict:
    """Load notifications from JSON file"""
    if not NOTIFICATIONS_FILE.exists():
        return {"notifications": []}
    try:
        with open(NOTIFICATIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Error loading: %s", e)
        return {"notifications": []}


def _save_notifications(data: dict) -> bool:
    """Save notifications to JSON file"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(NOTIFICATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        logger.error("Error saving: %s", e)
        return False


def create_notification(
    notification_type: str,
    plugin_id: str,
    plugin_name: str,
    actor: str,
    actor_name: str,
    message: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new notification.

    Args:
        notification_type: One of 'doc_update', 'comment_added', 'command_added', 'setting_added'
        plugin_id: The plugin identifier
        plugin_name: Display name of the plugin
        actor: Email of the user who triggered the notification
        actor_name: Display name of the user
        message: Optional custom message (defaults to type template)

    Returns:
        The created notification dict
    """
    with _file_lock:
        data = _load_notifications()
        notifications = data.setdefault("notifications", [])

        # Generate message if not provided
        if message is None:
            template = NOTIFICATION_TYPES.get(notification_type, "{plugin_name} was updated")
            message = template.format(plugin_name=plugin_name)

        notif_id = f"notif_{uuid.uuid4().hex[:8]}"
        notification = {
            "id": notif_id,
            "type": notification_type,
            "plugin_id": plugin_id,
            "plugin_name": plugin_name,
            "actor": actor,
            "actor_name": actor_name,
            "message": message,
            "timestamp": datetime.now().isoformat(),
            "read_by": []
        }

        # Add to beginning of list (newest first)
        notifications.insert(0, notification)

        # Keep only last 100 notifications
        if len(notifications) > 100:
            data["notifications"] = notifications[:100]

        _save_notifications(data)
        return notification


def get_notifications(
    user_email: str,
    limit: int = 50,
    unread_only: bool = False
) -> List[Dict[str, Any]]:
    """
    Get notifications for a user.

    Args:
        user_email: The user's email
        limit: Maximum number of notifications to return
        unread_only: If True, only return unread notifications

    Returns:
        List of notification dicts
    """
    with _file_lock:
        data = _load_notifications()

    notifications = data.get("notifications", [])

    if unread_only:
        notifications = [n for n in notifications if user_email not in n.get("read_by", [])]

    # Don't include notifications triggered by the user themselves
    # (they already know about their own actions)
    # Actually, let's keep them so they can see the history
    # notifications = [n for n in notifications if n.get("actor") != user_email]

    return notifications[:limit]


def get_unread_count(user_email: str) -> int:
    """Get count of unread notifications for a user"""
    with _file_lock:
        data = _load_notifications()

    notifications = data.get("notifications", [])
    count = 0

    for notif in notifications:
        if user_email not in notif.get("read_by", []):
            # Don't count own actions
            if notif.get("actor") != user_email:
                count += 1

    return count


def mark_as_read(user_email: str, notification_ids: Optional[List[str]] = None) -> int:
    """
    Mark notifications as read for a user.

    Args:
        user_email: The user's email
        notification_ids: List of notification IDs to mark as read.
                         If None, marks all notifications as read.

    Returns:
        Number of notifications marked as read
    """
    with _file_lock:
        data = _load_notifications()
        notifications = data.get("notifications", [])
        count = 0

        for notif in notifications:
            # Skip if already read by this user
            if user_email in notif.get("read_by", []):
                continue

            # If specific IDs provided, only mark those
            if notification_ids is not None and notif.get("id") not in notification_ids:
                continue

            notif.setdefault("read_by", []).append(user_email)
            count += 1

        if count > 0:
            _save_notifications(data)

        return count


def mark_plugin_notifications_read(user_email: str, plugin_id: str) -> int:
    """
    Mark all notifications for a specific plugin as read.

    This is useful when a user visits a plugin detail page.

    Args:
        user_email: The user's email
        plugin_id: The plugin identifier

    Returns:
        Number of notifications marked as read
    """
    with _file_lock:
        data = _load_notifications()
        notifications = data.get("notifications", [])
        count = 0

        for notif in notifications:
            if notif.get("plugin_id") != plugin_id:
                continue

            if user_email in notif.get("read_by", []):
                continue

            notif.setdefault("read_by", []).append(user_email)
            count += 1

        if count > 0:
            _save_notifications(data)

        return count


def clear_old_notifications(days: int = 30) -> int:
    """
    Clear notifications older than specified days.

    Args:
        days: Number of days to keep notifications

    Returns:
        Number of notifications removed
    """
    from datetime import timedelta

    cutoff = datetime.now() - timedelta(days=days)

    with _file_lock:
        data = _load_notifications()
        notifications = data.get("notifications", [])
        original_count = len(notifications)

        data["notifications"] = [
            n for n in notifications
            if datetime.fromisoformat(n.get("timestamp", "2000-01-01")) > cutoff
        ]

        removed = original_count - len(data["notifications"])

        if removed > 0:
            _save_notifications(data)

        return removed
