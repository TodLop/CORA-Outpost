import gzip

from app.services import minecraft_server


def test_read_latest_log_filters_rcon_polling_noise_by_default(monkeypatch, tmp_path):
    log_file = tmp_path / "latest.log"
    log_file.write_text(
        "\n".join(
            [
                "[16:13:21] [RCON Client /127.0.0.1 #14246/INFO]: Thread RCON Client /127.0.0.1 shutting down",
                "[16:13:21] [Server thread/INFO]: [Essentials] Rcon issued server command: /list",
                "[16:13:22] [Server thread/INFO]: Player joined the game",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(minecraft_server, "get_latest_log_path", lambda: log_file)

    logs = minecraft_server.read_latest_log(lines=10)

    assert [entry["message"] for entry in logs] == [
        "[16:13:22] [Server thread/INFO]: Player joined the game"
    ]


def test_read_latest_log_can_return_raw_rcon_polling_noise(monkeypatch, tmp_path):
    log_file = tmp_path / "latest.log"
    log_file.write_text(
        "\n".join(
            [
                "[16:13:21] [RCON Client /127.0.0.1 #14246/INFO]: Thread RCON Client /127.0.0.1 shutting down",
                "[16:13:21] [Server thread/INFO]: [Essentials] Rcon issued server command: /list",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(minecraft_server, "get_latest_log_path", lambda: log_file)

    logs = minecraft_server.read_latest_log(lines=10, filtered=False)

    assert len(logs) == 2


def test_read_latest_log_tails_without_returning_entire_file(monkeypatch, tmp_path):
    log_file = tmp_path / "latest.log"
    log_file.write_text(
        "\n".join(f"[16:13:{index:02d}] [Server thread/INFO]: line {index}" for index in range(20)),
        encoding="utf-8",
    )
    monkeypatch.setattr(minecraft_server, "get_latest_log_path", lambda: log_file)

    logs = minecraft_server.read_latest_log(lines=3, filtered=False)

    assert [entry["message"] for entry in logs] == [
        "[16:13:17] [Server thread/INFO]: line 17",
        "[16:13:18] [Server thread/INFO]: line 18",
        "[16:13:19] [Server thread/INFO]: line 19",
    ]


def test_read_log_file_tail_bounds_gzip_logs(tmp_path):
    log_file = tmp_path / "2026-06-13-1.log.gz"
    with gzip.open(log_file, "wt", encoding="utf-8") as f:
        for index in range(10):
            f.write(f"[16:14:{index:02d}] [Server thread/INFO]: archived {index}\n")

    result = minecraft_server.read_log_file_tail(log_file, lines=4)

    assert result["count"] == 4
    assert result["line_limit"] == 4
    assert result["truncated"] is True
    assert [entry["message"] for entry in result["logs"]] == [
        "[16:14:06] [Server thread/INFO]: archived 6",
        "[16:14:07] [Server thread/INFO]: archived 7",
        "[16:14:08] [Server thread/INFO]: archived 8",
        "[16:14:09] [Server thread/INFO]: archived 9",
    ]


def test_read_log_file_tail_rejects_oversized_gzip_logs(monkeypatch, tmp_path):
    log_file = tmp_path / "2026-06-13-2.log.gz"
    with gzip.open(log_file, "wt", encoding="utf-8") as f:
        f.write("[16:15:00] [Server thread/INFO]: archived\n" * 20)

    monkeypatch.setattr(minecraft_server, "MAX_COMPRESSED_LOG_BYTES", 1)

    result = minecraft_server.read_log_file_tail(log_file, lines=4)

    assert result["count"] == 0
    assert result["truncated"] is True
    assert result["max_compressed_bytes"] == 1
    assert "too large" in result["error"]
