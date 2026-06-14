import asyncio
from pathlib import Path

import pytest

from app.services import minecraft_server, minecraft_settings, operations


def _make_server_dir(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "server.properties").write_text("enable-rcon=true\nrcon.password=test\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "plugins").mkdir()
    return root


def _use_temp_profile_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_settings, "PROFILE_STATE_ROOT", tmp_path / "profile_state")


def _create_live_profile(monkeypatch, tmp_path, *, rcon_enabled: bool = True):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    server_dir = _make_server_dir(tmp_path / "live_server")
    minecraft_settings.create_profile(
        profile_id="live",
        name="Live Server",
        server_directory=server_dir,
        operations_enabled=True,
        rcon_enabled=rcon_enabled,
        readonly=False,
        set_active=True,
    )
    return server_dir


def _create_operations_disabled_profile(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    server_dir = _make_server_dir(tmp_path / "disabled_server")
    minecraft_settings.create_profile(
        profile_id="disabled",
        name="Disabled Server",
        server_directory=server_dir,
        operations_enabled=False,
        rcon_enabled=True,
        readonly=False,
        set_active=True,
    )
    return server_dir


def _setup_operation_test_env(monkeypatch, tmp_path):
    monkeypatch.setattr(operations, "_OPERATION_STATE_FILE", tmp_path / "operation_state.jsonl")
    monkeypatch.setattr(operations, "_SERVER_OPERATION_LOCK_FILE", tmp_path / "server_operation.lock")
    monkeypatch.setattr(operations, "_IDEMPOTENCY_TTL_SECONDS", 300)
    monkeypatch.setattr(operations, "check_rate_limit", lambda **kwargs: (True, 0))
    monkeypatch.setattr(operations, "is_admin", lambda user_info: True)
    operations._IDEMPOTENCY_CACHE.clear()


def test_sample_profile_blocks_lifecycle_before_runtime_checks(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("runtime lifecycle checks should not run for blocked profiles")

    monkeypatch.setattr(manager, "_is_server_running_sync", fail_if_called)
    monkeypatch.setattr(manager, "_write_pid_file", fail_if_called)
    monkeypatch.setattr(manager, "_acquire_restart_file_lock", fail_if_called)
    monkeypatch.setattr(manager, "get_server_status", fail_if_called)
    monkeypatch.setattr(minecraft_server.subprocess, "Popen", fail_if_called)
    monkeypatch.setattr(minecraft_server.os, "kill", fail_if_called)

    start = asyncio.run(manager.start_server())
    stop = asyncio.run(manager.stop_server())
    restart = asyncio.run(manager.restart_server())
    recover = asyncio.run(manager.recover_server())

    assert start["error_code"] == "profile_readonly"
    assert stop["error_code"] == "profile_readonly"
    assert restart["error_code"] == "profile_readonly"
    assert recover["error_code"] == "profile_readonly"


def test_sample_profile_blocks_rcon_before_runtime_checks(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("RCON/runtime checks should not run for blocked profiles")

    monkeypatch.setattr(manager, "_is_server_running_sync", fail_if_called)
    monkeypatch.setattr(minecraft_server, "get_rcon_config", fail_if_called)
    monkeypatch.setattr(minecraft_server, "RCONClient", fail_if_called)

    result = asyncio.run(manager.send_command("say hello"))

    assert result["success"] is False
    assert result["error_code"] == "profile_readonly"
    assert result["profile_id"] == "sample"


def test_operations_disabled_profile_blocks_before_rcon_flag(monkeypatch, tmp_path):
    _create_operations_disabled_profile(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("runtime checks should not run when operations are disabled")

    monkeypatch.setattr(manager, "_is_server_running_sync", fail_if_called)

    result = asyncio.run(manager.send_command("list"))

    assert result["success"] is False
    assert result["error_code"] == "profile_operations_disabled"
    assert result["profile_id"] == "disabled"


def test_module_level_start_guard_blocks_singleton_manager(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("singleton manager should not run when module guard blocks")

    monkeypatch.setattr(minecraft_server._manager, "start_server", fail_if_called)

    result = asyncio.run(minecraft_server.start_server())

    assert result["success"] is False
    assert result["error_code"] == "profile_readonly"


def test_rcon_disabled_live_profile_blocks_explicit_send_command(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, rcon_enabled=False)
    manager = minecraft_server.ServerManager()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("RCON checks should not run when profile RCON is disabled")

    monkeypatch.setattr(manager, "_is_server_running_sync", fail_if_called)
    monkeypatch.setattr(minecraft_server, "get_rcon_config", fail_if_called)

    result = asyncio.run(manager.send_command("list"))

    assert result["success"] is False
    assert result["error_code"] == "profile_rcon_disabled"
    assert result["profile_id"] == "live"


@pytest.mark.asyncio
async def test_live_profile_allows_start_path(monkeypatch, tmp_path):
    server_dir = _create_live_profile(monkeypatch, tmp_path)
    calls = {}

    class FakeProcess:
        pid = 12345

    def fake_popen(args, **kwargs):
        calls["args"] = args
        calls["cwd"] = kwargs.get("cwd")
        return FakeProcess()

    manager = minecraft_server.ServerManager()
    monkeypatch.setattr(manager, "_is_server_running_sync", lambda: False)
    monkeypatch.setattr(manager, "_tail_log_file", lambda: asyncio.sleep(0))
    monkeypatch.setattr(minecraft_server.subprocess, "Popen", fake_popen)

    result = await manager.start_server()
    if manager.log_reader_task is not None:
        await manager.log_reader_task

    assert result["success"] is True
    assert result["pid"] == 12345
    assert calls["cwd"] == str(server_dir)
    assert calls["args"] == ["sh", str(server_dir / "start.sh")]
    assert minecraft_server.get_pid_file().read_text(encoding="utf-8") == "12345"


def test_active_profile_controls_guard_decision(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    server_dir = _make_server_dir(tmp_path / "live_server")
    assert minecraft_settings.get_active_profile_operation_block("start server")["error_code"] == "profile_readonly"

    minecraft_settings.create_profile(
        profile_id="live",
        name="Live Server",
        server_directory=server_dir,
        operations_enabled=True,
        rcon_enabled=True,
        readonly=False,
        set_active=True,
    )

    assert minecraft_settings.get_active_profile_operation_block("start server") is None


def test_execute_operation_blocks_before_state_lock_and_executor(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    _setup_operation_test_env(monkeypatch, tmp_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("operation state, locks, and executors should not run when profile blocks")

    monkeypatch.setattr(operations, "_append_operation_state", fail_if_called)
    monkeypatch.setattr(operations, "_acquire_server_operation_lock", fail_if_called)
    monkeypatch.setattr(operations.minecraft_server, "start_server", fail_if_called)

    result = asyncio.run(
        operations.execute_operation(
            key="server:start",
            user_info={"email": "staff@example.com", "name": "Staff"},
            idempotency_key="blocked-start",
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "profile_readonly"
    assert not (tmp_path / "operation_state.jsonl").exists()


def test_execute_operation_blocks_staff_restart_consistently(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    _setup_operation_test_env(monkeypatch, tmp_path)
    monkeypatch.setattr(operations, "is_admin", lambda user_info: False)
    monkeypatch.setattr(operations.permissions_service, "has_permission", lambda email, permission: True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("restart executor should not run when profile blocks")

    monkeypatch.setattr(operations.minecraft_server, "restart_server", fail_if_called)

    result = asyncio.run(
        operations.execute_operation(
            key="server:restart",
            user_info={"email": "staff@example.com", "name": "Staff"},
            params={"source": "staff_ui"},
            idempotency_key="blocked-restart",
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "profile_readonly"


def test_read_only_status_and_logs_remain_allowed(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()
    monkeypatch.setattr(manager, "_get_process_snapshot_sync", lambda: (False, None, False))
    monkeypatch.setattr(manager, "_is_port_listening", lambda *args, **kwargs: False)

    status = manager.get_server_status()
    logs = manager.get_recent_logs(10)

    assert status.running is False
    assert logs == []


def test_path_dependent_runtime_reset_clears_profile_scoped_buffers():
    from app.services import whitelist_autocomplete_cache

    minecraft_server._manager.log_buffer.clear()
    minecraft_server._manager.log_buffer.append({"time": "12:00:00", "message": "old profile"})
    minecraft_server._manager.last_log_position = 123
    minecraft_server._manager.last_log_inode = 456
    whitelist_autocomplete_cache.store_players(["OldPlayer"], now=1.0)

    try:
        minecraft_server.reset_path_dependent_runtime_state()

        assert list(minecraft_server._manager.log_buffer) == []
        assert minecraft_server._manager.last_log_position == 0
        assert minecraft_server._manager.last_log_inode is None
        assert whitelist_autocomplete_cache.get_stale_players() == []
    finally:
        minecraft_server._manager.log_buffer.clear()
        whitelist_autocomplete_cache.invalidate()
