import pytest

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app.core.auth import ADMIN_EMAILS
from app.routers.admin_server import router as admin_server_router
from app.services import minecraft_server


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login")
    async def _login(request: Request):
        request.session["user_info"] = {"email": next(iter(ADMIN_EMAILS)), "name": "Admin"}
        return {"ok": True}

    app.include_router(admin_server_router, prefix="/minecraft/admin")
    return app


@pytest.mark.parametrize("cmd", ["stop", "/stop", "ban-ip 1.2.3.4", "pardon-ip 1.2.3.4"])
def test_dangerous_commands_are_blocked(monkeypatch, cmd: str):
    async def _fake_send_command(command: str) -> dict:
        return {"success": True, "response": "ok"}

    from app.services import minecraft_server
    monkeypatch.setattr(minecraft_server, "send_command", _fake_send_command)

    client = TestClient(_make_app())
    client.get("/__test/login")
    resp = client.post("/minecraft/admin/api/minecraft/server/command", json={"command": cmd})
    assert resp.status_code == 403


def test_allowed_command_is_disabled_in_public_extract(monkeypatch):
    calls = []

    async def _fake_send_command(command: str) -> dict:
        calls.append(command)
        return {"success": True, "response": "ok"}

    monkeypatch.setattr(minecraft_server, "send_command", _fake_send_command)

    client = TestClient(_make_app())
    client.get("/__test/login")
    resp = client.post("/minecraft/admin/api/minecraft/server/command", json={"command": "list"})
    assert resp.status_code == 403
    assert resp.json().get("success") is False
    assert calls == []


@pytest.mark.parametrize("cmd", ["op testuser", "/op testuser", "deop testuser", "/deop testuser"])
def test_op_and_deop_commands_are_disabled_in_public_extract(monkeypatch, cmd: str):
    calls = []

    async def _fake_send_command(command: str) -> dict:
        calls.append(command)
        return {"success": True, "response": "ok"}

    monkeypatch.setattr(minecraft_server, "send_command", _fake_send_command)

    client = TestClient(_make_app())
    client.get("/__test/login")
    resp = client.post("/minecraft/admin/api/minecraft/server/command", json={"command": cmd})
    assert resp.status_code == 403
    assert resp.json().get("success") is False
    assert calls == []


def test_log_file_listing_returns_all_archived_logs(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "latest.log").write_text("latest\n", encoding="utf-8")

    for idx in range(205):
        (logs_dir / f"2026-02-17-{idx}.log.gz").write_text(f"log {idx}\n", encoding="utf-8")

    monkeypatch.setattr(minecraft_server, "SERVER_DIR", tmp_path)

    client = TestClient(_make_app())
    client.get("/__test/login")
    resp = client.get("/minecraft/admin/api/minecraft/server/log-files")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert len(payload["files"]) == 206
    assert {item["name"] for item in payload["files"]} >= {
        "latest.log",
        "2026-02-17-0.log.gz",
        "2026-02-17-204.log.gz",
    }
