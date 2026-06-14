# CORA Minecraft Admin

FastAPI-based Minecraft Paper server management dashboard extracted from CORA-live.

This repository is intentionally scoped to protected server operations only:

- admin/staff auth and RBAC
- server status and operation wrappers
- restart/recover/start/stop workflows
- scheduler and backup controls
- whitelist/moderation tools
- plugin/update management
- metrics and redacted log viewing

The public/community website, player records, economy, market, donations, Wrapped pages, portfolio/finance tools, proxy modules, runtime data, credentials, and incident artifacts are not part of this repository.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python - <<'PY'
from app import create_app
app = create_app()
print(app.title)
PY
```

Run locally:

```bash
uvicorn app:create_app --factory --host 127.0.0.1 --port 8000
```

## Public Hygiene Check

Before publishing or pushing a mirror, run:

```bash
python scripts/check_public_extract.py
python -m pytest tests/test_public_extract_scope.py
```

The checker fails on common private paths, live identity defaults, excluded modules/routes, terminal shell surfaces, generated caches, and broad module defaults.

## Safety Defaults

- `ENABLED_MODULES` defaults to `minecraft_admin` only.
- `minecraft_runtime` is opt-in for local operators.
- `*` and `all` module enablement are ignored.
- Arbitrary RCON command execution and RCON password generation are disabled in this public extraction.
- Browser terminal/PTTY access is excluded.

