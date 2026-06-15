# Demo And Preview

CORA-Outpost currently exists as a public-safe extraction of a live Minecraft
operations cockpit. It does not yet include a fake-data demo mode or bundled
sample server state.

The README screenshots are redacted crops from the live operator workflow. They
are meant to show product shape without exposing account identity, private
navigation, runtime logs, player records, backups, OAuth material, incident
artifacts, local paths, or live domain details.

## Preview Surfaces

- Admin operations dashboard: server lifecycle, guarded operations, plugin
  versions, backup automation, reboot automation, and update safety.
- Moderation workflow: warnings, temp bans, whitelist controls, watchlist, and
  staff-scoped action forms.
- Plugin documentation: plugin inventory and operator-facing runbooks for
  maintaining server-specific plugin behavior.
- Metrics and analytics: operational health snapshots and time-series views.
- Backend runbooks: documentation surfaced inside the protected admin workflow.

## Not Included In The Public Preview

- Public community website pages.
- Player records, market, donations, Wrapped pages, portfolio tools, finance
  tools, and proxy modules.
- Runtime data, live logs, backups, OAuth files, service account files, private
  keys, and incident artifacts.
- Arbitrary RCON command execution, RCON password generation, or browser
  terminal/PTTY access.

## Planned Demo Mode

A future demo mode should use deterministic fake fixtures instead of live data:

- Sample server status, process state, TPS-like metrics, and disk usage.
- Sample staff accounts, permissions, warnings, watchlist entries, and
  moderation history.
- Sample plugin inventory, documentation pages, update checks, and preflight
  results.
- Sample scheduler state for backups, reboots, maintenance windows, and update
  automation.
- Sample redacted logs and runbook content suitable for screenshots and issue
  reproduction.

Until that exists, public screenshots should stay cropped, redacted, and free of
runtime identifiers.

## Local Smoke Test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
SECRET_KEY=replace-with-a-long-random-secret ENABLED_MODULES=minecraft_admin \
  uvicorn app:create_app --factory --host 127.0.0.1 --port 8000
```

Before publishing a public mirror, run the extraction checks:

```bash
python3 scripts/check_public_extract.py
python3 -m pytest tests/test_public_extract_scope.py
```
