# AGENTS.md

## Repository Policy

This repository is a sanitized public-prep extraction of the Minecraft admin web program.

- Do not commit secrets, runtime data, logs, backups, OAuth files, or local machine paths.
- Do not add remotes or push unless the repository owner explicitly asks.
- Keep the public extraction fail-closed: admin-only modules by default, no public site, no economy/market/player records/Wrapped/portfolio/proxy modules.
- Keep arbitrary RCON command execution, RCON password generation, and browser terminal access disabled unless a private fork deliberately reintroduces them with tests and documentation.

## Minecraft Setup Workspace Guardrails

- Setup preview and create preflight must stay side-effect free: no file writes, no profile writes, no server process calls, and no live server state reads.
- Setup execute must remain owner/current manager-admin only, require `application/json`, and require `X-CORA-Setup-Intent: create-server`.
- Setup execute may create files only inside the approved missing or empty target folder and must leave the new profile inactive with RCON disabled.
- Do not add an existing-folder registration branch to the setup workspace. Existing profiles belong in the profile management surface after deliberate review.
- Do not create, commit, or document real setup outputs such as Paper jars, local server folders, setup claim files, staging ledgers, or `eula.txt`.

## Verification

Before declaring publication readiness, run:

```bash
python3 scripts/check_public_extract.py
python3 -m pytest tests/test_public_extract_scope.py
```
