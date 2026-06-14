# app/services/plugin_docs.py
"""
Plugin Documentation Service

Handles CRUD operations for plugin documentation, including:
- Plugin summaries and descriptions
- Commands documentation
- Key settings highlights
- Comments/discussion threads

Uses JSON file storage with thread-safe operations.
"""

import json
import threading
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

from app.core.config import DATA_DIR
from app.services import minecraft_settings

# File paths
PLUGIN_DOCS_FILE = DATA_DIR / "plugin_docs.json"
PLUGINS_PATH = minecraft_settings.get_plugins_dir()


def _plugins_path() -> Path:
    return minecraft_settings.get_plugins_dir()

# Thread lock for file operations
_file_lock = threading.Lock()

# Plugin ID to folder name mapping (lowercase plugin_id -> actual folder name)
PLUGIN_FOLDER_MAP = {
    "ajleaderboards": "ajLeaderboards",
    "clearlag": "ClearLag",
    "coreprotect": "CoreProtect",
    "discordsrv": "DiscordSRV",
    "essentialsx": "Essentials",
    "floodgate": "floodgate",
    "geyser": "Geyser-Spigot",
    "grimac": "GrimAC",
    "invsee": "InvSeePlusPlus",
    "lands": "Lands",
    "luckperms": "LuckPerms",
    "minertrack": "MinerTrack",
    "ndailyrewards": "NDailyRewards",
    "orebfuscator": "Orebfuscator",
    "placeholderapi": "PlaceholderAPI",
    "playerauctions": "PlayerAuctions",
    "protocollib": "ProtocolLib",
    "servershop": "ServerShop",
    "tab": "TAB",
    "tcpshield": "TCPShield",
    "ultracosmetics": "UltraCosmetics",
    "vault": "Vault",
    "viaversion": "ViaVersion",
    "worldedit": "WorldEdit",
    "worldguard": "WorldGuard",
}

# Max config file size (1MB)
MAX_CONFIG_SIZE = 1024 * 1024


def _load_docs() -> dict:
    """Load plugin docs from JSON file"""
    if not PLUGIN_DOCS_FILE.exists():
        return {"plugins": {}}
    try:
        with open(PLUGIN_DOCS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[PluginDocs] Error loading: {e}")
        return {"plugins": {}}


def _save_docs(data: dict) -> bool:
    """Save plugin docs to JSON file"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(PLUGIN_DOCS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        print(f"[PluginDocs] Error saving: {e}")
        return False


def get_all_plugins() -> Dict[str, Any]:
    """Get all plugin documentation"""
    with _file_lock:
        data = _load_docs()
    return data.get("plugins", {})


def get_plugin(plugin_id: str) -> Optional[Dict[str, Any]]:
    """Get documentation for a specific plugin"""
    with _file_lock:
        data = _load_docs()
    return data.get("plugins", {}).get(plugin_id)


def update_plugin_doc(
    plugin_id: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    updated_by: str = "",
    updated_by_name: str = ""
) -> Dict[str, Any]:
    """Update plugin summary and/or description (Admin only)"""
    with _file_lock:
        data = _load_docs()
        plugins = data.setdefault("plugins", {})

        if plugin_id not in plugins:
            plugins[plugin_id] = {
                "summary": "",
                "description": "",
                "commands": [],
                "key_settings": [],
                "comments": [],
                "updated_by": "",
                "updated_at": ""
            }

        plugin = plugins[plugin_id]

        if summary is not None:
            plugin["summary"] = summary
        if description is not None:
            plugin["description"] = description

        plugin["updated_by"] = updated_by
        plugin["updated_by_name"] = updated_by_name
        plugin["updated_at"] = datetime.now().isoformat()

        _save_docs(data)
        return plugin


# ==================== Commands ====================

def add_command(
    plugin_id: str,
    command: str,
    description: str,
    permission: str = "",
    usage: str = "",
    added_by: str = ""
) -> Dict[str, Any]:
    """Add a command to plugin documentation"""
    with _file_lock:
        data = _load_docs()
        plugins = data.setdefault("plugins", {})

        if plugin_id not in plugins:
            plugins[plugin_id] = {
                "summary": "",
                "description": "",
                "commands": [],
                "key_settings": [],
                "comments": [],
                "updated_by": "",
                "updated_at": ""
            }

        cmd_id = f"cmd_{uuid.uuid4().hex[:8]}"
        cmd = {
            "id": cmd_id,
            "command": command,
            "description": description,
            "permission": permission,
            "usage": usage,
            "added_by": added_by,
            "added_at": datetime.now().isoformat()
        }

        plugins[plugin_id].setdefault("commands", []).append(cmd)
        plugins[plugin_id]["updated_at"] = datetime.now().isoformat()

        _save_docs(data)
        return cmd


def update_command(
    plugin_id: str,
    command_id: str,
    command: Optional[str] = None,
    description: Optional[str] = None,
    permission: Optional[str] = None,
    usage: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update an existing command"""
    with _file_lock:
        data = _load_docs()
        plugins = data.get("plugins", {})

        if plugin_id not in plugins:
            return None

        commands = plugins[plugin_id].get("commands", [])
        for cmd in commands:
            if cmd.get("id") == command_id:
                if command is not None:
                    cmd["command"] = command
                if description is not None:
                    cmd["description"] = description
                if permission is not None:
                    cmd["permission"] = permission
                if usage is not None:
                    cmd["usage"] = usage
                cmd["updated_at"] = datetime.now().isoformat()

                plugins[plugin_id]["updated_at"] = datetime.now().isoformat()
                _save_docs(data)
                return cmd

        return None


def delete_command(plugin_id: str, command_id: str) -> bool:
    """Delete a command from plugin documentation"""
    with _file_lock:
        data = _load_docs()
        plugins = data.get("plugins", {})

        if plugin_id not in plugins:
            return False

        commands = plugins[plugin_id].get("commands", [])
        original_len = len(commands)
        plugins[plugin_id]["commands"] = [c for c in commands if c.get("id") != command_id]

        if len(plugins[plugin_id]["commands"]) < original_len:
            plugins[plugin_id]["updated_at"] = datetime.now().isoformat()
            _save_docs(data)
            return True

        return False


# ==================== Key Settings ====================

def add_key_setting(
    plugin_id: str,
    path: str,
    description: str,
    current_value: str = "",
    added_by: str = ""
) -> Dict[str, Any]:
    """Add a key setting highlight"""
    with _file_lock:
        data = _load_docs()
        plugins = data.setdefault("plugins", {})

        if plugin_id not in plugins:
            plugins[plugin_id] = {
                "summary": "",
                "description": "",
                "commands": [],
                "key_settings": [],
                "comments": [],
                "updated_by": "",
                "updated_at": ""
            }

        setting_id = f"set_{uuid.uuid4().hex[:8]}"
        setting = {
            "id": setting_id,
            "path": path,
            "description": description,
            "current_value": current_value,
            "added_by": added_by,
            "added_at": datetime.now().isoformat()
        }

        plugins[plugin_id].setdefault("key_settings", []).append(setting)
        plugins[plugin_id]["updated_at"] = datetime.now().isoformat()

        _save_docs(data)
        return setting


def delete_key_setting(plugin_id: str, setting_id: str) -> bool:
    """Delete a key setting"""
    with _file_lock:
        data = _load_docs()
        plugins = data.get("plugins", {})

        if plugin_id not in plugins:
            return False

        settings = plugins[plugin_id].get("key_settings", [])
        original_len = len(settings)
        plugins[plugin_id]["key_settings"] = [s for s in settings if s.get("id") != setting_id]

        if len(plugins[plugin_id]["key_settings"]) < original_len:
            plugins[plugin_id]["updated_at"] = datetime.now().isoformat()
            _save_docs(data)
            return True

        return False


# ==================== Comments ====================

def add_comment(
    plugin_id: str,
    author: str,
    author_name: str,
    text: str
) -> Dict[str, Any]:
    """Add a comment to plugin documentation"""
    with _file_lock:
        data = _load_docs()
        plugins = data.setdefault("plugins", {})

        if plugin_id not in plugins:
            plugins[plugin_id] = {
                "summary": "",
                "description": "",
                "commands": [],
                "key_settings": [],
                "comments": [],
                "updated_by": "",
                "updated_at": ""
            }

        comment_id = f"cmt_{uuid.uuid4().hex[:8]}"
        comment = {
            "id": comment_id,
            "author": author,
            "author_name": author_name,
            "text": text,
            "timestamp": datetime.now().isoformat()
        }

        plugins[plugin_id].setdefault("comments", []).append(comment)

        _save_docs(data)
        return comment


def delete_comment(plugin_id: str, comment_id: str, user_email: str, is_admin: bool) -> bool:
    """Delete a comment (admin can delete any, staff can only delete own)"""
    with _file_lock:
        data = _load_docs()
        plugins = data.get("plugins", {})

        if plugin_id not in plugins:
            return False

        comments = plugins[plugin_id].get("comments", [])
        original_len = len(comments)

        # Filter: admin can delete any, others can only delete their own
        plugins[plugin_id]["comments"] = [
            c for c in comments
            if c.get("id") != comment_id or (not is_admin and c.get("author") != user_email)
        ]

        if len(plugins[plugin_id]["comments"]) < original_len:
            _save_docs(data)
            return True

        return False


# ==================== Config File Reading ====================

def get_plugin_folder(plugin_id: str) -> Optional[Path]:
    """Get the plugin folder path for a plugin ID"""
    folder_name = PLUGIN_FOLDER_MAP.get(plugin_id.lower())
    if not folder_name:
        # Try to find by case-insensitive match
        for pid, folder in PLUGIN_FOLDER_MAP.items():
            if pid.lower() == plugin_id.lower():
                folder_name = folder
                break

    if not folder_name:
        return None

    folder_path = _plugins_path() / folder_name
    if folder_path.exists() and folder_path.is_dir():
        return folder_path

    return None


def list_config_files(plugin_id: str) -> List[Dict[str, Any]]:
    """List available config files for a plugin"""
    folder = get_plugin_folder(plugin_id)
    if not folder:
        return []

    config_files = []

    # Common config file patterns
    patterns = ["*.yml", "*.yaml", "*.json", "*.properties"]

    for pattern in patterns:
        for file_path in folder.glob(pattern):
            # Skip very large files
            if file_path.stat().st_size > MAX_CONFIG_SIZE:
                continue

            config_files.append({
                "name": file_path.name,
                "size": file_path.stat().st_size,
                "modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
            })

    # Sort by name
    config_files.sort(key=lambda x: x["name"])
    return config_files


def read_config_file(plugin_id: str, filename: str = "config.yml") -> Optional[Dict[str, Any]]:
    """
    Read a config file for a plugin with path traversal prevention.

    Returns dict with:
    - content: The file content as string
    - filename: The actual filename
    - size: File size in bytes
    - modified: Last modified timestamp

    Returns None if file not found or access denied.
    """
    folder = get_plugin_folder(plugin_id)
    if not folder:
        return None

    # Security: Prevent path traversal
    # Normalize the filename and ensure it doesn't contain path separators
    safe_filename = Path(filename).name
    if safe_filename != filename:
        return None  # Attempted path traversal

    file_path = folder / safe_filename

    # Double-check the resolved path is still within the plugin folder
    try:
        resolved = file_path.resolve()
        if not str(resolved).startswith(str(folder.resolve())):
            return None  # Path traversal detected
    except (ValueError, OSError):
        return None

    if not file_path.exists() or not file_path.is_file():
        return None

    # Check file size
    file_size = file_path.stat().st_size
    if file_size > MAX_CONFIG_SIZE:
        return {
            "error": "File too large",
            "filename": safe_filename,
            "size": file_size,
            "max_size": MAX_CONFIG_SIZE
        }

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        return {
            "content": content,
            "filename": safe_filename,
            "size": file_size,
            "modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
        }
    except Exception as e:
        return {
            "error": str(e),
            "filename": safe_filename
        }


# ==================== Initialization ====================

def initialize_plugin_docs():
    """Initialize plugin docs with basic info for all tracked plugins"""
    from app.services.minecraft_updater import load_versions

    versions_data = load_versions()
    plugins = versions_data.get("plugins", {})

    # Plugin descriptions for initial population
    plugin_descriptions = {
        "paper": {
            "summary": "Paper Server - High performance Minecraft server",
            "description": "Paper is a high performance fork of Spigot that aims to fix gameplay and mechanics inconsistencies as well as to improve performance."
        },
        "grimac": {
            "summary": "GrimAC - Anti-cheat system",
            "description": "Grim is a free and open-source anti-cheat that uses predictions to detect cheaters. It handles all movement, vehicles, and flying-related cheats."
        },
        "viaversion": {
            "summary": "ViaVersion - Multi-version support",
            "description": "Allows clients of different Minecraft versions to connect to your server. Essential for cross-version compatibility."
        },
        "geyser": {
            "summary": "Geyser - Bedrock to Java bridge",
            "description": "Allows Minecraft Bedrock Edition players to join Java Edition servers. Works alongside Floodgate for seamless authentication."
        },
        "floodgate": {
            "summary": "Floodgate - Bedrock authentication",
            "description": "Allows Bedrock players to join without a Java Edition account. Pairs with Geyser for complete Bedrock support."
        },
        "luckperms": {
            "summary": "LuckPerms - Permission management",
            "description": "A permissions plugin for Minecraft servers. Allows you to control what features players can use by creating groups and assigning permissions.",
            "commands": [
                {"command": "/lp", "description": "Main LuckPerms command", "permission": "luckperms.user", "usage": "/lp <user|group> <name> <action>"},
                {"command": "/lp user", "description": "Manage user permissions", "permission": "luckperms.user.*", "usage": "/lp user <player> permission set <permission> true"},
                {"command": "/lp group", "description": "Manage group permissions", "permission": "luckperms.group.*", "usage": "/lp group <group> permission set <permission> true"},
                {"command": "/lp editor", "description": "Open web editor", "permission": "luckperms.editor", "usage": "/lp editor"}
            ]
        },
        "essentialsx": {
            "summary": "EssentialsX - Essential commands & economy",
            "description": "Provides essential commands like /spawn, /home, /warp, economy features, teleportation, kits, and much more.",
            "commands": [
                {"command": "/spawn", "description": "Teleport to spawn", "permission": "essentials.spawn", "usage": "/spawn"},
                {"command": "/home", "description": "Teleport to your home", "permission": "essentials.home", "usage": "/home [name]"},
                {"command": "/sethome", "description": "Set a home location", "permission": "essentials.sethome", "usage": "/sethome [name]"},
                {"command": "/warp", "description": "Teleport to a warp point", "permission": "essentials.warp", "usage": "/warp <name>"},
                {"command": "/tpa", "description": "Request to teleport to a player", "permission": "essentials.tpa", "usage": "/tpa <player>"},
                {"command": "/bal", "description": "Check your balance", "permission": "essentials.balance", "usage": "/bal [player]"},
                {"command": "/pay", "description": "Pay another player", "permission": "essentials.pay", "usage": "/pay <player> <amount>"}
            ]
        },
        "coreprotect": {
            "summary": "CoreProtect - Block logging & rollback",
            "description": "Fast, efficient block logging and anti-griefing tool. Logs block changes, container transactions, and chat.",
            "commands": [
                {"command": "/co inspect", "description": "Toggle inspector mode", "permission": "coreprotect.inspect", "usage": "/co i"},
                {"command": "/co lookup", "description": "Lookup block changes", "permission": "coreprotect.lookup", "usage": "/co l u:<user> t:<time> r:<radius>"},
                {"command": "/co rollback", "description": "Rollback changes", "permission": "coreprotect.rollback", "usage": "/co rb u:<user> t:<time> r:<radius>"},
                {"command": "/co restore", "description": "Restore rolled back changes", "permission": "coreprotect.restore", "usage": "/co rs u:<user> t:<time> r:<radius>"}
            ]
        },
        "discordsrv": {
            "summary": "DiscordSRV - Discord integration",
            "description": "Links your Minecraft server with Discord. Chat sync, role sync, and more."
        },
        "invsee": {
            "summary": "InvSee++ - Inventory viewer",
            "description": "View and edit other players' inventories and ender chests.",
            "commands": [
                {"command": "/invsee", "description": "View a player's inventory", "permission": "invseeplusplus.invsee", "usage": "/invsee <player>"},
                {"command": "/endersee", "description": "View a player's ender chest", "permission": "invseeplusplus.endersee", "usage": "/endersee <player>"}
            ]
        },
        "tcpshield": {
            "summary": "TCPShield - DDoS protection",
            "description": "Provides DDoS protection and proxy support for the server."
        },
        "vault": {
            "summary": "Vault - Economy & permissions API",
            "description": "Provides a common API for economy, permissions, and chat plugins to interface with each other."
        },
        "worldguard": {
            "summary": "WorldGuard - Region protection",
            "description": "Protects regions from griefing, controls PvP, mob spawning, and other gameplay mechanics.",
            "commands": [
                {"command": "//wand", "description": "Get the region selection wand", "permission": "worldedit.wand", "usage": "//wand"},
                {"command": "/rg define", "description": "Define a new region", "permission": "worldguard.region.define", "usage": "/rg define <name>"},
                {"command": "/rg flag", "description": "Set region flags", "permission": "worldguard.region.flag", "usage": "/rg flag <region> <flag> <value>"},
                {"command": "/rg addmember", "description": "Add a member to a region", "permission": "worldguard.region.addmember.own", "usage": "/rg addmember <region> <player>"}
            ]
        },
        "worldedit": {
            "summary": "WorldEdit - In-game map editor",
            "description": "In-game world editing tool. Build massive structures quickly with selections and patterns.",
            "commands": [
                {"command": "//wand", "description": "Get the selection wand", "permission": "worldedit.wand", "usage": "//wand"},
                {"command": "//set", "description": "Set all blocks in selection", "permission": "worldedit.region.set", "usage": "//set <block>"},
                {"command": "//copy", "description": "Copy selection to clipboard", "permission": "worldedit.clipboard.copy", "usage": "//copy"},
                {"command": "//paste", "description": "Paste clipboard contents", "permission": "worldedit.clipboard.paste", "usage": "//paste"},
                {"command": "//undo", "description": "Undo last action", "permission": "worldedit.history.undo", "usage": "//undo [count]"}
            ]
        },
        "placeholderapi": {
            "summary": "PlaceholderAPI - Placeholder support",
            "description": "Provides placeholders that can be used across different plugins for dynamic text."
        },
        "ndailyrewards": {
            "summary": "NDailyRewards - Daily rewards system",
            "description": "Reward players for logging in daily with customizable rewards."
        },
        "tab": {
            "summary": "TAB - Tab list customization",
            "description": "Customizes the player tab list with prefixes, suffixes, and formatting."
        },
        "playerauctions": {
            "summary": "PlayerAuctions - Player-to-player auctions",
            "description": "Allows players to auction items to each other with a built-in GUI."
        },
        "ajleaderboards": {
            "summary": "ajLeaderboards - Leaderboard display",
            "description": "Creates in-game leaderboards with holograms for various statistics."
        }
    }

    with _file_lock:
        data = _load_docs()
        existing_plugins = data.setdefault("plugins", {})

        for plugin_id, plugin_config in plugins.items():
            if plugin_id not in existing_plugins:
                # Get description data or use defaults
                desc_data = plugin_descriptions.get(plugin_id, {})

                existing_plugins[plugin_id] = {
                    "summary": desc_data.get("summary", f"{plugin_id.title()} plugin"),
                    "description": desc_data.get("description", ""),
                    "commands": [],
                    "key_settings": [],
                    "comments": [],
                    "updated_by": "system",
                    "updated_at": datetime.now().isoformat()
                }

                # Add default commands if available
                for cmd in desc_data.get("commands", []):
                    cmd_entry = {
                        "id": f"cmd_{uuid.uuid4().hex[:8]}",
                        "command": cmd["command"],
                        "description": cmd["description"],
                        "permission": cmd.get("permission", ""),
                        "usage": cmd.get("usage", ""),
                        "added_by": "system",
                        "added_at": datetime.now().isoformat()
                    }
                    existing_plugins[plugin_id]["commands"].append(cmd_entry)

        _save_docs(data)
        return len(existing_plugins)
