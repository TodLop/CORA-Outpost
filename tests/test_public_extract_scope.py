from app.modules.registry import ModuleRegistry


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

