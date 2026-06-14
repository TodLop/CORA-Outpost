import pytest

from app.services import user_preferences as prefs_service


def test_defaults_returned_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs_service, "PREFERENCES_FILE", tmp_path / "user_preferences.json")

    prefs = prefs_service.get_preferences("staff@example.com")

    assert prefs["language"] == "ko"
    assert prefs["theme"] == "dark"
    assert prefs["font_scale"] == "md"
    assert prefs["high_contrast"] is False
    assert prefs["reduced_motion"] is False
    assert prefs["toast_duration_ms"] == 4000
    assert prefs["server_name"] == "MINECRAFT SERVER"
    assert prefs["server_name_color"] == "#4ade80"


def test_set_preferences_persists_valid_patch(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs_service, "PREFERENCES_FILE", tmp_path / "user_preferences.json")

    updated = prefs_service.set_preferences(
        "staff@example.com",
        {
            "language": "en",
            "theme": "light",
            "toast_duration_ms": 7000,
            "server_name": "Near Outpost",
            "server_name_color": "#22C55E",
        },
        updated_by="self",
    )

    assert updated["language"] == "en"
    assert updated["theme"] == "light"
    assert updated["toast_duration_ms"] == 7000
    assert updated["server_name"] == "Near Outpost"
    assert updated["server_name_color"] == "#22c55e"

    loaded = prefs_service.get_preferences("staff@example.com")
    assert loaded["language"] == "en"
    assert loaded["theme"] == "light"
    assert loaded["toast_duration_ms"] == 7000
    assert loaded["server_name"] == "Near Outpost"
    assert loaded["server_name_color"] == "#22c55e"


def test_set_preferences_rejects_invalid_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs_service, "PREFERENCES_FILE", tmp_path / "user_preferences.json")

    with pytest.raises(prefs_service.PreferenceValidationError) as exc:
        prefs_service.set_preferences(
            "staff@example.com",
            {
                "theme": "purple",
                "toast_duration_ms": 12000,
                "server_name": " ",
                "server_name_color": "green",
                "unknown": True,
            },
            updated_by="self",
        )

    errors = exc.value.errors
    assert "theme" in errors
    assert "toast_duration_ms" in errors
    assert "server_name" in errors
    assert "server_name_color" in errors
    assert "unknown" in errors
