import asyncio
import json
import logging
import uuid

from app.services import audit_log
from app.services import operations


def _setup_operation_test_env(monkeypatch, tmp_path):
    monkeypatch.setattr(operations, "_OPERATION_STATE_FILE", tmp_path / "operation_state.jsonl")
    monkeypatch.setattr(operations, "_IDEMPOTENCY_TTL_SECONDS", 300)
    monkeypatch.setattr(operations, "check_rate_limit", lambda **kwargs: (True, 0))
    monkeypatch.setattr(operations, "is_admin", lambda user_info: True)
    monkeypatch.setattr(operations.minecraft_settings, "get_active_profile_operation_block", lambda *args, **kwargs: None)
    operations._IDEMPOTENCY_CACHE.clear()


def test_same_operation_same_idempotency_key_executes_once(monkeypatch, tmp_path):
    _setup_operation_test_env(monkeypatch, tmp_path)
    calls = {"count": 0}

    async def _fake_start_server() -> dict:
        calls["count"] += 1
        return {"success": True, "message": "started"}

    monkeypatch.setattr(operations.minecraft_server, "start_server", _fake_start_server)

    user_info = {"email": "admin@example.com", "name": "Admin"}
    first = asyncio.run(
        operations.execute_operation(
            key="server:start",
            user_info=user_info,
            idempotency_key="dup-token",
        )
    )
    second = asyncio.run(
        operations.execute_operation(
            key="server:start",
            user_info=user_info,
            idempotency_key="dup-token",
        )
    )

    assert first["success"] is True
    assert second["success"] is True
    assert second.get("idempotent_replay") is True
    assert calls["count"] == 1

    state_file = tmp_path / "operation_state.jsonl"
    lines = state_file.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert any(r["status"] == "started" for r in records)
    assert any(r["status"] == "succeeded" for r in records)
    assert all(r["idempotency_key"] == "dup-token" for r in records)


def test_same_operation_different_idempotency_keys_execute_twice(monkeypatch, tmp_path):
    _setup_operation_test_env(monkeypatch, tmp_path)
    calls = {"count": 0}

    async def _fake_start_server() -> dict:
        calls["count"] += 1
        return {"success": True, "message": "started"}

    monkeypatch.setattr(operations.minecraft_server, "start_server", _fake_start_server)

    user_info = {"email": "admin@example.com", "name": "Admin"}
    asyncio.run(
        operations.execute_operation(
            key="server:start",
            user_info=user_info,
            idempotency_key="token-a",
        )
    )
    asyncio.run(
        operations.execute_operation(
            key="server:start",
            user_info=user_info,
            idempotency_key="token-b",
        )
    )

    assert calls["count"] == 2


def test_audit_log_rotates_and_enforces_retention(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_log, "AUDIT_ROTATE_MAX_BYTES", 150)
    monkeypatch.setattr(audit_log, "AUDIT_ROTATE_RETENTION", 2)

    logger = logging.getLogger(f"test.audit.{uuid.uuid4()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_file = tmp_path / "audit.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    logger.addHandler(handler)

    try:
        for i in range(12):
            audit_log.audit_event(
                logger=logger,
                actor="tester",
                action="rotate_test",
                target=f"entry-{i}",
                result="ok",
                extra={"payload": "x" * 80},
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert log_file.exists()
    assert (tmp_path / "audit.log.1").exists()
    assert (tmp_path / "audit.log.2").exists()
    assert not (tmp_path / "audit.log.3").exists()
