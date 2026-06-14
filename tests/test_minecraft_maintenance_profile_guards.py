from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.services import backup_scheduler, minecraft_settings, minecraft_updater, reboot_scheduler
from app.services.minecraft_updater import UpdateCheck


def _use_temp_profile_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_settings, "PROFILE_STATE_ROOT", tmp_path / "profile_state")


def _make_live_profile(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    server_dir = tmp_path / "live_server"
    server_dir.mkdir()
    (server_dir / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (server_dir / "server.properties").write_text("enable-rcon=true\nrcon.password=test\n", encoding="utf-8")
    (server_dir / "plugins").mkdir()
    minecraft_settings.create_profile(
        profile_id="live",
        name="Live Server",
        server_directory=server_dir,
        operations_enabled=True,
        rcon_enabled=True,
        readonly=False,
        set_active=True,
    )
    return server_dir


def _update_check() -> UpdateCheck:
    return UpdateCheck(
        plugin_id="viaversion",
        source="modrinth",
        current_version="5.9.0",
        latest_version="5.9.1",
        has_update=True,
        download_url="https://example.com/ViaVersion-5.9.1.jar",
        filename="ViaVersion-5.9.1.jar",
    )


def _isolate_updater_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_updater, "_update_logs_path", lambda: tmp_path / "update_logs")


@pytest.mark.asyncio
async def test_apply_update_blocks_sample_before_update_work(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    _isolate_updater_logs(monkeypatch, tmp_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("update work should not start for a blocked profile")

    monkeypatch.setattr(minecraft_updater, "load_versions", fail_if_called)
    monkeypatch.setattr(minecraft_updater, "backup_plugin", fail_if_called)
    monkeypatch.setattr(minecraft_updater, "download_update", fail_if_called)

    log = await minecraft_updater.apply_update("viaversion", _update_check())

    assert log.status == "failed"
    assert log.error
    assert log.steps[-1]["action"] == "profile_guard_blocked"
    assert log.steps[-1]["error_code"] == "profile_readonly"
    assert log.steps[-1]["profile_id"] == "sample"


@pytest.mark.asyncio
async def test_apply_update_from_local_file_blocks_sample_before_file_checks(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    _isolate_updater_logs(monkeypatch, tmp_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("local update file checks should not run for a blocked profile")

    monkeypatch.setattr(minecraft_updater, "_safe_plugin_jar_name", fail_if_called)
    monkeypatch.setattr(minecraft_updater, "load_versions", fail_if_called)

    log = await minecraft_updater.apply_update_from_local_file(
        "viaversion",
        _update_check(),
        Path("/missing/ViaVersion-5.9.1.jar"),
    )

    assert log.status == "failed"
    assert log.steps[-1]["error_code"] == "profile_readonly"


@pytest.mark.asyncio
async def test_execute_upgrade_manifest_blocks_sample_before_manifest_load(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("upgrade manifest work should not start for a blocked profile")

    monkeypatch.setattr(minecraft_updater, "load_upgrade_manifest", fail_if_called)
    monkeypatch.setattr(minecraft_updater, "load_versions", fail_if_called)

    result = await minecraft_updater.execute_upgrade_manifest("upgrade-1", actor="admin@example.com")

    assert result["success"] is False
    assert result["status"] == "rejected"
    assert result["error_code"] == "profile_readonly"
    assert result["profile_id"] == "sample"


@pytest.mark.asyncio
async def test_manual_backup_blocks_sample_before_scheduler_work(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(backup_scheduler, "CONFIG_FILE", tmp_path / "backup_config.json")
    monkeypatch.setattr(backup_scheduler, "LOG_FILE", tmp_path / "backup_log.json")
    scheduler = backup_scheduler.BackupScheduler()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("backup work should not start for a blocked profile")

    monkeypatch.setattr(scheduler, "_add_log", fail_if_called)
    monkeypatch.setattr(scheduler, "_execute_backup", fail_if_called)
    monkeypatch.setattr(backup_scheduler.minecraft_server, "get_server_status", fail_if_called)

    result = await scheduler.trigger_manual_backup()

    assert result["success"] is False
    assert result["error_code"] == "profile_readonly"
    assert result["profile_id"] == "sample"


def test_schedule_maintenance_stop_blocks_sample_before_config_write(monkeypatch, tmp_path):
    _use_temp_profile_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(reboot_scheduler, "CONFIG_FILE", tmp_path / "reboot_config.json")
    monkeypatch.setattr(reboot_scheduler, "LOG_FILE", tmp_path / "reboot_log.json")
    monkeypatch.setattr(reboot_scheduler, "INSTANCE_LOCK_FILE", tmp_path / "reboot.lock")
    scheduler = reboot_scheduler.RebootScheduler()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("maintenance scheduling state should not be touched for a blocked profile")

    monkeypatch.setattr(scheduler, "_load_config", fail_if_called)
    monkeypatch.setattr(scheduler, "_save_config", fail_if_called)
    monkeypatch.setattr(scheduler, "_add_log", fail_if_called)

    result = scheduler.schedule_maintenance_stop((datetime.now() + timedelta(minutes=15)).isoformat())

    assert result["success"] is False
    assert result["error_code"] == "profile_readonly"
    assert scheduler.config.maintenance_stop_scheduled_at is None


@pytest.mark.asyncio
async def test_live_profile_allows_manual_backup_trigger_path(monkeypatch, tmp_path):
    _make_live_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(backup_scheduler, "CONFIG_FILE", tmp_path / "backup_config.json")
    monkeypatch.setattr(backup_scheduler, "LOG_FILE", tmp_path / "backup_log.json")
    service_account = tmp_path / "service_account.json"
    service_account.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(backup_scheduler, "SERVICE_ACCOUNT_FILE", service_account)
    scheduler = backup_scheduler.BackupScheduler()
    scheduler.config.drive_folder_id = "drive-folder"

    class FakeStatus:
        running = False
        players_online = 0

    scheduled = []

    async def fake_execute_backup(*, server_was_running):
        return None

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return object()

    monkeypatch.setattr(backup_scheduler.minecraft_server, "get_server_status", lambda: FakeStatus())
    monkeypatch.setattr(scheduler, "_execute_backup", fake_execute_backup)
    monkeypatch.setattr(backup_scheduler.asyncio, "create_task", fake_create_task)

    result = await scheduler.trigger_manual_backup()

    assert result["success"] is True
    assert scheduled


def test_live_profile_allows_maintenance_stop_schedule(monkeypatch, tmp_path):
    _make_live_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(reboot_scheduler, "CONFIG_FILE", tmp_path / "reboot_config.json")
    monkeypatch.setattr(reboot_scheduler, "LOG_FILE", tmp_path / "reboot_log.json")
    monkeypatch.setattr(reboot_scheduler, "INSTANCE_LOCK_FILE", tmp_path / "reboot.lock")
    scheduler = reboot_scheduler.RebootScheduler()

    target = datetime.now() + timedelta(minutes=15)
    result = scheduler.schedule_maintenance_stop(target.isoformat())

    assert result["success"] is True
    assert scheduler.config.maintenance_stop_scheduled_at == result["scheduled_at"]
