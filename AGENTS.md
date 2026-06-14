# AGENTS.md

## Repository Policy

This repository is a sanitized public-prep extraction of the Minecraft admin web program.

- Do not commit secrets, runtime data, logs, backups, OAuth files, or local machine paths.
- Do not add remotes or push unless the repository owner explicitly asks.
- Keep the public extraction fail-closed: admin-only modules by default, no public site, no economy/market/player records/Wrapped/portfolio/proxy modules.
- Keep arbitrary RCON command execution, RCON password generation, and browser terminal access disabled unless a private fork deliberately reintroduces them with tests and documentation.

## Verification

Before declaring publication readiness, run:

```bash
python scripts/check_public_extract.py
python -m pytest tests/test_public_extract_scope.py
```

