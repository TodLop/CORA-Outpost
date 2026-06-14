from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app.core import auth as auth_core
from app.routers.staff import router as staff_router
from app.services import minecraft_server, minecraft_settings
from app.services import permissions as permissions_service


STAFF_EMAIL = "staff@example.com"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login/{email}")
    async def _login(email: str, request: Request):
        request.session["user_info"] = {"email": email, "name": "Test Staff"}
        return {"ok": True}

    app.include_router(staff_router)
    return app


def _seed_staff(monkeypatch, tmp_path):
    staff_set = frozenset({STAFF_EMAIL})
    monkeypatch.setattr(auth_core, "STAFF_EMAILS", staff_set)
    monkeypatch.setattr(auth_core, "STAFF_EMAILS_NORMALIZED", staff_set)
    monkeypatch.setattr(permissions_service, "RBAC_SETTINGS_FILE", tmp_path / "rbac_settings.json")
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "minecraft_settings.json")
    monkeypatch.setattr(minecraft_settings, "PROFILE_STATE_ROOT", tmp_path / "profile_state")
    permissions_service.grant_permission(STAFF_EMAIL, "status:view", "owner@example.com")


def _make_server_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (path / "server.properties").write_text("enable-rcon=false\n", encoding="utf-8")
    (path / "logs").mkdir()
    (path / "plugins").mkdir()
    return path


def test_staff_status_includes_sanitized_active_profile_metadata(monkeypatch, tmp_path):
    _seed_staff(monkeypatch, tmp_path)
    server_dir = _make_server_dir(tmp_path / "live-server")
    minecraft_settings.create_profile(
        profile_id="staff-live",
        name="Staff Live",
        kind="live",
        server_directory=server_dir,
        operations_enabled=False,
        rcon_enabled=False,
        readonly=True,
        set_active=True,
    )
    monkeypatch.setattr(
        minecraft_server,
        "get_server_status",
        lambda: SimpleNamespace(
            running=False,
            process_running=False,
            healthy=False,
            state_reason="stopped",
            pid=None,
            game_port_listening=False,
            rcon_port_listening=False,
            players_online=0,
            max_players=20,
            players=[],
        ),
    )

    client = TestClient(_make_app())
    client.get(f"/__test/login/{STAFF_EMAIL}")

    response = client.get("/minecraft/staff/api/minecraft/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["active_profile"] == {
        "id": "staff-live",
        "name": "Staff Live",
        "kind": "live",
        "operations_enabled": False,
        "rcon_enabled": False,
        "readonly": True,
    }
    assert "server_directory" not in payload["active_profile"]
