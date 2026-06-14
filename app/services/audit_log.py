from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any


AUDIT_ROTATE_MAX_BYTES = int(os.getenv("AUDIT_ROTATE_MAX_BYTES", str(1024 * 1024)))
AUDIT_ROTATE_RETENTION = int(os.getenv("AUDIT_ROTATE_RETENTION", "5"))


def _rotate_file_if_needed(file_path: Path) -> bool:
    if AUDIT_ROTATE_MAX_BYTES <= 0 or not file_path.exists():
        return False
    if file_path.stat().st_size < AUDIT_ROTATE_MAX_BYTES:
        return False

    retention = max(AUDIT_ROTATE_RETENTION, 1)
    oldest_file = Path(f"{file_path}.{retention}")
    if oldest_file.exists():
        oldest_file.unlink()

    for idx in range(retention - 1, 0, -1):
        src = Path(f"{file_path}.{idx}")
        dst = Path(f"{file_path}.{idx + 1}")
        if src.exists():
            src.rename(dst)

    file_path.rename(Path(f"{file_path}.1"))
    return True


def _rotate_logger_files_if_needed(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None):
            handler.acquire()
            try:
                handler.flush()
                did_rotate = _rotate_file_if_needed(Path(handler.baseFilename))
                if did_rotate:
                    if handler.stream:
                        handler.stream.close()
                    handler.stream = handler._open()
            finally:
                handler.release()


def audit_event(*, logger, actor: str, action: str, target: str = "", result: str = "", extra: dict[str, Any] | None = None) -> None:
    _rotate_logger_files_if_needed(logger)
    payload: dict[str, Any] = {
        "ts": int(time.time()),
        "actor": actor,
        "action": action,
        "target": target,
        "result": result,
    }
    if extra:
        payload.update(extra)
    logger.info(json.dumps(payload, ensure_ascii=False))
