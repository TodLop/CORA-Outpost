import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app.core import config as core_config
from app.core.auth import ADMIN_EMAILS
from app.routers.admin_server import router as admin_server_router
from app.services import minecraft_server, minecraft_settings


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login")
    async def _login(request: Request):
        request.session["user_info"] = {"email": next(iter(ADMIN_EMAILS)), "name": "Admin"}
        return {"ok": True}

    app.include_router(admin_server_router, prefix="/minecraft/admin")
    return app


def _make_server_dir(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "server.properties").write_text("enable-rcon=false\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "plugins").mkdir()
    return root


def _setup_two_profiles(monkeypatch, tmp_path):
    server_a = _make_server_dir(tmp_path / "server_a")
    server_b = _make_server_dir(tmp_path / "server_b")
    state_root = tmp_path / "profile_state"
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_settings, "PROFILE_STATE_ROOT", state_root)
    minecraft_settings.create_profile(
        profile_id="server-a",
        name="Server A",
        server_directory=server_a,
        set_active=True,
    )
    minecraft_settings.create_profile(
        profile_id="server-b",
        name="Server B",
        server_directory=server_b,
    )
    return server_a, server_b, state_root


def _use_temp_profile_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        minecraft_server.minecraft_settings,
        "get_active_profile_state_dir",
        lambda: tmp_path / "profile_state" / "active",
    )


def test_settings_default_uses_sample_profile(monkeypatch, tmp_path):
    default_server = _make_server_dir(tmp_path / "minecraft_server_paper")
    monkeypatch.setattr(core_config, "MINECRAFT_SERVER_PATH", default_server)
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")

    settings = minecraft_settings.get_settings()

    sample_dir = (core_config.ROOT_DIR / "examples/minecraft_sample_server").resolve()
    assert settings["server_directory"] == str(sample_dir)
    assert settings["default_server_directory"] == str(default_server.resolve())
    assert settings["active_profile_id"] == "sample"
    assert settings["active_profile"]["name"] == "Sample Server"
    assert settings["active_profile"]["readonly"] is True
    assert settings["active_profile"]["operations_enabled"] is False
    assert settings["active_profile"]["rcon_enabled"] is False


def test_get_server_directory_returns_active_profile_directory(monkeypatch, tmp_path):
    server_a = _make_server_dir(tmp_path / "server_a")
    server_b = _make_server_dir(tmp_path / "server_b")
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", settings_file)

    minecraft_settings.create_profile(
        profile_id="server-a",
        name="Server A",
        server_directory=server_a,
        set_active=True,
    )
    minecraft_settings.create_profile(
        profile_id="server-b",
        name="Server B",
        server_directory=server_b,
    )

    assert minecraft_settings.get_server_directory() == server_a.resolve()

    minecraft_settings.set_active_profile("server-b")

    assert minecraft_settings.get_server_directory() == server_b.resolve()


def test_legacy_server_directory_settings_migrate_to_profiles(monkeypatch, tmp_path):
    legacy_server = _make_server_dir(tmp_path / "legacy_server")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "server_directory": str(legacy_server),
            "updated_at": "2026-06-11T10:00:00",
            "updated_by": "admin@example.com",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", settings_file)

    settings = minecraft_settings.get_settings()
    migrated_payload = json.loads(settings_file.read_text(encoding="utf-8"))

    assert settings["active_profile_id"] == "main"
    assert settings["server_directory"] == str(legacy_server.resolve())
    assert "server_directory" not in migrated_payload
    assert migrated_payload["active_profile_id"] == "main"
    assert {profile["id"] for profile in migrated_payload["profiles"]} == {"sample", "main"}
    main_profile = minecraft_settings.get_profile("main")
    assert main_profile["kind"] == "live"
    assert main_profile["operations_enabled"] is True
    assert main_profile["readonly"] is False


def test_set_active_profile_changes_only_active_pointer(monkeypatch, tmp_path):
    server_a = _make_server_dir(tmp_path / "server_a")
    server_b = _make_server_dir(tmp_path / "server_b")
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", settings_file)

    minecraft_settings.create_profile(
        profile_id="server-a",
        name="Server A",
        server_directory=server_a,
        set_active=True,
    )
    minecraft_settings.create_profile(
        profile_id="server-b",
        name="Server B",
        server_directory=server_b,
    )
    before = json.loads(settings_file.read_text(encoding="utf-8"))

    settings = minecraft_settings.set_active_profile("server-b")
    after = json.loads(settings_file.read_text(encoding="utf-8"))

    assert settings["active_profile_id"] == "server-b"
    assert after["active_profile_id"] == "server-b"
    assert before["active_profile_id"] == "server-a"
    assert after["profiles"] == before["profiles"]
    assert not (server_a / "server.pid").exists()
    assert not (server_b / "server.pid").exists()


def test_sample_profile_is_readonly_and_operations_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")

    sample = minecraft_settings.get_profile("sample")

    assert sample["kind"] == "sample"
    assert sample["readonly"] is True
    assert sample["operations_enabled"] is False
    assert sample["rcon_enabled"] is False


def test_profile_state_directory_is_profile_scoped(monkeypatch, tmp_path):
    server_a = _make_server_dir(tmp_path / "server_a")
    server_b = _make_server_dir(tmp_path / "server_b")
    state_root = tmp_path / "profile_state"
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_settings, "PROFILE_STATE_ROOT", state_root)

    minecraft_settings.create_profile(
        profile_id="server-a",
        name="Server A",
        server_directory=server_a,
        set_active=True,
    )
    minecraft_settings.create_profile(
        profile_id="server-b",
        name="Server B",
        server_directory=server_b,
    )

    assert minecraft_settings.get_profile_state_dir("server-a") == state_root / "server-a"
    assert minecraft_settings.get_profile_state_dir("server-b") == state_root / "server-b"
    assert minecraft_settings.get_active_profile_state_dir() == state_root / "server-a"


def test_active_profile_pid_path_uses_profile_state_dir(monkeypatch, tmp_path):
    _server_a, _server_b, state_root = _setup_two_profiles(monkeypatch, tmp_path)

    expected = state_root / "server-a" / "server.pid"
    assert minecraft_server.get_pid_file() == expected
    assert minecraft_settings.get_pid_file() == expected


def test_two_profiles_have_distinct_pid_paths(monkeypatch, tmp_path):
    _server_a, _server_b, state_root = _setup_two_profiles(monkeypatch, tmp_path)

    path_a = minecraft_server.get_pid_file()
    minecraft_settings.set_active_profile("server-b")
    path_b = minecraft_server.get_pid_file()

    assert path_a == state_root / "server-a" / "server.pid"
    assert path_b == state_root / "server-b" / "server.pid"
    assert path_a != path_b


def test_switching_active_profile_changes_runtime_state_paths(monkeypatch, tmp_path):
    _server_a, _server_b, state_root = _setup_two_profiles(monkeypatch, tmp_path)

    paths_a = {
        minecraft_server.get_pid_file(),
        minecraft_server.get_restart_state_file(),
        minecraft_server.get_restart_lock_file(),
    }

    minecraft_settings.set_active_profile("server-b")
    paths_b = {
        minecraft_server.get_pid_file(),
        minecraft_server.get_restart_state_file(),
        minecraft_server.get_restart_lock_file(),
    }

    assert paths_a == {
        state_root / "server-a" / "server.pid",
        state_root / "server-a" / "minecraft_restart_state.json",
        state_root / "server-a" / "minecraft_restart.lock",
    }
    assert paths_b == {
        state_root / "server-b" / "server.pid",
        state_root / "server-b" / "minecraft_restart_state.json",
        state_root / "server-b" / "minecraft_restart.lock",
    }
    assert paths_a.isdisjoint(paths_b)


def test_invalid_profile_activation_fails_safely(monkeypatch, tmp_path):
    server_a = _make_server_dir(tmp_path / "server_a")
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    minecraft_settings.create_profile(
        profile_id="server-a",
        name="Server A",
        server_directory=server_a,
        set_active=True,
    )

    with pytest.raises(KeyError):
        minecraft_settings.set_active_profile("missing")

    assert minecraft_settings.get_settings()["active_profile_id"] == "server-a"


def test_delete_active_profile_is_blocked(monkeypatch, tmp_path):
    server_a = _make_server_dir(tmp_path / "server_a")
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    minecraft_settings.create_profile(
        profile_id="server-a",
        name="Server A",
        server_directory=server_a,
        set_active=True,
    )

    with pytest.raises(minecraft_settings.ProfileValidationError):
        minecraft_settings.delete_profile("server-a")

    assert minecraft_settings.get_profile("server-a")["id"] == "server-a"


def test_admin_can_save_valid_server_directory(monkeypatch, tmp_path):
    new_server = _make_server_dir(tmp_path / "new_server")
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_server, "is_server_running", lambda: False)

    client = TestClient(_make_app())
    client.get("/__test/login")

    response = client.put(
        "/minecraft/admin/api/minecraft/server-directory",
        json={"server_directory": str(new_server)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["settings"]["server_directory"] == str(new_server.resolve())
    assert payload["changed"] is True


def test_admin_server_directory_rejects_invalid_path(monkeypatch, tmp_path):
    invalid_server = tmp_path / "not_a_server"
    invalid_server.mkdir()
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_server, "is_server_running", lambda: False)

    client = TestClient(_make_app())
    client.get("/__test/login")

    response = client.put(
        "/minecraft/admin/api/minecraft/server-directory",
        json={"server_directory": str(invalid_server)},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["status"] == "error"
    assert "start_script" in payload["errors"]
    assert "server_properties" in payload["errors"]


def test_admin_server_directory_rejects_non_writable_path(monkeypatch, tmp_path):
    server_dir = _make_server_dir(tmp_path / "readonly_server")
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_server, "is_server_running", lambda: False)

    real_access = os.access

    def fake_access(path, mode):
        if Path(path) == server_dir.resolve() and mode == os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(minecraft_settings.os, "access", fake_access)

    client = TestClient(_make_app())
    client.get("/__test/login")

    response = client.put(
        "/minecraft/admin/api/minecraft/server-directory",
        json={"server_directory": str(server_dir)},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["errors"]["writable"] == "Directory must be writable."


def test_admin_server_directory_requires_stopped_server(monkeypatch, tmp_path):
    current_server = _make_server_dir(tmp_path / "current")
    new_server = _make_server_dir(tmp_path / "new")
    monkeypatch.setattr(core_config, "MINECRAFT_SERVER_PATH", current_server)
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_server, "is_server_running", lambda: True)

    client = TestClient(_make_app())
    client.get("/__test/login")

    response = client.put(
        "/minecraft/admin/api/minecraft/server-directory",
        json={"server_directory": str(new_server)},
    )

    assert response.status_code == 409
    assert not (tmp_path / "settings.json").exists()


def test_process_snapshot_ignores_pid_from_other_server_directory(monkeypatch, tmp_path):
    current_server, event_server, _state_root = _setup_two_profiles(monkeypatch, tmp_path)
    active_pid_file = minecraft_server.get_pid_file()
    active_pid_file.parent.mkdir(parents=True)
    active_pid_file.write_text("111", encoding="utf-8")

    manager = minecraft_server.ServerManager()
    monkeypatch.setattr(manager, "_is_minecraft_process", lambda pid: True)
    monkeypatch.setattr(manager, "_get_process_cwd", lambda pid: event_server)
    monkeypatch.setattr(manager, "_find_minecraft_pid", lambda: None)

    running, pid, stale = manager._get_process_snapshot_sync()

    assert running is False
    assert pid is None
    assert stale is True
    assert not active_pid_file.exists()


def test_stale_pid_cleanup_only_deletes_active_profile_pid(monkeypatch, tmp_path):
    server_a, server_b, state_root = _setup_two_profiles(monkeypatch, tmp_path)
    pid_a = state_root / "server-a" / "server.pid"
    pid_b = state_root / "server-b" / "server.pid"
    pid_a.parent.mkdir(parents=True)
    pid_b.parent.mkdir(parents=True)
    pid_a.write_text("111", encoding="utf-8")
    pid_b.write_text("222", encoding="utf-8")
    minecraft_settings.set_active_profile("server-b")

    manager = minecraft_server.ServerManager()
    monkeypatch.setattr(manager, "_is_minecraft_process", lambda pid: True)
    monkeypatch.setattr(
        manager,
        "_get_process_cwd",
        lambda pid: server_a if pid == 222 else server_b,
    )
    monkeypatch.setattr(manager, "_find_minecraft_pid", lambda: None)

    running, pid, stale = manager._get_process_snapshot_sync()

    assert running is False
    assert pid is None
    assert stale is True
    assert pid_a.exists()
    assert not pid_b.exists()


def test_switching_to_sample_profile_does_not_delete_live_profile_pid(monkeypatch, tmp_path):
    _server_a, _server_b, state_root = _setup_two_profiles(monkeypatch, tmp_path)
    pid_a = state_root / "server-a" / "server.pid"
    pid_a.parent.mkdir(parents=True)
    pid_a.write_text("111", encoding="utf-8")
    minecraft_settings.set_active_profile("sample")

    manager = minecraft_server.ServerManager()
    monkeypatch.setattr(manager, "_find_minecraft_pid", lambda: None)

    running, pid, stale = manager._get_process_snapshot_sync()

    assert running is False
    assert pid is None
    assert stale is False
    assert pid_a.exists()


def test_pgrep_fallback_selects_only_configured_server_directory(monkeypatch, tmp_path):
    current_server = _make_server_dir(tmp_path / "current")
    event_server = _make_server_dir(tmp_path / "event")
    _use_temp_profile_state(monkeypatch, tmp_path)

    class FakeResult:
        returncode = 0
        stdout = "111\n222\n"

    manager = minecraft_server.ServerManager()
    monkeypatch.setattr(minecraft_server.minecraft_settings, "get_server_directory", lambda: current_server)
    monkeypatch.setattr(minecraft_server.subprocess, "run", lambda *args, **kwargs: FakeResult())
    monkeypatch.setattr(manager, "_is_minecraft_process", lambda pid: True)
    monkeypatch.setattr(
        manager,
        "_get_process_cwd",
        lambda pid: event_server if pid == 111 else current_server,
    )

    assert manager._find_minecraft_pid() == 222


def test_process_directory_match_handles_symlink(monkeypatch, tmp_path):
    _use_temp_profile_state(monkeypatch, tmp_path)
    manager = minecraft_server.ServerManager()
    target = _make_server_dir(tmp_path / "target")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    assert manager._same_directory(link, target) is True


def test_status_uses_server_port_from_configured_directory(monkeypatch, tmp_path):
    server_dir = _make_server_dir(tmp_path / "managed_server")
    _use_temp_profile_state(monkeypatch, tmp_path)
    (server_dir / "server.properties").write_text(
        "server-port=25566\n"
        "enable-rcon=true\n"
        "rcon.port=25576\n"
        "rcon.password=test\n",
        encoding="utf-8",
    )
    checked_ports = []

    manager = minecraft_server.ServerManager()
    monkeypatch.setattr(minecraft_server.minecraft_settings, "get_server_directory", lambda: server_dir)
    monkeypatch.setattr(manager, "_get_process_snapshot_sync", lambda: (True, 222, False))
    monkeypatch.setattr(manager, "_is_port_listening", lambda port, host="127.0.0.1": checked_ports.append(port) or False)

    status = manager.get_server_status()

    assert status.healthy is False
    assert checked_ports[:2] == [25566, 25576]


@pytest.mark.asyncio
async def test_start_server_uses_configured_directory(monkeypatch, tmp_path):
    server_dir = _make_server_dir(tmp_path / "managed_server")
    _use_temp_profile_state(monkeypatch, tmp_path)
    recorded = {}

    class FakeProcess:
        pid = 12345

    def fake_popen(args, **kwargs):
        recorded["args"] = args
        recorded["cwd"] = kwargs.get("cwd")
        return FakeProcess()

    manager = minecraft_server.ServerManager()
    monkeypatch.setattr(manager, "_is_server_running_sync", lambda: False)
    monkeypatch.setattr(manager, "_tail_log_file", lambda: asyncio.sleep(0))
    monkeypatch.setattr(minecraft_server.minecraft_settings, "get_server_directory", lambda: server_dir)
    monkeypatch.setattr(
        minecraft_server.minecraft_settings,
        "get_active_profile_operation_block",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(minecraft_server.subprocess, "Popen", fake_popen)

    result = await manager.start_server()
    if manager.log_reader_task is not None:
        await manager.log_reader_task

    assert result["success"] is True
    assert recorded["cwd"] == str(server_dir)
    assert recorded["args"] == ["sh", str(server_dir / "start.sh")]
