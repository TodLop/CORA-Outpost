# Roadmap

CORA-Outpost is being prepared as a public-safe operations project. The main
goal is to make the repository understandable in a few minutes without leaking
anything from the private CORA-live deployment.

## Near Term

- Keep README positioning, screenshots, setup, and data-hygiene notes aligned
  with the public extraction boundary.
- Maintain the `v0.1.0-public-preview` release notes as the first public preview
  reference point.
- Add a fake-data demo mode so visitors can run and inspect the admin workflow
  without connecting to a real Minecraft server.
- Document which routes are intentionally disabled in the public extraction.

## Demo Mode

The demo mode should be deterministic, local-only, and clearly marked as sample
data. It should cover:

- Server status, lifecycle operation results, scheduler state, and backup state.
- Staff permissions, warnings, whitelist entries, watchlist entries, and
  moderation history.
- Plugin inventory, plugin documentation, update checks, and preflight results.
- Metrics charts, redacted logs, and backend runbook examples.

## Documentation

- Add a guide for extending the `minecraft_admin` module safely.
- Add an operator guide for plugin documentation and runbook maintenance.
- Add a contributor checklist for public-extraction reviews.
- Consider separate Korean and English operator notes after the first public
  preview is stable.

## Guardrails

The public repository should continue to exclude public-site modules, player
records, market, donations, Wrapped pages, portfolio tools, finance tools, proxy
modules, runtime data, logs, backups, OAuth files, private keys, arbitrary RCON
execution, RCON password generation, and browser terminal/PTTY access.
