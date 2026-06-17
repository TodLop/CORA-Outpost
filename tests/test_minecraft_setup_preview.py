import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from app import create_app
from app.core.auth import ADMIN_EMAILS
from app.routers import admin_server
from app.routers.admin_server import router as admin_server_router
from app.services import (
    minecraft_server,
    minecraft_settings,
    minecraft_setup,
    minecraft_setup_executor,
    minecraft_updater,
    native_folder_picker,
)


ROOT = Path(__file__).resolve().parents[1]


def _make_router_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/__test/login")
    async def _login(request: Request):
        request.session["user_info"] = {"email": next(iter(ADMIN_EMAILS)), "name": "Admin"}
        return {"ok": True}

    app.include_router(admin_server_router, prefix="/minecraft/admin")
    return app


def _memory(total_mb: int = 32768, available_mb: int = 24576) -> SimpleNamespace:
    return SimpleNamespace(
        total=total_mb * minecraft_setup.MIB,
        available=available_mb * minecraft_setup.MIB,
    )


def _payload(tmp_path: Path, **overrides):
    payload = {
        "profile_name": "Friends Survival",
        "server_directory": str(tmp_path / "new-server"),
        "expected_players": 12,
        "memory_max_mb": 4096,
        "use_aikar_flags": True,
        "eula_accepted": True,
        "minecraft_version": "1.21.11",
        "paper_version": "130",
        "paper_filename": "paper-1.21.11-130.jar",
        "server_properties": {
            "motd": "CORA test server",
            "max_players": 24,
            "white_list": True,
            "difficulty": "normal",
            "gamemode": "survival",
            "view_distance": 10,
            "simulation_distance": 8,
            "online_mode": True,
            "server_port": 25565,
        },
    }
    payload.update(overrides)
    return payload


def _make_existing_server_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (path / "server.properties").write_text("enable-rcon=false\n", encoding="utf-8")
    return path


def _execute_headers() -> dict[str, str]:
    return {"X-CORA-Setup-Intent": "create-server"}


def _paper_target(jar_bytes: bytes = b"paper jar") -> SimpleNamespace:
    return SimpleNamespace(
        version="1.21.11-130",
        build=130,
        download_url="https://paper.example/paper.jar",
        filename="paper-1.21.11-130.jar",
        sha256=hashlib.sha256(jar_bytes).hexdigest(),
        game_versions=["1.21.11"],
    )


def _patch_setup_executor(monkeypatch, jar_bytes: bytes = b"paper jar") -> None:
    async def _resolve(minecraft_version: str):
        assert minecraft_version == "1.21.11"
        return _paper_target(jar_bytes)

    async def _download(download_url: str):
        assert download_url == "https://paper.example/paper.jar"
        return jar_bytes

    monkeypatch.setattr(minecraft_setup_executor, "resolve_paper_target", _resolve)
    monkeypatch.setattr(minecraft_setup_executor, "download_paper_bytes", _download)


def _old_setup_timestamp() -> str:
    value = datetime.now(timezone.utc) - timedelta(seconds=minecraft_setup_executor.CLAIM_STALE_SECONDS + 30)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_setup_service_source_does_not_import_live_settings_or_updater():
    source = (ROOT / "app/services/minecraft_setup.py").read_text(encoding="utf-8")

    assert "minecraft_settings" not in source
    assert "minecraft_updater" not in source


def test_native_folder_picker_source_is_neutral():
    source = (ROOT / "app/services/native_folder_picker.py").read_text(encoding="utf-8")

    assert "minecraft_settings" not in source
    assert "minecraft_updater" not in source


def test_setup_folder_picker_returns_selected_path(monkeypatch, tmp_path):
    selected = tmp_path / "server"
    monkeypatch.setattr(
        minecraft_setup.native_folder_picker,
        "choose_directory_with_native_dialog",
        lambda prompt: str(selected),
    )

    assert minecraft_setup.choose_setup_server_directory() == str(selected)


def test_setup_folder_picker_wraps_cancelled_selection(monkeypatch):
    def _cancelled(prompt):
        raise native_folder_picker.FolderPickerCancelled("Folder selection was cancelled.")

    monkeypatch.setattr(minecraft_setup.native_folder_picker, "choose_directory_with_native_dialog", _cancelled)

    with pytest.raises(minecraft_setup.SetupFolderPickerCancelled):
        minecraft_setup.choose_setup_server_directory()


def test_native_folder_picker_reports_unavailable(monkeypatch):
    monkeypatch.setattr(native_folder_picker.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(
        native_folder_picker.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    with pytest.raises(native_folder_picker.FolderPickerUnavailable):
        native_folder_picker.choose_directory_with_native_dialog("Choose folder")


def test_setup_defaults_contract(monkeypatch):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())

    result = minecraft_setup.build_setup_defaults()

    assert result["host_memory"] == {
        "total_mb": 32768,
        "available_mb": 24576,
        "reserved_mb": 8192,
        "recommended_max_mb": 4096,
        "safe_max_mb": 16384,
    }
    assert result["defaults"]["profile_name"] == "CORA-Outpost Server"
    assert result["defaults"]["memory_max_mb"] == 4096
    assert result["defaults"]["eula_accepted"] is False
    assert result["defaults"]["paper_filename"] == "paper.jar"
    assert result["defaults"]["server_properties"]["white_list"] is True
    assert result["options"]["difficulties"] == ["peaceful", "easy", "normal", "hard"]
    assert result["options"]["gamemodes"] == ["survival", "creative", "adventure", "spectator"]


def test_setup_preview_builds_files_without_creating_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "new-server"

    preview = minecraft_setup.build_setup_preview(_payload(tmp_path))

    assert not target.exists()
    assert preview["profile"] == {
        "name": "Friends Survival",
        "suggested_id": "friends-survival",
        "server_directory": str(target),
    }
    assert preview["folder"]["exists"] is False
    assert preview["folder"]["start_script_exists"] is False
    assert preview["folder"]["server_properties_exists"] is False
    assert preview["folder"]["profile_registration_ready"] is False
    assert preview["memory"]["memory_max_mb"] == 4096
    assert "-Xmx4096M" in preview["start_script"]
    assert "-XX:+UseG1GC" in preview["start_script"]
    assert "paper-1.21.11-130.jar" in preview["start_script"]
    assert "white-list=true" in preview["server_properties"]
    assert "enable-rcon=false" in preview["server_properties"]
    install_plan = preview["install_plan"]
    assert install_plan["mode"] == "plan_only"
    assert install_plan["ready_for_creation"] is True
    assert install_plan["target"]["server_directory"] == str(target)
    assert install_plan["paper"]["label"] == "1.21.11 / 130 / paper-1.21.11-130.jar"
    assert install_plan["planned_artifacts"] == [
        {
            "label": "Paper server jar",
            "path": str(target / "paper-1.21.11-130.jar"),
            "source": "selected Paper target",
        },
        {
            "label": "Start script",
            "path": str(target / "start.sh"),
            "source": "start.sh preview",
        },
        {
            "label": "Server properties",
            "path": str(target / "server.properties"),
            "source": "server.properties preview",
        },
    ]
    assert "No files are written." in install_plan["non_actions"]
    assert "No profile is activated." in install_plan["non_actions"]
    policy = preview["creation_policy"]
    assert policy["ready_for_execution"] is True
    assert policy["requires_eula_acceptance"] is True
    assert policy["eula_accepted"] is True
    assert policy["future_eula_txt"] == "eula=true"
    assert policy["write_scope"] == "target_directory_only"
    assert policy["profile_metadata"]["set_active"] is False
    assert policy["failure_cleanup"]["paper_download"] == "remove_only_files_created_by_this_attempt"
    assert policy["failure_cleanup"]["never_delete_existing_target_files"] is True
    assert policy["server_actions"] == {
        "start": False,
        "stop": False,
        "restart": False,
        "backup": False,
        "update": False,
        "rcon": False,
    }


def test_setup_preview_requires_explicit_eula_for_future_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())

    preview = minecraft_setup.build_setup_preview(_payload(tmp_path, eula_accepted=False))

    assert preview["install_plan"]["ready_for_creation"] is True
    policy = preview["creation_policy"]
    assert policy["ready_for_execution"] is False
    assert policy["requires_eula_acceptance"] is True
    assert policy["eula_accepted"] is False
    assert policy["future_eula_txt"] is None
    assert policy["profile_metadata"]["set_active"] is False
    assert all(value is False for value in policy["server_actions"].values())


def test_setup_create_preflight_allows_missing_folder_without_creating_it(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "new-server"

    result = minecraft_setup.build_create_server_preflight(_payload(tmp_path))

    assert not target.exists()
    preflight = result["preflight"]
    assert preflight["mode"] == "preflight_only"
    assert preflight["ready_for_creation"] is True
    assert preflight["ready_for_execution"] is True
    assert preflight["target"]["server_directory"] == str(target)
    assert preflight["creation_policy"]["eula_accepted"] is True
    assert preflight["creation_policy"]["profile_metadata"]["set_active"] is False
    assert "No files are written." in preflight["non_actions"]
    assert "No server profile is created." in preflight["non_actions"]
    assert result["preview"]["install_plan"]["ready_for_creation"] is True


def test_setup_create_preflight_blocks_ready_folder_until_eula_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "new-server"

    with pytest.raises(minecraft_setup.SetupCreationNotReady) as exc_info:
        minecraft_setup.build_create_server_preflight(_payload(tmp_path, eula_accepted=False))

    assert not target.exists()
    assert exc_info.value.errors == {
        "eula_accepted": "Accept the Minecraft EULA before creating a new server."
    }
    assert exc_info.value.preflight["ready_for_creation"] is True
    assert exc_info.value.preflight["ready_for_execution"] is False
    assert exc_info.value.preflight["creation_policy"]["eula_accepted"] is False
    assert exc_info.value.preflight["creation_policy"]["future_eula_txt"] is None
    assert exc_info.value.preview["install_plan"]["ready_for_creation"] is True


def test_setup_preview_allows_existing_empty_folder_for_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "empty-server"
    target.mkdir()

    preview = minecraft_setup.build_setup_preview(_payload(tmp_path, server_directory=str(target)))
    preflight = minecraft_setup.build_create_server_preflight(_payload(tmp_path, server_directory=str(target)))

    assert list(target.iterdir()) == []
    assert preview["folder"]["exists"] is True
    assert preview["folder"]["empty"] is True
    assert preview["folder"]["start_script_exists"] is False
    assert preview["folder"]["server_properties_exists"] is False
    assert preview["folder"]["profile_registration_ready"] is False
    install_plan = preview["install_plan"]
    assert install_plan["ready_for_creation"] is True
    assert install_plan["target"]["server_directory"] == str(target)
    assert install_plan["target"]["folder_exists"] is True
    assert install_plan["target"]["folder_empty"] is True
    assert preflight["preflight"]["ready_for_creation"] is True
    assert list(target.iterdir()) == []


def test_setup_preflight_existing_empty_folder_uses_target_writability(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "empty-server"
    target.mkdir()

    def _fake_access(path, mode):
        resolved = Path(path).resolve()
        if resolved == target.resolve():
            return True
        if resolved == tmp_path.resolve():
            return False
        return True

    monkeypatch.setattr(minecraft_setup.os, "access", _fake_access)

    preview = minecraft_setup.build_setup_preview(_payload(tmp_path, server_directory=str(target)))
    preflight = minecraft_setup.build_create_server_preflight(_payload(tmp_path, server_directory=str(target)))

    assert preview["folder"]["parent_writable"] is False
    assert preview["folder"]["target_writable"] is True
    assert preview["install_plan"]["ready_for_creation"] is True
    assert preflight["preflight"]["ready_for_creation"] is True


def test_setup_preview_marks_existing_server_folder_registration_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    server_dir = _make_existing_server_dir(tmp_path / "existing-server")

    preview = minecraft_setup.build_setup_preview(_payload(tmp_path, server_directory=str(server_dir)))

    assert preview["folder"]["exists"] is True
    assert preview["folder"]["empty"] is False
    assert preview["folder"]["start_script_exists"] is True
    assert preview["folder"]["server_properties_exists"] is True
    assert preview["folder"]["profile_registration_ready"] is True
    assert preview["profile"]["server_directory"] == str(server_dir)
    assert preview["install_plan"]["ready_for_creation"] is False
    assert preview["install_plan"]["warnings"] == [
        "Existing server files were detected; choose a missing or empty folder for setup creation."
    ]
    with pytest.raises(minecraft_setup.SetupCreationNotReady) as exc_info:
        minecraft_setup.build_create_server_preflight(_payload(tmp_path, server_directory=str(server_dir)))
    assert exc_info.value.errors == {
        "server_directory": "Existing server files were detected; choose a missing or empty folder for setup creation."
    }
    assert exc_info.value.preflight["ready_for_creation"] is False


def test_setup_preview_install_plan_warns_for_existing_non_empty_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    server_dir = tmp_path / "occupied-folder"
    server_dir.mkdir()
    (server_dir / "notes.txt").write_text("not a server marker\n", encoding="utf-8")

    preview = minecraft_setup.build_setup_preview(_payload(tmp_path, server_directory=str(server_dir)))

    assert preview["folder"]["exists"] is True
    assert preview["folder"]["empty"] is False
    assert preview["folder"]["profile_registration_ready"] is False
    assert preview["install_plan"]["ready_for_creation"] is False
    assert preview["install_plan"]["warnings"] == [
        "Choose a missing or empty folder for setup creation."
    ]
    with pytest.raises(minecraft_setup.SetupCreationNotReady) as exc_info:
        minecraft_setup.build_create_server_preflight(_payload(tmp_path, server_directory=str(server_dir)))
    assert exc_info.value.errors == {
        "server_directory": "Target directory already contains files; preview will not modify them."
    }


def test_setup_preview_clamps_and_sanitizes(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    payload = _payload(
        tmp_path,
        expected_players=500,
        memory_max_mb=999999,
        server_properties={
            "motd": "Hello\nWorld\x00!",
            "max_players": 999,
            "white_list": False,
            "difficulty": "hard",
            "gamemode": "creative",
            "view_distance": 1,
            "simulation_distance": 99,
            "online_mode": True,
            "server_port": 70000,
        },
    )

    preview = minecraft_setup.build_setup_preview(payload)

    assert preview["memory"]["expected_players"] == 100
    assert preview["memory"]["memory_max_mb"] == 16384
    assert preview["server_properties_entries"]["motd"] == "Hello World !"
    assert preview["server_properties_entries"]["max-players"] == 200
    assert preview["server_properties_entries"]["view-distance"] == 2
    assert preview["server_properties_entries"]["simulation-distance"] == 32
    assert preview["server_properties_entries"]["server-port"] == 65535
    assert any("expected_players was clamped" in warning for warning in preview["warnings"])
    assert any("memory_max_mb was clamped" in warning for warning in preview["warnings"])


def test_setup_preview_rejects_unsafe_jar(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())

    with pytest.raises(minecraft_setup.SetupValidationError) as exc_info:
        minecraft_setup.build_setup_preview(_payload(tmp_path, paper_filename="../paper.jar"))

    assert exc_info.value.errors == {"paper_filename": "Paper filename must be a safe .jar basename."}


def test_setup_defaults_and_preview_require_admin():
    client = TestClient(_make_router_app())

    defaults = client.get("/minecraft/admin/api/minecraft/setup/defaults")
    choose_folder = client.post("/minecraft/admin/api/minecraft/setup/choose-folder")
    preview = client.post("/minecraft/admin/api/minecraft/setup/preview", json={})
    create_server = client.post("/minecraft/admin/api/minecraft/setup/create-server", json={})
    execute_server = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json={},
        headers=_execute_headers(),
    )

    assert defaults.status_code in {401, 403}
    assert choose_folder.status_code in {401, 403}
    assert preview.status_code in {401, 403}
    assert create_server.status_code in {401, 403}
    assert execute_server.status_code in {401, 403}


def test_setup_choose_folder_endpoint_returns_selected_path(monkeypatch, tmp_path):
    selected = tmp_path / "selected-server"
    calls: list[str] = []

    def _choose():
        calls.append("choose")
        return str(selected)

    async def _to_thread(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", "unknown"))
        return func(*args, **kwargs)

    monkeypatch.setattr(minecraft_setup, "choose_setup_server_directory", _choose)
    monkeypatch.setattr(admin_server.asyncio, "to_thread", _to_thread)

    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post("/minecraft/admin/api/minecraft/setup/choose-folder")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "path": str(selected)}
    assert calls == ["_choose", "choose"]


def test_setup_choose_folder_endpoint_handles_cancel_without_state_change(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", settings_file)

    def _cancelled():
        raise minecraft_setup.SetupFolderPickerCancelled("Folder selection was cancelled.")

    monkeypatch.setattr(minecraft_setup, "choose_setup_server_directory", _cancelled)

    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post("/minecraft/admin/api/minecraft/setup/choose-folder")

    assert resp.status_code == 400
    assert resp.json()["status"] == "cancelled"
    assert not settings_file.exists()


def test_setup_api_is_side_effect_free(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())

    def _forbidden(*args, **kwargs):
        raise AssertionError("setup preview API must not call live server helpers")

    for name in (
        "_save_payload",
        "get_settings",
        "get_active_profile",
        "get_profile",
        "list_profiles",
        "get_server_directory",
        "inspect_server_directory",
        "validate_server_directory",
        "create_profile",
        "set_active_profile",
        "update_profile",
        "delete_profile",
    ):
        monkeypatch.setattr(minecraft_settings, name, _forbidden, raising=False)

    for name in (
        "start_server",
        "stop_server",
        "restart_server",
        "recover_server",
        "send_command",
        "is_server_running",
    ):
        monkeypatch.setattr(minecraft_server, name, _forbidden, raising=False)

    for name in (
        "create_upgrade_manifest",
        "execute_upgrade_manifest",
        "download_upgrade_artifacts",
        "get_paper_upgrade_targets",
    ):
        monkeypatch.setattr(minecraft_updater, name, _forbidden, raising=False)

    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    defaults = client.get("/minecraft/admin/api/minecraft/setup/defaults")
    monkeypatch.setattr(minecraft_setup, "choose_setup_server_directory", lambda: str(target))
    choose_folder = client.post("/minecraft/admin/api/minecraft/setup/choose-folder")
    preview = client.post("/minecraft/admin/api/minecraft/setup/preview", json=_payload(tmp_path))
    create_server = client.post("/minecraft/admin/api/minecraft/setup/create-server", json=_payload(tmp_path))

    assert defaults.status_code == 200
    assert choose_folder.status_code == 200
    assert preview.status_code == 200
    assert create_server.status_code == 200
    assert choose_folder.json()["path"] == str(target)
    assert preview.json()["preview"]["profile"]["server_directory"] == str(target)
    assert create_server.json()["preflight"]["target"]["server_directory"] == str(target)
    assert create_server.json()["preflight"]["ready_for_execution"] is True
    assert not target.exists()


def test_setup_preview_endpoint_rejects_invalid_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/preview",
        json=_payload(tmp_path, paper_filename="paper.sh"),
    )

    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error_code"] == "setup_preview_invalid"
    assert payload["errors"] == {"paper_filename": "Paper filename must be a safe .jar basename."}


def test_setup_create_server_endpoint_rejects_invalid_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server",
        json=_payload(tmp_path, paper_filename="paper.sh"),
    )

    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error_code"] == "setup_create_invalid"
    assert payload["errors"] == {"paper_filename": "Paper filename must be a safe .jar basename."}


def test_setup_create_server_endpoint_keeps_invalid_target_parent_as_400(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server",
        json=_payload(tmp_path, server_directory=str(tmp_path / "missing-parent" / "server")),
    )

    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error_code"] == "setup_create_invalid"
    assert payload["errors"] == {"server_directory_parent": "Parent directory does not exist."}
    assert "preflight" not in payload
    assert "preview" not in payload


def test_setup_create_server_endpoint_rejects_unwritable_existing_empty_target(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "empty-server"
    target.mkdir()

    def _fake_access(path, mode):
        resolved = Path(path).resolve()
        if resolved == target.resolve():
            return False
        return True

    monkeypatch.setattr(minecraft_setup.os, "access", _fake_access)
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server",
        json=_payload(tmp_path, server_directory=str(target)),
    )

    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error_code"] == "setup_create_invalid"
    assert payload["errors"] == {"server_directory_permissions": "Target directory is not writable."}
    assert "preflight" not in payload
    assert list(target.iterdir()) == []


def test_setup_create_server_endpoint_rejects_unwritable_non_empty_target(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "occupied-folder"
    target.mkdir()
    (target / "notes.txt").write_text("not a server marker\n", encoding="utf-8")

    def _fake_access(path, mode):
        resolved = Path(path).resolve()
        if resolved == target.resolve():
            return False
        return True

    monkeypatch.setattr(minecraft_setup.os, "access", _fake_access)
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server",
        json=_payload(tmp_path, server_directory=str(target)),
    )

    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error_code"] == "setup_create_invalid"
    assert payload["errors"] == {"server_directory_permissions": "Target directory is not writable."}
    assert "preflight" not in payload
    assert "preview" not in payload
    assert (target / "notes.txt").read_text(encoding="utf-8") == "not a server marker\n"


def test_setup_create_server_endpoint_returns_not_ready_for_existing_server(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    server_dir = _make_existing_server_dir(tmp_path / "existing-server")
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server",
        json=_payload(tmp_path, server_directory=str(server_dir)),
    )

    assert resp.status_code == 409
    payload = resp.json()
    assert payload["error_code"] == "setup_create_not_ready"
    assert payload["errors"] == {
        "server_directory": "Existing server files were detected; choose a missing or empty folder for setup creation."
    }
    assert payload["preflight"]["ready_for_creation"] is False
    assert payload["preflight"]["ready_for_execution"] is False
    assert payload["preview"]["folder"]["profile_registration_ready"] is True


def test_setup_create_server_endpoint_returns_not_ready_until_eula_acceptance(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server",
        json=_payload(tmp_path, eula_accepted=False),
    )

    assert resp.status_code == 409
    payload = resp.json()
    assert payload["error_code"] == "setup_create_not_ready"
    assert payload["error"] == "Minecraft setup policy is not ready for execution."
    assert payload["errors"] == {
        "eula_accepted": "Accept the Minecraft EULA before creating a new server."
    }
    assert payload["preflight"]["ready_for_creation"] is True
    assert payload["preflight"]["ready_for_execution"] is False
    assert payload["preflight"]["creation_policy"]["requires_eula_acceptance"] is True
    assert payload["preflight"]["creation_policy"]["eula_accepted"] is False
    assert payload["preview"]["install_plan"]["ready_for_creation"] is True
    assert not target.exists()


def test_setup_create_server_endpoint_requires_boolean_eula_acceptance(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server",
        json=_payload(tmp_path, eula_accepted="yes"),
    )

    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error_code"] == "setup_create_invalid"
    assert payload["errors"] == {"eula_accepted": "Value must be true or false."}
    assert "preflight" not in payload
    assert "preview" not in payload
    assert not target.exists()


def test_setup_create_server_endpoint_returns_preflight_for_ready_target(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post("/minecraft/admin/api/minecraft/setup/create-server", json=_payload(tmp_path))

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["preflight"]["mode"] == "preflight_only"
    assert payload["preflight"]["ready_for_creation"] is True
    assert payload["preflight"]["ready_for_execution"] is True
    assert payload["preflight"]["target"]["server_directory"] == str(target)
    assert payload["preflight"]["creation_policy"]["eula_accepted"] is True
    assert payload["preflight"]["creation_policy"]["profile_metadata"]["set_active"] is False
    assert payload["preflight"]["creation_policy"]["server_actions"]["start"] is False
    assert payload["preview"]["install_plan"]["ready_for_creation"] is True
    assert not target.exists()


def test_setup_execute_endpoint_rejects_requests_without_intent_or_json(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    missing_intent = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
    )
    wrong_type = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        content="not-json",
        headers={"X-CORA-Setup-Intent": "create-server", "content-type": "text/plain"},
    )
    malformed_json = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        content="{",
        headers={"X-CORA-Setup-Intent": "create-server", "content-type": "application/json"},
    )

    assert missing_intent.status_code == 400
    assert missing_intent.json()["error_code"] == "setup_create_intent_required"
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error_code"] == "setup_create_json_required"
    assert malformed_json.status_code == 400
    assert malformed_json.json()["error_code"] == "setup_create_invalid"
    assert not target.exists()


def test_setup_execute_endpoint_creates_server_files_and_inactive_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    audit_messages = []
    monkeypatch.setattr(
        admin_server.admin_audit_logger,
        "info",
        lambda message, *args: audit_messages.append(message % args),
    )

    def _forbidden(*args, **kwargs):
        raise AssertionError("setup execute must not call server lifecycle helpers")

    for name in ("start_server", "stop_server", "restart_server", "recover_server", "send_command"):
        monkeypatch.setattr(minecraft_server, name, _forbidden, raising=False)
    for name in ("create_upgrade_manifest", "execute_upgrade_manifest", "download_upgrade_artifacts"):
        monkeypatch.setattr(minecraft_updater, name, _forbidden, raising=False)

    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert resp.status_code == 200
    payload = resp.json()
    profile = payload["result"]["profile"]
    assert payload["result"]["mode"] == "execution"
    assert payload["result"]["idempotent"] is False
    assert profile["id"].startswith("setup-friends-survival-")
    assert profile["server_directory"] == str(target.resolve())
    assert profile["operations_enabled"] is True
    assert profile["rcon_enabled"] is False
    assert profile["readonly"] is False
    assert minecraft_settings.get_settings()["active_profile_id"] == "sample"
    assert (target / "paper-1.21.11-130.jar").read_bytes() == b"paper jar"
    assert "exec java" in (target / "start.sh").read_text(encoding="utf-8")
    assert (target / "start.sh").stat().st_mode & 0o111
    assert "motd=CORA test server" in (target / "server.properties").read_text(encoding="utf-8")
    assert (target / "eula.txt").read_text(encoding="utf-8") == "eula=true\n"
    assert not (target / minecraft_setup_executor.CLAIM_FILE).exists()
    assert not (target / minecraft_setup_executor.STAGING_DIR).exists()
    assert any("setup_create_execute_completed" in message for message in audit_messages)
    assert any(profile["id"] in message for message in audit_messages)


def test_setup_execute_endpoint_supports_existing_empty_target(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    target = tmp_path / "empty-server"
    target.mkdir()
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path, server_directory=str(target)),
        headers=_execute_headers(),
    )

    assert resp.status_code == 200
    assert (target / "paper-1.21.11-130.jar").is_file()
    assert resp.json()["result"]["profile"]["server_directory"] == str(target.resolve())


def test_setup_execute_endpoint_is_idempotent_for_matching_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    first = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )
    second = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["result"]["idempotent"] is True
    assert {artifact["kind"] for artifact in second.json()["result"]["created_artifacts"]} == {
        "paper_jar",
        "start_script",
        "server_properties",
        "eula",
    }
    assert len(minecraft_settings.get_settings()["profiles"]) == 2


def test_setup_execute_endpoint_idempotent_path_requires_eula(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    first = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )
    retry = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path, eula_accepted=False),
        headers=_execute_headers(),
    )

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["error_code"] == "setup_create_not_ready"


def test_setup_execute_endpoint_idempotent_path_rejects_extra_files_without_exposing_them(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    first = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )
    (target / "private-note.txt").write_text("do not expose me\n", encoding="utf-8")
    retry = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["error_code"] == "setup_create_conflict"
    assert "private-note" not in json.dumps(retry.json())


def test_setup_execute_endpoint_idempotent_path_surfaces_fresh_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    first = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )
    profile_id = first.json()["result"]["profile"]["id"]
    (target / minecraft_setup_executor.CLAIM_FILE).write_text(
        json.dumps({
            "owner": minecraft_setup_executor.OWNER,
            "profile_id": profile_id,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "state": "claimed",
        }),
        encoding="utf-8",
    )
    retry = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["error_code"] == "setup_create_in_progress"
    assert (target / minecraft_setup_executor.CLAIM_FILE).exists()


def test_setup_execute_endpoint_idempotent_path_cleans_stale_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    first = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )
    profile_id = first.json()["result"]["profile"]["id"]
    (target / minecraft_setup_executor.CLAIM_FILE).write_text(
        json.dumps({
            "owner": minecraft_setup_executor.OWNER,
            "profile_id": profile_id,
            "created_at": _old_setup_timestamp(),
            "state": "claimed",
        }),
        encoding="utf-8",
    )
    retry = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["result"]["idempotent"] is True
    assert retry.json()["result"]["recovered_attempts"] == [{"removed": [minecraft_setup_executor.CLAIM_FILE]}]
    assert not (target / minecraft_setup_executor.CLAIM_FILE).exists()


def test_setup_execute_endpoint_idempotent_path_refuses_stale_profile_created_staging(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    first = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )
    profile_id = first.json()["result"]["profile"]["id"]
    attempt_dir = target / minecraft_setup_executor.STAGING_DIR / "attempt"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / minecraft_setup_executor.LEDGER_FILE).write_text(
        json.dumps({
            "owner": minecraft_setup_executor.OWNER,
            "profile_id": profile_id,
            "state": "profile_created",
            "created_at": _old_setup_timestamp(),
            "artifacts": [],
        }),
        encoding="utf-8",
    )
    retry = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["error_code"] == "setup_create_conflict"
    assert "manual review" in retry.json()["error"]


def test_setup_execute_endpoint_rejects_paper_mismatch_before_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    audit_messages = []
    monkeypatch.setattr(
        admin_server.admin_audit_logger,
        "info",
        lambda message, *args: audit_messages.append(message % args),
    )

    async def _resolve(minecraft_version: str):
        target = _paper_target()
        target.build = 131
        target.version = "1.21.11-131"
        target.filename = "paper-1.21.11-131.jar"
        return target

    async def _download(download_url: str):
        raise AssertionError("mismatched Paper target must not be downloaded")

    monkeypatch.setattr(minecraft_setup_executor, "resolve_paper_target", _resolve)
    monkeypatch.setattr(minecraft_setup_executor, "download_paper_bytes", _download)
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "setup_paper_target_mismatch"
    assert not target.exists()
    assert any("setup_create_execute_failed" in message for message in audit_messages)
    assert any("setup_paper_target_mismatch" in message for message in audit_messages)


def test_setup_execute_endpoint_rejects_hash_mismatch_before_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    target = _paper_target(b"expected")
    target.sha256 = "0" * 64

    async def _resolve(minecraft_version: str):
        return target

    async def _download(download_url: str):
        return b"actual"

    monkeypatch.setattr(minecraft_setup_executor, "resolve_paper_target", _resolve)
    monkeypatch.setattr(minecraft_setup_executor, "download_paper_bytes", _download)
    server_dir = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert resp.status_code == 502
    assert resp.json()["error_code"] == "setup_paper_hash_mismatch"
    assert not server_dir.exists()


def test_setup_execute_endpoint_returns_in_progress_for_fresh_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    target = tmp_path / "new-server"
    target.mkdir()
    (target / minecraft_setup_executor.CLAIM_FILE).write_text(
        json.dumps({
            "owner": minecraft_setup_executor.OWNER,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "state": "claimed",
        }),
        encoding="utf-8",
    )
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "setup_create_in_progress"
    assert (target / minecraft_setup_executor.CLAIM_FILE).exists()


def test_setup_execute_endpoint_recovers_stale_claim_before_create(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    target = tmp_path / "new-server"
    target.mkdir()
    (target / minecraft_setup_executor.CLAIM_FILE).write_text(
        json.dumps({
            "owner": minecraft_setup_executor.OWNER,
            "created_at": _old_setup_timestamp(),
            "state": "claimed",
        }),
        encoding="utf-8",
    )
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert resp.status_code == 200
    assert resp.json()["result"]["recovered_attempts"]
    assert (target / "paper-1.21.11-130.jar").is_file()
    assert not (target / minecraft_setup_executor.CLAIM_FILE).exists()


def test_setup_execute_endpoint_preserves_final_file_collision(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)

    def _race_link(src, dst):
        Path(dst).write_bytes(b"external file")
        raise FileExistsError(str(dst))

    monkeypatch.setattr(minecraft_setup_executor.os, "link", _race_link)
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "setup_create_conflict"
    assert (target / "paper-1.21.11-130.jar").read_bytes() == b"external file"
    assert not (target / "start.sh").exists()
    assert not (target / minecraft_setup_executor.CLAIM_FILE).exists()
    assert not (target / minecraft_setup_executor.STAGING_DIR).exists()


def test_setup_execute_endpoint_cleans_claim_when_target_changes_after_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)
    original_create_claim_file = minecraft_setup_executor._create_claim_file

    def _claim_then_race(target, claim):
        original_create_claim_file(target, claim)
        (target / "racer.txt").write_text("external file\n", encoding="utf-8")

    monkeypatch.setattr(minecraft_setup_executor, "_create_claim_file", _claim_then_race)
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    resp = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "setup_create_conflict"
    assert (target / "racer.txt").read_text(encoding="utf-8") == "external file\n"
    assert not (target / minecraft_setup_executor.CLAIM_FILE).exists()
    assert not (target / minecraft_setup_executor.STAGING_DIR).exists()
    assert not (target / "paper-1.21.11-130.jar").exists()
    assert not (target / "start.sh").exists()


def test_setup_execute_endpoint_preserves_finals_after_profile_created_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(minecraft_setup.psutil, "virtual_memory", lambda: _memory())
    monkeypatch.setattr(minecraft_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    _patch_setup_executor(monkeypatch)

    original_write_claim = minecraft_setup_executor._write_claim

    def _fail_after_profile_created(target, claim):
        if claim.get("state") == "profile_created":
            raise RuntimeError("simulated ledger tail failure")
        return original_write_claim(target, claim)

    monkeypatch.setattr(minecraft_setup_executor, "_write_claim", _fail_after_profile_created)
    target = tmp_path / "new-server"
    client = TestClient(_make_router_app())
    client.get("/__test/login")

    failed = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert failed.status_code == 500
    assert (target / "paper-1.21.11-130.jar").read_bytes() == b"paper jar"
    assert (target / "start.sh").is_file()

    monkeypatch.setattr(minecraft_setup_executor, "_write_claim", original_write_claim)
    retry = client.post(
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        json=_payload(tmp_path),
        headers=_execute_headers(),
    )

    assert retry.status_code == 200
    assert retry.json()["result"]["idempotent"] is True
    assert (target / "paper-1.21.11-130.jar").read_bytes() == b"paper jar"


def test_setup_executor_refuses_to_cleanup_unlisted_staging_files(monkeypatch, tmp_path):
    target = tmp_path / "server"
    target.mkdir()
    attempt_dir = target / minecraft_setup_executor.STAGING_DIR / "attempt"
    files_dir = attempt_dir / "files"
    files_dir.mkdir(parents=True)
    (attempt_dir / minecraft_setup_executor.LEDGER_FILE).write_text(
        json.dumps({
            "owner": minecraft_setup_executor.OWNER,
            "attempt_id": "attempt",
            "created_at": _old_setup_timestamp(),
            "state": "staged",
            "artifacts": [],
        }),
        encoding="utf-8",
    )
    (files_dir / "unlisted.tmp").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(minecraft_setup_executor.SetupExecutionError) as exc_info:
        minecraft_setup_executor._cleanup_ledger(
            target,
            {
                "owner": minecraft_setup_executor.OWNER,
                "attempt_id": "attempt",
                "created_at": _old_setup_timestamp(),
                "state": "staged",
                "artifacts": [],
            },
            delete_publishing_finals=True,
        )

    assert exc_info.value.error_code == "setup_create_conflict"
    assert (files_dir / "unlisted.tmp").read_text(encoding="utf-8") == "keep me\n"


SETUP_ROUTE_PATHS = {
    "/minecraft/admin/setup",
    "/minecraft/admin/api/minecraft/setup/defaults",
    "/minecraft/admin/api/minecraft/setup/choose-folder",
    "/minecraft/admin/api/minecraft/setup/preview",
    "/minecraft/admin/api/minecraft/setup/create-server",
    "/minecraft/admin/api/minecraft/setup/create-server/execute",
}


def _route_paths(routes, prefix: str = "") -> set[str]:
    paths = set()
    for route in routes:
        route_path = getattr(route, "path", None)
        if route_path is not None:
            paths.add(f"{prefix}{route_path}")

        nested_routes = getattr(route, "routes", ())
        if nested_routes:
            paths.update(_route_paths(nested_routes, f"{prefix}{route_path or ''}"))

        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            include_context = getattr(route, "include_context", None)
            include_prefix = getattr(include_context, "prefix", "")
            paths.update(
                _route_paths(
                    getattr(original_router, "routes", ()),
                    f"{prefix}{include_prefix}",
                )
            )
    return paths


def _app_route_paths() -> set[str]:
    return _route_paths(create_app().routes)


def test_minecraft_admin_setup_routes_follow_module_enabled_state(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    monkeypatch.setenv("ENABLED_MODULES", "minecraft_admin")
    enabled_paths = _app_route_paths()
    assert SETUP_ROUTE_PATHS <= enabled_paths

    monkeypatch.setenv("ENABLED_MODULES", "minecraft_runtime")
    disabled_paths = _app_route_paths()
    assert SETUP_ROUTE_PATHS.isdisjoint(disabled_paths)
