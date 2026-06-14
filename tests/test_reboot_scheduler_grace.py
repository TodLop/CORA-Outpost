"""Tests for reboot scheduler grace period and auto-recovery logic."""

import asyncio
import fcntl
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services import minecraft_server
from app.services import reboot_scheduler


def _make_scheduler(monkeypatch, tmp_path):
    """Create a scheduler with isolated config/log files."""
    monkeypatch.setattr(reboot_scheduler, "CONFIG_FILE", tmp_path / "cfg.json")
    monkeypatch.setattr(reboot_scheduler, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(reboot_scheduler, "INSTANCE_LOCK_FILE", tmp_path / "scheduler.instance.lock")
    monkeypatch.setattr(
        reboot_scheduler.minecraft_settings,
        "get_active_profile_operation_block",
        lambda *args, **kwargs: None,
    )
    sched = reboot_scheduler.RebootScheduler()
    sched.config.enabled = True
    sched.config.empty_server_enabled = True
    sched.config.empty_hours_threshold = 1.0
    sched.config.uptime_restart_enabled = True
    sched.config.max_uptime_hours = 8.0
    sched.config.restart_grace_minutes = 30
    return sched


@dataclass
class _FakeServerStatus:
    running: bool = True
    process_running: bool = True
    game_port_listening: bool = True
    rcon_port_listening: bool = True
    healthy: bool = True
    state_reason: str = "ok"
    pid: int = 12345
    players_online: int = 0
    max_players: int = 20


def _patch_status(monkeypatch, status):
    """Monkey-patch minecraft_server.get_server_status to return *status*."""
    monkeypatch.setattr(reboot_scheduler.minecraft_server, "get_server_status", lambda: status)


def _patch_commands(monkeypatch):
    """Patch send_command and restart_server to no-ops."""
    async def _noop_cmd(cmd):
        return {"success": True}

    async def _noop_restart(**kwargs):
        return {"success": True, "message": "restarted"}

    monkeypatch.setattr(reboot_scheduler.minecraft_server, "send_command", _noop_cmd)
    monkeypatch.setattr(reboot_scheduler.minecraft_server, "restart_server", _noop_restart)
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


def _write_disk_config(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ── Grace period blocks empty-server restart ──


def test_grace_period_blocks_empty_restart(monkeypatch, tmp_path):
    """After a restart, the scheduler should NOT trigger an empty-server
    restart during the grace period, even if the server has been empty
    longer than empty_hours_threshold."""
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=0))
    _patch_commands(monkeypatch)

    # Simulate: restart just completed 5 minutes ago
    sched._last_restart_completed_at = datetime.now() - timedelta(minutes=5)
    # Simulate: server has been empty for 2 hours (would normally trigger restart)
    sched._empty_since = datetime.now() - timedelta(hours=2)
    sched._server_start_time = datetime.now() - timedelta(hours=2)

    asyncio.run(sched._check_and_act())

    # Should stay in MONITORING (grace period active), NOT start a countdown
    assert sched.status.state == reboot_scheduler.SchedulerState.MONITORING
    assert "grace period" in (sched.status.next_action or "").lower()


def test_grace_period_still_updates_uptime(monkeypatch, tmp_path):
    """Even during grace period, uptime should remain populated."""
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=0, pid=54321))
    _patch_commands(monkeypatch)

    process_start = datetime.now() - timedelta(hours=2, minutes=10)
    monkeypatch.setattr(
        reboot_scheduler.minecraft_server._manager,
        "_get_process_start_time",
        lambda pid: process_start,
    )

    sched._server_start_time = None
    sched._empty_since = None
    sched._last_restart_completed_at = datetime.now() - timedelta(minutes=2)

    asyncio.run(sched._check_and_act())

    assert sched.status.state == reboot_scheduler.SchedulerState.MONITORING
    assert "grace period" in (sched.status.next_action or "").lower()
    assert sched._server_start_time == process_start
    assert sched.status.server_started_at == process_start.isoformat()
    assert sched.status.uptime_seconds > 0
    assert sched.status.uptime_formatted != "0h 0m"


def test_realtime_status_bootstraps_uptime_without_monitor_loop(monkeypatch, tmp_path):
    """Status API path should populate uptime/empty even before monitor loop priming."""
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=0, pid=98765))
    _patch_commands(monkeypatch)

    process_start = datetime.now() - timedelta(hours=1, minutes=20)
    monkeypatch.setattr(
        reboot_scheduler.minecraft_server._manager,
        "_get_process_start_time",
        lambda pid: process_start,
    )

    sched._server_start_time = None
    sched._empty_since = None

    status = sched.get_status()

    assert status["server_running"] is True
    assert status["uptime_seconds"] > 0
    assert status["uptime_formatted"] != "0h 0m"
    assert status["empty_seconds"] > 0
    assert status["empty_since"] == process_start.isoformat()
    # Display fallback should not mutate scheduler trigger baseline.
    assert sched._empty_since is None


# ── Grace period expires and restart triggers normally ──


def test_grace_period_expires(monkeypatch, tmp_path):
    """After the grace period ends, the empty-server restart should trigger
    normally."""
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=0))
    _patch_commands(monkeypatch)

    # Grace period expired 5 minutes ago
    sched._last_restart_completed_at = datetime.now() - timedelta(minutes=35)
    # Server empty for 2 hours
    sched._empty_since = datetime.now() - timedelta(hours=2)
    sched._server_start_time = datetime.now() - timedelta(hours=2)

    asyncio.run(sched._check_and_act())

    # Should have triggered restart (state becomes RESTARTING or COUNTDOWN)
    assert sched.status.state in (
        reboot_scheduler.SchedulerState.COUNTDOWN_EMPTY,
        reboot_scheduler.SchedulerState.RESTARTING,
    )


# ── Zombie process auto-recovery ──


def test_degraded_auto_recover(monkeypatch, tmp_path):
    """When server is stuck in process_no_port for > 3 minutes, the scheduler
    should automatically trigger recover_server()."""
    sched = _make_scheduler(monkeypatch, tmp_path)

    degraded_status = _FakeServerStatus(
        running=False,
        process_running=True,
        game_port_listening=False,
        healthy=False,
        state_reason="process_no_port",
    )
    _patch_status(monkeypatch, degraded_status)

    recover_called = {"called": False}

    async def _fake_recover(**kwargs):
        recover_called["called"] = True
        return {"success": True, "message": "recovered"}

    monkeypatch.setattr(reboot_scheduler.minecraft_server, "recover_server", _fake_recover)

    # Simulate: degraded for 4 minutes already
    sched._degraded_since = datetime.now() - timedelta(minutes=4)

    asyncio.run(sched._check_and_act())

    assert recover_called["called"], "recover_server should have been called"
    assert sched._degraded_since is None, "should reset after recovery"
    # Grace period should be set after auto-recovery
    assert sched._last_restart_completed_at is not None


def test_degraded_waits_before_recovery(monkeypatch, tmp_path):
    """When server just entered process_no_port, the scheduler should wait
    before recovering (not trigger immediately)."""
    sched = _make_scheduler(monkeypatch, tmp_path)

    degraded_status = _FakeServerStatus(
        running=False,
        process_running=True,
        game_port_listening=False,
        healthy=False,
        state_reason="process_no_port",
    )
    _patch_status(monkeypatch, degraded_status)

    recover_called = {"called": False}

    async def _fake_recover(**kwargs):
        recover_called["called"] = True
        return {"success": True}

    monkeypatch.setattr(reboot_scheduler.minecraft_server, "recover_server", _fake_recover)

    # Simulate: degraded for only 1 minute
    sched._degraded_since = datetime.now() - timedelta(minutes=1)

    asyncio.run(sched._check_and_act())

    assert not recover_called["called"], "should NOT recover yet (only 1 min elapsed)"
    assert sched._degraded_since is not None, "should still be tracking degraded state"
    assert "degraded" in (sched.status.next_action or "").lower()


def test_disabled_scheduler_skips_automatic_actions(monkeypatch, tmp_path):
    """Disabled scheduler should not trigger countdown/restart logic in check loop."""
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=0))
    _patch_commands(monkeypatch)

    sched.config.enabled = False
    sched.config.coreprotect_purge_enabled = False
    sched._server_start_time = datetime.now() - timedelta(hours=2)
    sched._empty_since = datetime.now() - timedelta(hours=2)

    countdown_called = {"called": False}

    async def _fake_start_countdown(reason, details):
        countdown_called["called"] = True
        return True

    monkeypatch.setattr(sched, "_start_countdown", _fake_start_countdown)

    asyncio.run(sched._check_and_act())

    assert not countdown_called["called"]
    assert sched.status.state == reboot_scheduler.SchedulerState.DISABLED
    assert sched.status.next_action is None


def test_runtime_disk_reload_disables_loop_behavior(monkeypatch, tmp_path):
    """If disk config disables scheduler, loop should honor it before auto actions."""
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=0))
    _patch_commands(monkeypatch)

    sched.config.coreprotect_purge_enabled = False
    sched._server_start_time = datetime.now() - timedelta(hours=2)
    sched._empty_since = datetime.now() - timedelta(hours=2)

    disk_config = sched.config.to_dict()
    disk_config["enabled"] = False
    _write_disk_config(reboot_scheduler.CONFIG_FILE, disk_config)

    # Simulate stale in-memory state while file on disk has been disabled.
    sched.config.enabled = True

    countdown_called = {"called": False}

    async def _fake_start_countdown(reason, details):
        countdown_called["called"] = True
        return True

    monkeypatch.setattr(sched, "_start_countdown", _fake_start_countdown)

    asyncio.run(sched._check_and_act())

    assert sched.config.enabled is False
    assert not countdown_called["called"]
    assert sched.status.state == reboot_scheduler.SchedulerState.DISABLED


def test_coreprotect_purge_path_preserves_disabled_disk_config(monkeypatch, tmp_path):
    """CoreProtect purge writes should not flip persisted enabled=false back to true."""
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=0))

    disk_config = sched.config.to_dict()
    disk_config["enabled"] = False
    _write_disk_config(reboot_scheduler.CONFIG_FILE, disk_config)

    # Simulate stale in-memory state before purge persistence.
    sched.config.enabled = True
    sched.status.server_running = True

    sent_commands = []

    async def _fake_send_command(command):
        sent_commands.append(command)
        return {"success": True}

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(reboot_scheduler.minecraft_server, "send_command", _fake_send_command)
    monkeypatch.setattr(reboot_scheduler.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(sched.execute_coreprotect_purge(manual=True))
    assert result["success"] is True

    with open(reboot_scheduler.CONFIG_FILE, "r") as f:
        persisted = json.load(f)

    assert persisted["enabled"] is False
    assert sent_commands == [
        f"co purge t:{sched.config.coreprotect_retention_days}d",
        f"co purge t:{sched.config.coreprotect_retention_days}d confirm",
    ]


def test_scheduler_start_enters_standby_if_lock_owned(monkeypatch, tmp_path):
    """When another process owns the lock, scheduler must not start monitor loop."""
    sched = _make_scheduler(monkeypatch, tmp_path)

    lock_file = reboot_scheduler.INSTANCE_LOCK_FILE
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(sched.start())

    assert sched._running is False
    assert sched._monitor_task is None
    assert sched.logs[-1].action == "scheduler_standby"
    assert "another process owns reboot scheduler lock" in sched.logs[-1].details.lower()


def test_scheduler_stop_releases_instance_lock(monkeypatch, tmp_path):
    """Stopping scheduler should release lock so a new instance can acquire it."""
    sched = _make_scheduler(monkeypatch, tmp_path)

    async def _noop_check_and_act():
        return None

    monkeypatch.setattr(sched, "_check_and_act", _noop_check_and_act)

    asyncio.run(sched.start())
    assert sched._instance_lock_handle is not None
    asyncio.run(sched.stop())
    assert sched._instance_lock_handle is None

    with open(reboot_scheduler.INSTANCE_LOCK_FILE, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_action_log_records_source_pid(monkeypatch, tmp_path):
    """Every scheduler action log should include source process PID."""
    sched = _make_scheduler(monkeypatch, tmp_path)
    sched._add_log("test_action", "info", "log pid check")
    assert sched.logs[-1].source_pid == os.getpid()


def test_schedule_maintenance_stop_persists(monkeypatch, tmp_path):
    sched = _make_scheduler(monkeypatch, tmp_path)
    target = datetime.now() + timedelta(minutes=15)

    result = sched.schedule_maintenance_stop(target.isoformat())

    assert result["success"] is True
    assert sched.config.maintenance_stop_scheduled_at == result["scheduled_at"]
    assert sched.logs[-1].action == "maintenance_scheduled"


def test_schedule_maintenance_stop_rejects_past_time(monkeypatch, tmp_path):
    sched = _make_scheduler(monkeypatch, tmp_path)
    target = datetime.now() - timedelta(minutes=1)

    result = sched.schedule_maintenance_stop(target.isoformat())

    assert result["success"] is False
    assert "future" in result["error"].lower()


def test_maintenance_schedule_blocks_auto_restart_trigger(monkeypatch, tmp_path):
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=0))
    _patch_commands(monkeypatch)

    sched.config.coreprotect_purge_enabled = False
    sched._server_start_time = datetime.now() - timedelta(hours=3)
    sched._empty_since = datetime.now() - timedelta(hours=2)
    sched.config.maintenance_stop_scheduled_at = (datetime.now() + timedelta(minutes=20)).isoformat()

    countdown_called = {"called": False}

    async def _fake_start_countdown(reason, details):
        countdown_called["called"] = True
        return True

    monkeypatch.setattr(sched, "_start_countdown", _fake_start_countdown)

    asyncio.run(sched._check_and_act())

    assert countdown_called["called"] is False
    assert "scheduled stop" in (sched.status.next_action or "").lower()
    assert sched.status.maintenance_scheduled_at is not None


def test_due_maintenance_schedule_stops_server_and_clears(monkeypatch, tmp_path):
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=2))
    _patch_commands(monkeypatch)
    sched.config.coreprotect_purge_enabled = False
    sched.config.maintenance_stop_scheduled_at = (datetime.now() - timedelta(seconds=3)).isoformat()

    stop_called = {"count": 0}

    async def _fake_stop_server(force=False):
        stop_called["count"] += 1
        return {"success": True, "message": "stopped"}

    monkeypatch.setattr(reboot_scheduler.minecraft_server, "stop_server", _fake_stop_server)

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(reboot_scheduler.asyncio, "sleep", _fake_sleep)

    asyncio.run(sched._check_and_act())

    assert stop_called["count"] == 1
    assert sched.config.maintenance_stop_scheduled_at is None
    assert any(log.action == "maintenance_stop_completed" for log in sched.logs)


def test_disabled_scheduler_still_honors_due_maintenance_stop(monkeypatch, tmp_path):
    sched = _make_scheduler(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _FakeServerStatus(players_online=1))
    _patch_commands(monkeypatch)
    sched.config.enabled = False
    sched.config.maintenance_stop_scheduled_at = (datetime.now() - timedelta(seconds=1)).isoformat()

    stop_called = {"count": 0}

    async def _fake_stop_server(force=False):
        stop_called["count"] += 1
        return {"success": True}

    monkeypatch.setattr(reboot_scheduler.minecraft_server, "stop_server", _fake_stop_server)

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(reboot_scheduler.asyncio, "sleep", _fake_sleep)

    asyncio.run(sched._check_and_act())

    assert stop_called["count"] == 1
    assert sched.config.maintenance_stop_scheduled_at is None
