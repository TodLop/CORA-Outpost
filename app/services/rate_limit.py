from __future__ import annotations

import time
from collections import defaultdict


_buckets: dict[str, dict[str, list[float]]] = defaultdict(dict)


def check_rate_limit(*, bucket: str, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    now = time.time()
    by_key = _buckets[bucket]
    timestamps = by_key.get(key, [])
    timestamps = [t for t in timestamps if now - t < window_seconds]

    if len(timestamps) >= limit:
        oldest = min(timestamps) if timestamps else now
        retry_after = max(1, int(window_seconds - (now - oldest)))
        by_key[key] = timestamps
        return False, retry_after

    timestamps.append(now)
    by_key[key] = timestamps
    return True, 0


def clear_bucket(bucket: str) -> None:
    _buckets.pop(bucket, None)
