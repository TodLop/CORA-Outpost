import asyncio
import json
from datetime import datetime

from app.services import minecraft_server
from app.services import operations
from app.services import reboot_scheduler


def _setup_restart_state_test_env(monkeypatch, tmp_path):
    state_dir = tmp_path / "profile_state" / "active"
    monkeypatch.setattr(
        minecraft_server.minecraft_settings,
        "get_active_profile_state_dir",
        lambda: state_dir,
    )
    monkeypatch.setattr(
        minecraft_server.minecraft_settings,
        "get_active_profile_operation_block",
        lambda *args, **kwargs: None,
    )
    return state_dir


def _setup_operation_test_env(monkeypatch, tmp_path):
    monkeypatch.setattr(operations, "_OPERATION_STATE_FILE", tmp_path / "operation_state.jsonl")
    monkeypatch.setattr(operations, "_IDEMPOTENCY_TTL_SECONDS", 300)
    monkeypatch.setattr(operations, "check_rate_limit", lambda **kwargs: (True, 0))
    monkeypatch.setattr(operations, "is_admin", lambda user_info: True)
    monkeypatch.setattr(operations.minecraft_settings, "get_active_profile_operation_block", lambda *args, **kwargs: None)
    operations._IDEMPOTENCY_CACHE.clear()


def test_restart_rejected_when_in_progress(monkeypatch, tmp_path):
    _setup_restart_state_test_env(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()
    manager.restart_in_progress = True

    result = asyncio.run(manager.restart_server(source="admin_ui"))

    assert result["success"] is False
    assert result["error_code"] == "restart_in_progress"


def test_restart_rejected_during_cooldown(monkeypatch, tmp_path):
    _setup_restart_state_test_env(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()
    manager.last_restart_completed_at = datetime.now()
    manager.last_restart_source = "admin_ui"

    result = asyncio.run(manager.restart_server(source="staff_ui"))

    assert result["success"] is False
    assert result["error_code"] == "restart_cooldown"
    assert result["retry_after_seconds"] > 0
    assert result["last_restart_source"] == "admin_ui"


def test_restart_sets_cooldown_after_success(monkeypatch, tmp_path):
    state_dir = _setup_restart_state_test_env(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()

    async def _fake_sleep(seconds):
        return None

    async def _fake_stop_server(force: bool = False):
        return {"success": True, "message": "stopped"}

    async def _fake_start_server(
        wait_for_ready: bool = False,
        ready_timeout_sec: int = minecraft_server.DEFAULT_READY_TIMEOUT_SEC,
        require_rcon_ready: bool = True,
    ):
        return {"success": True, "message": "started"}

    monkeypatch.setattr(minecraft_server.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(manager, "stop_server", _fake_stop_server)
    monkeypatch.setattr(manager, "start_server", _fake_start_server)

    first = asyncio.run(manager.restart_server(source="admin_ui"))
    second = asyncio.run(manager.restart_server(source="staff_ui"))

    assert first["success"] is True
    assert second["success"] is False
    assert second["error_code"] == "restart_cooldown"
    assert second["last_restart_source"] == "admin_ui"
    assert (state_dir / "minecraft_restart_state.json").exists()


def test_restart_state_file_is_profile_scoped(monkeypatch, tmp_path):
    state_dir = _setup_restart_state_test_env(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()
    manager.last_restart_completed_at = datetime.now()
    manager.last_restart_source = "admin_ui"

    manager._save_restart_state()

    restart_state_file = state_dir / "minecraft_restart_state.json"
    assert minecraft_server.get_restart_state_file() == restart_state_file
    assert restart_state_file.exists()
    assert json.loads(restart_state_file.read_text(encoding="utf-8"))["last_restart_source"] == "admin_ui"


def test_restart_lock_file_is_profile_scoped(monkeypatch, tmp_path):
    state_dir = _setup_restart_state_test_env(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()

    handle = manager._acquire_restart_file_lock()
    try:
        assert handle is not None
        assert minecraft_server.get_restart_lock_file() == state_dir / "minecraft_restart.lock"
        assert (state_dir / "minecraft_restart.lock").exists()
        assert handle.name == str(state_dir / "minecraft_restart.lock")
    finally:
        if handle is not None:
            minecraft_server.ServerManager._release_restart_file_lock(handle)


def test_switching_active_profile_changes_restart_state_and_lock_paths(monkeypatch, tmp_path):
    active_profile = {"id": "server-a"}
    state_root = tmp_path / "profile_state"
    monkeypatch.setattr(
        minecraft_server.minecraft_settings,
        "get_active_profile_state_dir",
        lambda: state_root / active_profile["id"],
    )

    paths_a = {
        minecraft_server.get_restart_state_file(),
        minecraft_server.get_restart_lock_file(),
    }
    active_profile["id"] = "server-b"
    paths_b = {
        minecraft_server.get_restart_state_file(),
        minecraft_server.get_restart_lock_file(),
    }

    assert paths_a == {
        state_root / "server-a" / "minecraft_restart_state.json",
        state_root / "server-a" / "minecraft_restart.lock",
    }
    assert paths_b == {
        state_root / "server-b" / "minecraft_restart_state.json",
        state_root / "server-b" / "minecraft_restart.lock",
    }
    assert paths_a.isdisjoint(paths_b)


def test_execute_operation_passes_restart_source(monkeypatch, tmp_path):
    _setup_operation_test_env(monkeypatch, tmp_path)
    captured = {}

    async def _fake_restart_server(**kwargs):
        captured["source"] = kwargs.get("source")
        return {"success": True, "message": "restarted"}

    monkeypatch.setattr(operations.minecraft_server, "restart_server", _fake_restart_server)

    result = asyncio.run(
        operations.execute_operation(
            key="server:restart",
            user_info={"email": "admin@example.com", "name": "Admin"},
            params={"source": "staff_ui"},
            idempotency_key="restart-source-token",
        )
    )

    assert result["success"] is True
    assert captured["source"] == "staff_ui"


def test_reboot_scheduler_skips_when_restart_cooldown(monkeypatch, tmp_path):
    monkeypatch.setattr(reboot_scheduler, "CONFIG_FILE", tmp_path / "reboot_scheduler_config.json")
    monkeypatch.setattr(reboot_scheduler, "LOG_FILE", tmp_path / "reboot_scheduler_log.json")

    scheduler = reboot_scheduler.RebootScheduler()
    scheduler.status.state = reboot_scheduler.SchedulerState.COUNTDOWN_UPTIME
    scheduler.status.players_online = 0
    token = scheduler._new_restart_token()

    async def _fake_send_command(command: str):
        return {"success": True, "message": "ok"}

    async def _fake_restart_server(**kwargs):
        return {
            "success": False,
            "error": "Restart cooldown active",
            "error_code": "restart_cooldown",
            "retry_after_seconds": 95,
        }

    monkeypatch.setattr(
        reboot_scheduler.minecraft_server,
        "get_restart_gate_status",
        lambda: {
            "can_restart": True,
            "in_progress": False,
            "cooldown_remaining_seconds": 0,
            "last_restart_completed_at": None,
            "last_restart_source": "",
        },
    )
    monkeypatch.setattr(reboot_scheduler.minecraft_server, "send_command", _fake_send_command)
    monkeypatch.setattr(reboot_scheduler.minecraft_server, "restart_server", _fake_restart_server)

    asyncio.run(scheduler._execute_restart("uptime", token=token))

    assert scheduler.status.state == reboot_scheduler.SchedulerState.MONITORING
    assert scheduler.status.error_message is None
    assert scheduler.logs[-1].action == "restart_skipped"


def test_reboot_scheduler_warning_failure_is_not_deduped(monkeypatch, tmp_path):
    monkeypatch.setattr(reboot_scheduler, "CONFIG_FILE", tmp_path / "reboot_scheduler_config.json")
    monkeypatch.setattr(reboot_scheduler, "LOG_FILE", tmp_path / "reboot_scheduler_log.json")

    scheduler = reboot_scheduler.RebootScheduler()
    scheduler.status.state = reboot_scheduler.SchedulerState.COUNTDOWN_UPTIME
    scheduler._new_restart_token()
    reboot_scheduler._last_warning_sent.clear()

    call_count = {"n": 0}

    async def _fake_send_command(command: str):
        call_count["n"] += 1
        return {"success": False, "error": "rcon down"}

    monkeypatch.setattr(reboot_scheduler.minecraft_server, "send_command", _fake_send_command)

    asyncio.run(scheduler._send_warning(1))
    asyncio.run(scheduler._send_warning(1))

    assert call_count["n"] == 6
    assert scheduler.logs[-1].status == "failed"


def test_reboot_scheduler_failure_does_not_start_grace(monkeypatch, tmp_path):
    monkeypatch.setattr(reboot_scheduler, "CONFIG_FILE", tmp_path / "reboot_scheduler_config.json")
    monkeypatch.setattr(reboot_scheduler, "LOG_FILE", tmp_path / "reboot_scheduler_log.json")

    scheduler = reboot_scheduler.RebootScheduler()
    scheduler.status.state = reboot_scheduler.SchedulerState.COUNTDOWN_UPTIME
    token = scheduler._new_restart_token()

    monkeypatch.setattr(
        reboot_scheduler.minecraft_server,
        "get_restart_gate_status",
        lambda: {
            "can_restart": True,
            "in_progress": False,
            "cooldown_remaining_seconds": 0,
            "last_restart_completed_at": None,
            "last_restart_source": "",
        },
    )

    async def _fake_restart_server(**kwargs):
        return {"success": False, "error": "boom", "error_code": "internal"}

    async def _ok_command(command: str):
        return {"success": True}

    monkeypatch.setattr(reboot_scheduler.minecraft_server, "restart_server", _fake_restart_server)
    monkeypatch.setattr(reboot_scheduler.minecraft_server, "send_command", _ok_command)

    asyncio.run(scheduler._execute_restart("uptime", token=token))

    assert scheduler.status.state == reboot_scheduler.SchedulerState.ERROR
    assert scheduler._last_restart_completed_at is None


def test_execute_operation_fails_when_server_operation_busy(monkeypatch, tmp_path):
    _setup_operation_test_env(monkeypatch, tmp_path)
    monkeypatch.setattr(operations, "_acquire_server_operation_lock", lambda: None)

    result = asyncio.run(
        operations.execute_operation(
            key="server:restart",
            user_info={"email": "admin@example.com", "name": "Admin"},
            params={"source": "staff_ui"},
            idempotency_key="restart-busy-token",
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "server_operation_in_progress"
