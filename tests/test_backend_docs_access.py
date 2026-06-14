from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app.core import auth as auth_core
from app.core.auth import ADMIN_EMAILS
from app.routers.backend_docs import router as backend_docs_router
from app.services import backend_docs as backend_docs_service
from app.services import permissions as permissions_service


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login/{email}")
    async def _login(email: str, request: Request):
        request.session["user_info"] = {
            "email": email,
            "name": "Test User",
        }
        return {"ok": True}

    app.include_router(backend_docs_router)
    return app


def _seed_docs(monkeypatch, tmp_path):
    docs_dir = tmp_path / "docs" / "minecraft" / "backend"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "index.md").write_text(
        """---
title: Backend Docs Index
audience: privileged_staff
owner: ops
last_reviewed_at: 2026-02-18
tags:
  - index
---
# Backend Docs Index

- 000-restart-control
- 040-admin-only-contract
""",
        encoding="utf-8",
    )
    (docs_dir / "000-restart-control.md").write_text(
        """---
title: Restart Control
audience: privileged_staff
owner: ops
last_reviewed_at: 2026-02-18
tags:
  - restart
---
# Restart Control

Cooldown is **120 seconds** after success.
""",
        encoding="utf-8",
    )
    (docs_dir / "040-admin-only-contract.md").write_text(
        """---
title: Admin Contract
audience: admin_only
owner: backend-admin
last_reviewed_at: 2026-02-18
tags:
  - idempotency
---
# Admin Contract

Sensitive operations contract details.
""",
        encoding="utf-8",
    )
    (docs_dir / "090-legacy-notes.md").write_text(
        "# Legacy Notes\n\nNo front matter here.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(backend_docs_service, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(permissions_service, "RBAC_SETTINGS_FILE", tmp_path / "rbac_settings.json")
    monkeypatch.setattr(auth_core, "STAFF_EMAILS", frozenset({"staff@example.com"}))


def test_backend_docs_staff_without_permission_gets_403(monkeypatch, tmp_path):
    _seed_docs(monkeypatch, tmp_path)
    client = TestClient(_make_app())
    client.get("/__test/login/staff@example.com")

    resp = client.get("/minecraft/backend-docs")
    assert resp.status_code == 403


def test_backend_docs_staff_with_permission_gets_page(monkeypatch, tmp_path):
    _seed_docs(monkeypatch, tmp_path)
    permissions_service.grant_permission(
        email="staff@example.com",
        permission="ops:backend_docs:view",
        admin_email="admin@example.com",
    )

    client = TestClient(_make_app())
    client.get("/__test/login/staff@example.com")

    resp = client.get("/minecraft/backend-docs/000-restart-control")
    assert resp.status_code == 200
    assert "Restart Control" in resp.text
    assert "120 seconds" in resp.text
    assert "000-restart-control.md" in resp.text
    assert "Staff Visible" in resp.text


def test_backend_docs_admin_bypass(monkeypatch, tmp_path):
    _seed_docs(monkeypatch, tmp_path)
    admin_email = next(iter(ADMIN_EMAILS))

    client = TestClient(_make_app())
    client.get(f"/__test/login/{admin_email}")

    resp = client.get("/minecraft/backend-docs")
    assert resp.status_code == 200
    assert "Backend Docs Index" in resp.text
    assert "Admin Only" in resp.text


def test_backend_docs_missing_slug_returns_404(monkeypatch, tmp_path):
    _seed_docs(monkeypatch, tmp_path)
    permissions_service.grant_permission(
        email="staff@example.com",
        permission="ops:backend_docs:view",
        admin_email="admin@example.com",
    )

    client = TestClient(_make_app())
    client.get("/__test/login/staff@example.com")

    resp = client.get("/minecraft/backend-docs/not-found")
    assert resp.status_code == 404


def test_staff_cannot_access_admin_only_doc(monkeypatch, tmp_path):
    _seed_docs(monkeypatch, tmp_path)
    permissions_service.grant_permission(
        email="staff@example.com",
        permission="ops:backend_docs:view",
        admin_email="admin@example.com",
    )

    client = TestClient(_make_app())
    client.get("/__test/login/staff@example.com")

    resp = client.get("/minecraft/backend-docs/040-admin-only-contract")
    assert resp.status_code == 404

    api_resp = client.get("/minecraft/backend-docs/api/docs/040-admin-only-contract")
    assert api_resp.status_code == 404


def test_docs_index_filters_by_audience(monkeypatch, tmp_path):
    _seed_docs(monkeypatch, tmp_path)
    permissions_service.grant_permission(
        email="staff@example.com",
        permission="ops:backend_docs:view",
        admin_email="admin@example.com",
    )
    admin_email = next(iter(ADMIN_EMAILS))

    client = TestClient(_make_app())
    client.get("/__test/login/staff@example.com")
    staff_index = client.get("/minecraft/backend-docs/api/docs")
    assert staff_index.status_code == 200
    staff_slugs = {doc["slug"] for doc in staff_index.json()["docs"]}
    assert "040-admin-only-contract" not in staff_slugs
    assert "000-restart-control" in staff_slugs

    client.get(f"/__test/login/{admin_email}")
    admin_index = client.get("/minecraft/backend-docs/api/docs")
    assert admin_index.status_code == 200
    admin_slugs = {doc["slug"] for doc in admin_index.json()["docs"]}
    assert "040-admin-only-contract" in admin_slugs


def test_missing_front_matter_defaults_to_admin_only(monkeypatch, tmp_path):
    _seed_docs(monkeypatch, tmp_path)
    permissions_service.grant_permission(
        email="staff@example.com",
        permission="ops:backend_docs:view",
        admin_email="admin@example.com",
    )
    admin_email = next(iter(ADMIN_EMAILS))

    client = TestClient(_make_app())
    client.get("/__test/login/staff@example.com")

    staff_resp = client.get("/minecraft/backend-docs/090-legacy-notes")
    assert staff_resp.status_code == 404

    client.get(f"/__test/login/{admin_email}")
    admin_resp = client.get("/minecraft/backend-docs/090-legacy-notes")
    assert admin_resp.status_code == 200
    assert "Legacy Notes" in admin_resp.text


def test_backend_docs_permission_maps_to_module(monkeypatch, tmp_path):
    _seed_docs(monkeypatch, tmp_path)
    permissions_service.grant_permission(
        email="staff@example.com",
        permission="ops:backend_docs:view",
        admin_email="admin@example.com",
    )

    modules = permissions_service.get_user_visible_modules("staff@example.com")
    assert "operations_docs" in modules
