import asyncio

from app.services import minecraft_server
from app.services.rcon import RCONConfig


def test_online_player_snapshot_reuses_recent_rcon_list(monkeypatch):
    manager = minecraft_server._manager
    manager.online_players_cache = None
    manager.online_players_cache_time = 0
    manager.status_cache = None
    manager.status_cache_time = 0

    calls = {"connect": 0, "send": 0, "disconnect": 0}

    class FakeRCON:
        def __init__(self, host, port, password):
            pass

        def connect(self):
            calls["connect"] += 1
            return True

        def send_command(self, command):
            calls["send"] += 1
            assert command == "list"
            return "There are 2 of 15 players online: [VIP] Alice, Bob"

        def disconnect(self):
            calls["disconnect"] += 1

    monkeypatch.setattr(minecraft_server, "load_server_properties", lambda: {"max-players": "15"})
    monkeypatch.setattr(minecraft_server, "get_rcon_config", lambda: RCONConfig(True, "127.0.0.1", 25575, "pw"))
    monkeypatch.setattr(minecraft_server, "RCONClient", FakeRCON)
    monkeypatch.setattr(manager, "_get_process_snapshot_sync", lambda: (True, 1234, False))
    monkeypatch.setattr(manager, "_is_port_listening", lambda port: True)

    first = minecraft_server.get_online_players_snapshot()
    second = minecraft_server.get_online_players_snapshot()

    assert first["players"] == ["Alice", "Bob"]
    assert second["players_online"] == 2
    assert second["max_players"] == 15
    assert calls == {"connect": 1, "send": 1, "disconnect": 1}


def test_server_status_reuses_recent_full_status_snapshot(monkeypatch):
    manager = minecraft_server.ServerManager()
    calls = {"process": 0, "port": 0}

    def fake_process_snapshot():
        calls["process"] += 1
        return False, None, False

    def fake_port_check(port):
        calls["port"] += 1
        return False

    monkeypatch.setattr(minecraft_server, "load_server_properties", lambda: {"max-players": "15"})
    monkeypatch.setattr(minecraft_server, "get_rcon_config", lambda: RCONConfig(False, "127.0.0.1", 25575, ""))
    monkeypatch.setattr(manager, "_get_process_snapshot_sync", fake_process_snapshot)
    monkeypatch.setattr(manager, "_is_port_listening", fake_port_check)

    first = manager.get_server_status()
    second = manager.get_server_status()

    assert first.running is False
    assert second.max_players == 15
    assert calls == {"process": 1, "port": 1}

    manager.invalidate_status_cache()
    manager.get_server_status()

    assert calls == {"process": 2, "port": 2}


def test_server_status_preserves_player_snapshot_freshness(monkeypatch):
    manager = minecraft_server.ServerManager()

    monkeypatch.setattr(minecraft_server, "load_server_properties", lambda: {"max-players": "15"})
    monkeypatch.setattr(minecraft_server, "get_rcon_config", lambda: RCONConfig(False, "127.0.0.1", 25575, ""))
    monkeypatch.setattr(manager, "_get_process_snapshot_sync", lambda: (True, 1234, False))
    monkeypatch.setattr(manager, "_is_port_listening", lambda port: True)
    monkeypatch.setattr(
        manager,
        "get_online_players_snapshot",
        lambda: {
            "players_online": 1,
            "max_players": 15,
            "players": ["Steve"],
            "stale": True,
            "last_updated": 123.0,
        },
    )

    status = manager.get_server_status(force_refresh=True)
    cached = manager.get_server_status()

    assert status.players == ["Steve"]
    assert status.stale is True
    assert status.last_updated == 123.0
    assert cached.stale is True
    assert cached.last_updated == 123.0


def test_korean_rcon_list_response_keeps_current_and_max_order():
    response = (
        "§6최대 §c15§6 명이 접속 가능하고, §c4§6 명의 플레이어가 접속중입니다.\n"
        "§6default§r: §7[Infrastructure_Architect]§fBuilderOne, "
        "§5[Manager]ExampleAdmin§f, "
        "§x§1§0§b§9§8§1[Plugin_Curator]§fRedstoneTwo, "
        "[§4Raid_Leader§f]Quartz07\n"
    )

    parsed = minecraft_server._manager._parse_list_response(response, fallback_max_players=15)

    assert parsed["players_online"] == 4
    assert parsed["max_players"] == 15
    assert parsed["players"] == ["BuilderOne", "ExampleAdmin", "RedstoneTwo", "Quartz07"]


def test_rcon_list_fallback_uses_player_names_not_unrelated_numbers():
    response = "default: Player123, Bob2"

    parsed = minecraft_server._manager._parse_list_response(response, fallback_max_players=15)

    assert parsed["players_online"] == 2
    assert parsed["max_players"] == 15
    assert parsed["players"] == ["Player123", "Bob2"]


def test_send_command_disconnects_when_command_fails(monkeypatch):
    manager = minecraft_server._manager
    calls = {"disconnect": 0}

    class FakeRCON:
        def __init__(self, host, port, password):
            pass

        def connect(self):
            return True

        def send_command(self, command):
            raise ConnectionError("boom")

        def disconnect(self):
            calls["disconnect"] += 1

    monkeypatch.setattr(manager, "_is_server_running_sync", lambda: True)
    monkeypatch.setattr(
        minecraft_server.minecraft_settings,
        "get_active_profile_operation_block",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(minecraft_server, "get_rcon_config", lambda: RCONConfig(True, "127.0.0.1", 25575, "pw"))
    monkeypatch.setattr(minecraft_server, "RCONClient", FakeRCON)

    result = asyncio.run(manager.send_command("list"))

    assert result["success"] is False
    assert calls["disconnect"] == 1
