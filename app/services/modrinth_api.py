import json
import threading
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
import httpx
import asyncio
from datetime import datetime, timedelta, timezone

from app.core.config import DATA_DIR

logger = logging.getLogger(__name__)

MODRINTH_CACHE_FILE = DATA_DIR / "modrinth_cache.json"
MODRINTH_API_BASE = "https://api.modrinth.com/v2"
CACHE_TTL_DAYS = 7

_file_lock = threading.Lock()


def _load_cache() -> dict:
    if not MODRINTH_CACHE_FILE.exists():
        return {"plugins": {}}
    try:
        with open(MODRINTH_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"[ModrinthAPI] Error loading cache: {e}")
        return {"plugins": {}}


def _save_cache(data: dict) -> bool:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(MODRINTH_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except IOError as e:
        logger.warning(f"[ModrinthAPI] Error saving cache: {e}")
        return False


def _is_cache_expired(plugin_data: Dict[str, Any]) -> bool:
    """Check if cached data is older than CACHE_TTL_DAYS"""
    cached_at_str = plugin_data.get("_cached_at")
    if not cached_at_str:
        return True

    try:
        cached_at = datetime.fromisoformat(cached_at_str)
        now = datetime.now(timezone.utc)
        age = now - cached_at
        return age > timedelta(days=CACHE_TTL_DAYS)
    except (ValueError, TypeError):
        return True


def get_plugin_from_cache(project_id: str) -> Optional[Dict[str, Any]]:
    with _file_lock:
        cache = _load_cache()
    return cache.get("plugins", {}).get(project_id)


def save_plugin_to_cache(project_id: str, data: Dict[str, Any]) -> bool:
    with _file_lock:
        cache = _load_cache()
        data["_cached_at"] = datetime.now(timezone.utc).isoformat()
        cache.setdefault("plugins", {})[project_id] = data
        return _save_cache(cache)


async def fetch_plugin_from_modrinth(project_id: str) -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{MODRINTH_API_BASE}/project/{project_id}")
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.info(f"[ModrinthAPI] Project not found: {project_id}")
                return None
            else:
                logger.warning(f"[ModrinthAPI] Error fetching {project_id}: {response.status_code}")
                return None
    except Exception as e:
        logger.warning(f"[ModrinthAPI] Exception fetching {project_id}: {e}")
        return None


async def get_plugin_icon(project_id: str) -> Optional[str]:
    cache_data = get_plugin_from_cache(project_id)

    if cache_data and "icon_url" in cache_data:
        if _is_cache_expired(cache_data):
            logger.info(f"[ModrinthAPI] Cache expired for {project_id}, fetching fresh data")
        else:
            return cache_data["icon_url"]

    data = await fetch_plugin_from_modrinth(project_id)
    if data and "icon_url" in data:
        icon_url = data["icon_url"]
        save_plugin_to_cache(project_id, data)
        logger.info(f"[ModrinthAPI] Fetched icon for {project_id}: {icon_url}")
        return icon_url

    logger.warning(f"[ModrinthAPI] No icon found for {project_id}")
    return None


async def batch_get_icons(project_ids: List[str]) -> Dict[str, str]:
    """Batch fetch plugin icons with caching and error handling.

    Strategy:
    1. Check cache for each plugin (with TTL validation)
    2. For uncached/expired plugins, fetch in batches of 5
    3. Handle errors gracefully - continue fetching other plugins on failure
    4. Return all available icons (cached + fetched)
    """
    results = {}
    uncached_ids = []
    expired_ids = []

    for pid in project_ids:
        cache_data = get_plugin_from_cache(pid)
        if cache_data and "icon_url" in cache_data:
            if _is_cache_expired(cache_data):
                logger.info(f"[ModrinthAPI] Cache expired for {pid}, will fetch fresh")
                expired_ids.append(pid)
            else:
                results[pid] = cache_data["icon_url"]
        else:
            uncached_ids.append(pid)

    ids_to_fetch = uncached_ids + expired_ids

    if not ids_to_fetch:
        logger.info(f"[ModrinthAPI] All {len(results)} icons found in cache")
        return results

    logger.info(f"[ModrinthAPI] Fetching icons for {len(ids_to_fetch)} plugins: {ids_to_fetch[:5]}{'...' if len(ids_to_fetch) > 5 else ''}")

    async with httpx.AsyncClient(timeout=10.0) as client:
        BATCH_SIZE = 5
        total_batches = (len(ids_to_fetch) + BATCH_SIZE - 1) // BATCH_SIZE

        for batch_num in range(total_batches):
            start_idx = batch_num * BATCH_SIZE
            end_idx = min(start_idx + BATCH_SIZE, len(ids_to_fetch))
            batch = ids_to_fetch[start_idx:end_idx]

            logger.info(f"[ModrinthAPI] Processing batch {batch_num + 1}/{total_batches}: {batch}")

            tasks = [fetch_plugin_from_modrinth(pid) for pid in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for pid, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.warning(f"[ModrinthAPI] Error fetching {pid}: {result}")
                    continue

                if result and "icon_url" in result:
                    results[pid] = result["icon_url"]
                    save_plugin_to_cache(pid, result)
                    logger.info(f"[ModrinthAPI] Fetched icon for {pid}")
                else:
                    logger.warning(f"[ModrinthAPI] No icon found for {pid}")

    logger.info(f"[ModrinthAPI] Fetched {len(results)}/{len(project_ids)} icons total")
    return results
