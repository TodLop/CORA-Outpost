from fastapi import Depends, FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app.core.auth import (
    OWNER_EMAIL,
    require_admin,
    require_owner,
    require_staff,
)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login/{email}")
    async def _test_login(email: str, request: Request):
        request.session["user_info"] = {"email": email, "name": "Test"}
        return {"ok": True}

    @app.get("/minecraft/admin")
    async def _admin_gate(user_info: dict = Depends(require_admin)):
        return {"ok": True, "user": user_info.get("email")}

    @app.get("/minecraft/staff")
    async def _staff_gate(user_info: dict = Depends(require_staff)):
        return {"ok": True, "user": user_info.get("email")}

    @app.get("/owner-only")
    async def _owner_gate(user_info: dict = Depends(require_owner)):
        return {"ok": True, "user": user_info.get("email")}

    return app


def test_admin_route_unauthenticated_returns_401():
    client = TestClient(_make_app())
    resp = client.get("/minecraft/admin")
    assert resp.status_code == 401


def test_staff_route_unauthenticated_returns_401():
    client = TestClient(_make_app())
    resp = client.get("/minecraft/staff")
    assert resp.status_code == 401


def test_require_staff_authenticated_but_not_staff_returns_403():
    client = TestClient(_make_app())

    client.get("/__test/login/notstaff@example.com")

    resp = client.get("/minecraft/staff")
    assert resp.status_code == 403


def test_owner_route_requires_owner():
    client = TestClient(_make_app())
    client.get("/__test/login/notowner@example.com")
    resp = client.get("/owner-only")
    assert resp.status_code == 403


def test_owner_route_allows_owner():
    client = TestClient(_make_app())
    client.get(f"/__test/login/{OWNER_EMAIL}")
    resp = client.get("/owner-only")
    assert resp.status_code == 200

