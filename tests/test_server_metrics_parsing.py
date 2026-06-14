import pytest

from app.services import server_metrics


@pytest.mark.parametrize(
    "text, expected",
    [
        ("TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0", 20.0),
        ("TPS from last 5s, 1m, 5m, 15m: 19.8, 19.6, 20.0, 20.0", 19.6),
        ("§aTPS from last 5s, 1m, 5m, 15m: §e19.8, §619.5, §a20.0, §a20.0", 19.5),
        ("TPS: *18.73", 18.73),
    ],
)
def test_parse_tps_handles_multiple_output_formats(text: str, expected: float):
    assert server_metrics._parse_tps(text) == pytest.approx(expected)


def test_parse_tps_returns_none_when_no_value_present():
    assert server_metrics._parse_tps("No TPS data available") is None
    assert server_metrics._parse_tps("TPS from last 5s, 1m, 5m, 15m:") is None
    assert server_metrics._parse_tps("TPS from last 1m, 5m, 15m: N/A, N/A, N/A") is None


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Server tick times (avg/min/max) from last 5s, 10s, 1m:\n"
            "◴ 4.6/0.5/21.7, 5.7/0.5/105.5, 6.4/0.3/133.6",
            4.6,
        ),
        (
            "Server tick times (avg/min/max) from last 5s, 10s, 1m: "
            "11.6/6.6/18.8, 11.8/4.8/76.2, 12.0/4.8/88.8",
            11.6,
        ),
        (
            "Server tick times (avg/min/max) from last 5s, 10s, 1m:\n"
            "◴ 12 / 6.0 / 30, 13/7/40, 14/8/50",
            12.0,
        ),
        (
            "MSPT: 7.1/0.4/32.8",
            7.1,
        ),
        (
            "MSPT from last 5s, 10s, 1m: 10.2, 11.0, 12.3",
            10.2,
        ),
        (
            "§aMSPT: §e9.8/0.4/30.1",
            9.8,
        ),
    ],
)
def test_parse_mspt_extracts_first_avg_value(text: str, expected: float):
    assert server_metrics._parse_mspt(text) == pytest.approx(expected)


def test_parse_mspt_returns_none_when_no_bucket_found():
    assert server_metrics._parse_mspt("No tick timing data available") is None


def test_normalize_process_cpu_percent_returns_expected_value():
    assert server_metrics.normalize_process_cpu_percent(400.0, logical_cores=8) == pytest.approx(50.0)


def test_normalize_process_cpu_percent_supports_none():
    assert server_metrics.normalize_process_cpu_percent(None, logical_cores=8) is None


def test_get_process_cpu_percent_max_uses_core_count():
    assert server_metrics.get_process_cpu_percent_max(logical_cores=12) == pytest.approx(1200.0)


class _DummyProcess:
    def __init__(self, cpu_value: float):
        self._cpu_value = cpu_value

    def cpu_percent(self, interval=None):
        return self._cpu_value


def test_sample_process_cpu_percent_returns_raw_and_normalized():
    raw, normalized = server_metrics.sample_process_cpu_percent(_DummyProcess(240.0), logical_cores=6)
    assert raw == pytest.approx(240.0)
    assert normalized == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_canonical_metric_sample_writes_db_and_broadcasts_matching_live_payload(monkeypatch):
    inserted = []
    broadcasted = []

    def fake_insert(cpu_percent, ram_mb, players=0, tps=None, mspt=None):
        inserted.append({
            "cpu_percent": cpu_percent,
            "ram_mb": ram_mb,
            "players": players,
            "tps": tps,
            "mspt": mspt,
        })

    async def subscriber(payload):
        broadcasted.append(dict(payload))

    monkeypatch.setattr(server_metrics.metrics_db, "insert_raw_metric", fake_insert)
    monkeypatch.setattr(server_metrics.time, "time", lambda: 1_800_000_001.25)
    monkeypatch.setattr(server_metrics, "_metric_subscribers", [subscriber])
    monkeypatch.setattr(server_metrics, "_latest_tps", 19.876)
    monkeypatch.setattr(server_metrics, "_latest_mspt", 12.345)
    monkeypatch.setattr(server_metrics, "_latest_live_metric", None)

    await server_metrics._record_and_publish_metric_sample(
        cpu_raw=240.0,
        cpu_normalized=40.0,
        ram_mb=1536.44,
        players=7,
    )

    assert inserted == [{
        "cpu_percent": 240.0,
        "ram_mb": 1536.44,
        "players": 7,
        "tps": 19.876,
        "mspt": 12.345,
    }]
    assert len(broadcasted) == 1
    payload = broadcasted[0]
    assert payload["type"] == "metric"
    assert payload["timestamp"] == pytest.approx(1_800_000_001.25)
    assert payload["cpu_percent_raw"] == pytest.approx(240.0)
    assert payload["cpu_percent_normalized"] == pytest.approx(40.0)
    assert payload["ram_mb"] == pytest.approx(1536.4)
    assert payload["players"] == 7
    assert payload["tps"] == pytest.approx(19.88)
    assert payload["mspt"] == pytest.approx(12.35)
    assert payload["collection_role"] == "canonical"
    assert server_metrics._latest_live_metric == payload


@pytest.mark.asyncio
async def test_canonical_metric_sample_writes_db_without_live_subscribers(monkeypatch):
    inserted = []

    def fake_insert(cpu_percent, ram_mb, players=0, tps=None, mspt=None):
        inserted.append((cpu_percent, ram_mb, players, tps, mspt))

    monkeypatch.setattr(server_metrics.metrics_db, "insert_raw_metric", fake_insert)
    monkeypatch.setattr(server_metrics, "_metric_subscribers", [])
    monkeypatch.setattr(server_metrics, "_latest_tps", None)
    monkeypatch.setattr(server_metrics, "_latest_mspt", None)
    monkeypatch.setattr(server_metrics, "_latest_live_metric", None)

    await server_metrics._record_and_publish_metric_sample(
        cpu_raw=12.0,
        cpu_normalized=3.0,
        ram_mb=512.0,
        players=1,
    )

    assert inserted == [(12.0, 512.0, 1, None, None)]
    assert server_metrics._latest_live_metric["cpu_percent_raw"] == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_live_broadcast_failure_does_not_block_canonical_insert(monkeypatch):
    inserted = []

    def fake_insert(cpu_percent, ram_mb, players=0, tps=None, mspt=None):
        inserted.append((cpu_percent, ram_mb, players, tps, mspt))

    async def failing_subscriber(payload):
        raise RuntimeError("closed")

    monkeypatch.setattr(server_metrics.metrics_db, "insert_raw_metric", fake_insert)
    monkeypatch.setattr(server_metrics, "_metric_subscribers", [failing_subscriber])
    monkeypatch.setattr(server_metrics, "_latest_tps", 20.0)
    monkeypatch.setattr(server_metrics, "_latest_mspt", 7.5)
    monkeypatch.setattr(server_metrics, "_latest_live_metric", None)

    await server_metrics._record_and_publish_metric_sample(
        cpu_raw=50.0,
        cpu_normalized=10.0,
        ram_mb=768.0,
        players=2,
    )

    assert inserted == [(50.0, 768.0, 2, 20.0, 7.5)]
    assert server_metrics._metric_subscribers == []
