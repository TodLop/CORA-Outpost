from app.modules.registry import ModuleRegistry
from scripts import check_public_extract


EXCLUDED_MODULES = {
    "hub",
    "nearoutpost_public",
    "player_stats",
    "economy",
    "minecraft_market",
    "minecraft_wrapped",
    "portfolio_private",
    "bluemap_proxy",
    "terminal",
    "finance",
}


def test_registry_is_admin_only_by_default(monkeypatch):
    monkeypatch.delenv("ENABLED_MODULES", raising=False)
    registry = ModuleRegistry.from_environment()
    assert registry.enabled_slugs == {"minecraft_admin"}
    assert not (registry.enabled_slugs & EXCLUDED_MODULES)


def test_registry_ignores_broad_enablement(monkeypatch):
    monkeypatch.setenv("ENABLED_MODULES", "all")
    registry = ModuleRegistry.from_environment()
    assert registry.enabled_slugs == {"minecraft_admin"}
    assert not (registry.enabled_slugs & EXCLUDED_MODULES)


def test_registry_allows_runtime_opt_in_only(monkeypatch):
    monkeypatch.setenv("ENABLED_MODULES", "minecraft_admin,minecraft_runtime,economy,terminal")
    registry = ModuleRegistry.from_environment()
    assert registry.enabled_slugs == {"minecraft_admin", "minecraft_runtime"}
    assert not (registry.enabled_slugs & EXCLUDED_MODULES)


def test_public_extract_remote_policy_is_enforced_locally(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    assert check_public_extract.should_enforce_local_only_remote_policy() is True


def test_public_extract_remote_policy_allows_github_actions_checkout(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert check_public_extract.should_enforce_local_only_remote_policy() is False
    assert check_public_extract.validate_remote_policy(
        "origin\thttps://example.invalid/private.git (fetch)\n"
        "origin\thttps://example.invalid/private.git (push)\n"
    ) == []


def test_public_extract_remote_policy_allows_expected_public_origin(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    errors = check_public_extract.validate_remote_policy(
        "origin\thttps://github.com/TodLop/CORA-Outpost.git (fetch)\n"
        "origin\thttps://github.com/TodLop/CORA-Outpost.git (push)\n"
    )

    assert errors == []


def test_public_extract_remote_policy_allows_expected_public_origin_over_ssh(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    errors = check_public_extract.validate_remote_policy(
        "origin\tgit@github.com:TodLop/CORA-Outpost.git (fetch)\n"
        "origin\tgit@github.com:TodLop/CORA-Outpost.git (push)\n"
    )

    assert errors == []


def test_public_extract_remote_policy_rejects_unexpected_remote(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    errors = check_public_extract.validate_remote_policy(
        "origin\thttps://github.com/TodLop/CORA-Outpost.git (fetch)\n"
        "backup\thttps://example.invalid/private.git (push)\n"
        "origin\thttps://example.invalid/private.git (push)\n"
    )

    assert any("unexpected git remote configured: backup" in error for error in errors)
    assert any("unexpected git remote URL for origin" in error for error in errors)
