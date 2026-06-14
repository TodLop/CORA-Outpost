from pathlib import Path

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app.core.auth import ADMIN_EMAILS
from app.routers.admin_server import router as admin_server_router
from app.services import minecraft_server, minecraft_settings, whitelist_autocomplete_cache


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login")
    async def _login(request: Request):
        request.session["user_info"] = {"email": next(iter(ADMIN_EMAILS)), "name": "Admin"}
        return {"ok": True}

    app.include_router(admin_server_router, prefix="/minecraft/admin")
    return app


def _client() -> TestClient:
    client = TestClient(_make_app())
    client.get("/__test/login")
    return client


def _make_server_dir(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "server.properties").write_text("enable-rcon=false\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "plugins").mkdir()
    return root


def _use_profile_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(minecraft_settings, "PROFILE_STATE_ROOT", tmp_path / "profile_state")


def _create_live_profile(
    monkeypatch,
    tmp_path,
    *,
    profile_id: str = "live",
    name: str = "Live Server",
    set_active: bool = True,
) -> Path:
    _use_profile_settings(monkeypatch, tmp_path)
    server_dir = _make_server_dir(tmp_path / profile_id)
    minecraft_settings.create_profile(
        profile_id=profile_id,
        name=name,
        server_directory=server_dir,
        operations_enabled=True,
        rcon_enabled=True,
        readonly=False,
        set_active=set_active,
    )
    return server_dir


def test_get_server_profiles_returns_sample_and_active_profile_id(monkeypatch, tmp_path):
    _use_profile_settings(monkeypatch, tmp_path)

    response = _client().get("/minecraft/admin/api/minecraft/server-profiles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["active_profile_id"] == "sample"
    assert [profile["id"] for profile in payload["profiles"]] == ["sample"]


def test_create_live_profile_succeeds_and_persists(monkeypatch, tmp_path):
    _use_profile_settings(monkeypatch, tmp_path)
    server_dir = _make_server_dir(tmp_path / "live_server")

    response = _client().post(
        "/minecraft/admin/api/minecraft/server-profiles",
        json={
            "id": "live-one",
            "name": "Live One",
            "server_directory": str(server_dir),
            "operations_enabled": True,
            "rcon_enabled": False,
        },
    )

    assert response.status_code == 201
    profile = response.json()["profile"]
    assert profile["id"] == "live-one"
    assert profile["name"] == "Live One"
    assert profile["server_directory"] == str(server_dir.resolve())
    assert profile["operations_enabled"] is True
    assert profile["rcon_enabled"] is False
    assert minecraft_settings.get_profile("live-one")["server_directory"] == str(server_dir.resolve())
    assert minecraft_settings.get_settings()["active_profile_id"] == "sample"


def test_update_profile_preserves_unrelated_profile_settings(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", name="Server A", set_active=True)
    server_b = _make_server_dir(tmp_path / "server-b")
    minecraft_settings.create_profile(
        profile_id="server-b",
        name="Server B",
        server_directory=server_b,
        operations_enabled=False,
        rcon_enabled=False,
        readonly=False,
    )

    response = _client().put(
        "/minecraft/admin/api/minecraft/server-profiles/server-b",
        json={"name": "Server Bee", "operations_enabled": True},
    )

    assert response.status_code == 200
    updated = response.json()["profile"]
    assert updated["name"] == "Server Bee"
    assert updated["operations_enabled"] is True
    server_a = minecraft_settings.get_profile("server-a")
    assert server_a["name"] == "Server A"
    assert server_a["operations_enabled"] is True
    assert minecraft_settings.get_settings()["active_profile_id"] == "server-a"


def test_activate_profile_changes_only_active_pointer(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", set_active=True)
    server_b = _make_server_dir(tmp_path / "server-b")
    minecraft_settings.create_profile(
        profile_id="server-b",
        name="Server B",
        server_directory=server_b,
        set_active=False,
    )
    before_profiles = {
        profile["id"]: profile
        for profile in minecraft_settings.get_settings()["profiles"]
    }
    whitelist_autocomplete_cache.store_players(["OldPlayer"], now=1.0)

    response = _client().post("/minecraft/admin/api/minecraft/server-profiles/server-b/activate")

    assert response.status_code == 200
    assert response.json()["active_profile_id"] == "server-b"
    after_profiles = {
        profile["id"]: profile
        for profile in minecraft_settings.get_settings()["profiles"]
    }
    assert after_profiles == before_profiles
    assert whitelist_autocomplete_cache.get_stale_players() == []


def test_activate_profile_does_not_call_lifecycle_backup_update_or_rcon(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", set_active=True)
    server_b = _make_server_dir(tmp_path / "server-b")
    minecraft_settings.create_profile(
        profile_id="server-b",
        name="Server B",
        server_directory=server_b,
        set_active=False,
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("activation must not run server lifecycle or RCON work")

    monkeypatch.setattr(minecraft_server, "start_server", fail_if_called)
    monkeypatch.setattr(minecraft_server, "stop_server", fail_if_called)
    monkeypatch.setattr(minecraft_server, "restart_server", fail_if_called)
    monkeypatch.setattr(minecraft_server, "recover_server", fail_if_called)
    monkeypatch.setattr(minecraft_server, "send_command", fail_if_called)
    monkeypatch.setattr(minecraft_server, "is_server_running", fail_if_called)

    response = _client().post("/minecraft/admin/api/minecraft/server-profiles/server-b/activate")

    assert response.status_code == 200
    assert response.json()["active_profile_id"] == "server-b"


def test_activate_sample_does_not_delete_another_profile_pid(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", set_active=True)
    state_dir = minecraft_settings.get_profile_state_dir("server-a")
    state_dir.mkdir(parents=True)
    pid_file = state_dir / "server.pid"
    pid_file.write_text("12345", encoding="utf-8")

    response = _client().post("/minecraft/admin/api/minecraft/server-profiles/sample/activate")

    assert response.status_code == 200
    assert response.json()["active_profile_id"] == "sample"
    assert pid_file.read_text(encoding="utf-8") == "12345"


def test_delete_active_profile_is_blocked(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", set_active=True)

    response = _client().delete("/minecraft/admin/api/minecraft/server-profiles/server-a")

    assert response.status_code == 409
    assert response.json()["error_code"] == "profile_delete_active"
    assert minecraft_settings.get_profile("server-a")["id"] == "server-a"


def test_delete_sample_profile_is_blocked(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", set_active=True)

    response = _client().delete("/minecraft/admin/api/minecraft/server-profiles/sample")

    assert response.status_code == 409
    assert response.json()["error_code"] == "profile_delete_sample"
    assert minecraft_settings.get_profile("sample")["id"] == "sample"


def test_delete_profile_with_pid_file_is_blocked(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", set_active=True)
    server_b = _make_server_dir(tmp_path / "server-b")
    minecraft_settings.create_profile(
        profile_id="server-b",
        name="Server B",
        server_directory=server_b,
    )
    state_dir = minecraft_settings.get_profile_state_dir("server-b")
    state_dir.mkdir(parents=True)
    (state_dir / "server.pid").write_text("12345", encoding="utf-8")

    response = _client().delete("/minecraft/admin/api/minecraft/server-profiles/server-b")

    assert response.status_code == 409
    assert response.json()["error_code"] == "profile_delete_has_pid"
    assert minecraft_settings.get_profile("server-b")["id"] == "server-b"


def test_server_directory_get_remains_backward_compatible(monkeypatch, tmp_path):
    server_dir = _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", set_active=True)

    response = _client().get("/minecraft/admin/api/minecraft/server-directory")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["settings"]["server_directory"] == str(server_dir.resolve())
    assert payload["settings"]["active_profile_id"] == "server-a"


def test_server_directory_put_remains_backward_compatible(monkeypatch, tmp_path):
    _create_live_profile(monkeypatch, tmp_path, profile_id="server-a", set_active=True)
    new_server = _make_server_dir(tmp_path / "new-server")
    monkeypatch.setattr(minecraft_server, "is_server_running", lambda: False)

    response = _client().put(
        "/minecraft/admin/api/minecraft/server-directory",
        json={"server_directory": str(new_server)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["settings"]["server_directory"] == str(new_server.resolve())
    assert payload["settings"]["active_profile_id"] == "server-a"
