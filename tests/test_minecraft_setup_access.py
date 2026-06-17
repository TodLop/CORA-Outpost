import json

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app.core import auth as core_auth
from app.routers import admin as admin_page
from app.routers.admin import router as admin_router
from app.services import minecraft_admin_tiers as tiers


SETUP_EXECUTE_ENDPOINT = "/minecraft/admin/api/minecraft/setup/create-server/execute"
SETUP_CHOOSE_FOLDER_ENDPOINT = "/minecraft/admin/api/minecraft/setup/choose-folder"


def _write_tier_state(path, *, email: str, active: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "manager_admins": {
            email: {
                "email": email,
                "active": active,
                "promoted_at": "2026-02-18T00:00:00",
                "promoted_by": "owner@example.com",
                "snapshot": {
                    "role": "viewer",
                    "grants": [],
                    "revokes": [],
                    "hidden_features": [],
                },
                "restored_after_demotion": not active,
                "demoted_at": None,
                "demoted_by": None,
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login/{email}")
    async def _login(email: str, request: Request):
        request.session["user_info"] = {"email": email, "name": "Test"}
        return {"ok": True}

    app.include_router(admin_router)
    return app


def test_setup_workspace_renders_without_live_server_reads(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("setup workspace must not read live server state")

    monkeypatch.setattr(admin_page.minecraft_settings, "get_settings", _forbidden, raising=False)
    monkeypatch.setattr(admin_page.minecraft_settings, "get_active_profile", _forbidden, raising=False)
    monkeypatch.setattr(admin_page.minecraft_settings, "get_server_directory", _forbidden, raising=False)
    monkeypatch.setattr(admin_page.minecraft_settings, "get_server_icon_path", _forbidden, raising=False)
    monkeypatch.setattr(admin_page.minecraft_server, "get_server_status", _forbidden)
    monkeypatch.setattr(admin_page.minecraft_updater, "load_versions", _forbidden)
    monkeypatch.setattr(admin_page.minecraft_updater, "get_server_status", _forbidden)
    monkeypatch.setattr(admin_page.minecraft_updater, "get_update_logs", _forbidden)

    client = TestClient(_make_app())
    client.get(f"/__test/login/{core_auth.OWNER_EMAIL}")

    resp = client.get("/minecraft/admin/setup")

    assert resp.status_code == 200
    assert "Server Setup Workspace" in resp.text
    assert 'href="/minecraft/admin"' in resp.text
    assert "canExecuteSetupCreate: true" in resp.text
    assert SETUP_EXECUTE_ENDPOINT in resp.text


def test_setup_workspace_requires_minecraft_admin():
    client = TestClient(_make_app())

    anonymous = client.get("/minecraft/admin/setup")
    assert anonymous.status_code == 401

    client.get("/__test/login/random-user@example.com")
    non_admin = client.get("/minecraft/admin/setup")
    assert non_admin.status_code == 403


def test_setup_workspace_exposes_create_execution_for_owner():
    client = TestClient(_make_app())
    client.get(f"/__test/login/{core_auth.OWNER_EMAIL}")

    resp = client.get("/minecraft/admin/setup")

    assert resp.status_code == 200
    assert "canExecuteSetupCreate: true" in resp.text
    assert SETUP_EXECUTE_ENDPOINT in resp.text
    assert SETUP_CHOOSE_FOLDER_ENDPOINT in resp.text
    assert '@click="executeCreateServer()"' in resp.text


def test_setup_workspace_exposes_create_execution_for_current_manager_admin(monkeypatch, tmp_path):
    manager_email = "manager@example.com"
    tier_file = tmp_path / "minecraft_admin_tiers.json"
    _write_tier_state(tier_file, email=manager_email, active=True)

    monkeypatch.setattr(tiers, "TIER_STATE_FILE", tier_file)

    client = TestClient(_make_app())
    client.get(f"/__test/login/{manager_email}")

    resp = client.get("/minecraft/admin/setup")

    assert resp.status_code == 200
    assert "canExecuteSetupCreate: true" in resp.text
    assert SETUP_EXECUTE_ENDPOINT in resp.text
    assert SETUP_CHOOSE_FOLDER_ENDPOINT in resp.text
    assert '@click="executeCreateServer()"' in resp.text


def test_setup_workspace_hides_create_execution_from_legacy_global_admin(monkeypatch, tmp_path):
    legacy_email = "legacy-admin@example.com"
    tier_file = tmp_path / "minecraft_admin_tiers.json"
    tier_file.write_text(json.dumps({"version": 2, "manager_admins": {}}), encoding="utf-8")

    monkeypatch.setattr(tiers, "TIER_STATE_FILE", tier_file)
    monkeypatch.setattr(core_auth, "ADMIN_EMAILS", frozenset({core_auth.OWNER_EMAIL, legacy_email}))

    client = TestClient(_make_app())
    client.get(f"/__test/login/{legacy_email}")

    resp = client.get("/minecraft/admin/setup")

    assert resp.status_code == 200
    assert "canExecuteSetupCreate: false" in resp.text
    assert "createExecuteEndpoint: null" in resp.text
    assert "folderPickerEndpoint: null" in resp.text
    assert SETUP_EXECUTE_ENDPOINT not in resp.text
    assert SETUP_CHOOSE_FOLDER_ENDPOINT not in resp.text
    assert "Owner or manager-admin authorization is required to execute server creation." in resp.text
    assert '@click="executeCreateServer()"' not in resp.text
    assert "Choose Folder" not in resp.text
    assert "Manual path fallback" in resp.text


def test_setup_execute_endpoint_rejects_legacy_global_admin(monkeypatch, tmp_path):
    legacy_email = "legacy-admin@example.com"
    tier_file = tmp_path / "minecraft_admin_tiers.json"
    tier_file.write_text(json.dumps({"version": 2, "manager_admins": {}}), encoding="utf-8")

    monkeypatch.setattr(tiers, "TIER_STATE_FILE", tier_file)
    monkeypatch.setattr(core_auth, "ADMIN_EMAILS", frozenset({core_auth.OWNER_EMAIL, legacy_email}))

    client = TestClient(_make_app())
    client.get(f"/__test/login/{legacy_email}")

    resp = client.post(
        SETUP_EXECUTE_ENDPOINT,
        json={},
        headers={"X-CORA-Setup-Intent": "create-server"},
    )

    assert resp.status_code == 403


def test_setup_choose_folder_endpoint_rejects_legacy_global_admin(monkeypatch, tmp_path):
    legacy_email = "legacy-admin@example.com"
    tier_file = tmp_path / "minecraft_admin_tiers.json"
    tier_file.write_text(json.dumps({"version": 2, "manager_admins": {}}), encoding="utf-8")

    monkeypatch.setattr(tiers, "TIER_STATE_FILE", tier_file)
    monkeypatch.setattr(core_auth, "ADMIN_EMAILS", frozenset({core_auth.OWNER_EMAIL, legacy_email}))

    client = TestClient(_make_app())
    client.get(f"/__test/login/{legacy_email}")

    resp = client.post(SETUP_CHOOSE_FOLDER_ENDPOINT)

    assert resp.status_code == 403
