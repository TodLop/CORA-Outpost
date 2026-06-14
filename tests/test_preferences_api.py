from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app.core.auth import ADMIN_EMAILS
from app.routers.admin import router as admin_router
from app.routers.staff import router as staff_router
from app.services import user_preferences as prefs_service


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login")
    async def _login(request: Request):
        request.session["user_info"] = {
            "email": next(iter(ADMIN_EMAILS)),
            "name": "Admin",
        }
        return {"ok": True}

    app.include_router(admin_router)
    app.include_router(staff_router)
    return app


def test_staff_preferences_get_put_and_my_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs_service, "PREFERENCES_FILE", tmp_path / "user_preferences.json")

    client = TestClient(_make_app())
    client.get("/__test/login")

    get_resp = client.get("/minecraft/staff/api/preferences")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "ok"
    assert get_resp.json()["preferences"]["theme"] == "dark"

    put_resp = client.put(
        "/minecraft/staff/api/preferences",
        json={"language": "en", "theme": "light", "toast_duration_ms": 7000},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["preferences"]["language"] == "en"
    assert put_resp.json()["preferences"]["theme"] == "light"

    settings_resp = client.get("/minecraft/staff/api/my-settings")
    assert settings_resp.status_code == 200
    assert settings_resp.json()["preferences"]["language"] == "en"
    assert settings_resp.json()["preferences"]["theme"] == "light"


def test_admin_preferences_get_put_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs_service, "PREFERENCES_FILE", tmp_path / "user_preferences.json")

    client = TestClient(_make_app())
    client.get("/__test/login")

    get_resp = client.get("/minecraft/admin/api/preferences")
    assert get_resp.status_code == 200
    assert get_resp.json()["preferences"]["theme"] == "dark"

    bad_put = client.put(
        "/minecraft/admin/api/preferences",
        json={"theme": "ultra-light"},
    )
    assert bad_put.status_code == 400
    assert bad_put.json()["status"] == "error"

    good_put = client.put(
        "/minecraft/admin/api/preferences",
        json={
            "theme": "dark",
            "font_scale": "lg",
            "high_contrast": True,
            "server_name": "Near Outpost",
            "server_name_color": "#38BDF8",
        },
    )
    assert good_put.status_code == 200
    assert good_put.json()["preferences"]["theme"] == "dark"
    assert good_put.json()["preferences"]["font_scale"] == "lg"
    assert good_put.json()["preferences"]["high_contrast"] is True
    assert good_put.json()["preferences"]["server_name"] == "Near Outpost"
    assert good_put.json()["preferences"]["server_name_color"] == "#38bdf8"
