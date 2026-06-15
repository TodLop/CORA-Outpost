# Contributing

Thanks for taking a look at CORA-Outpost. This repository is a public-safe
extraction of a private live-operations system, so contribution work starts with
the safety boundary.

## Public Extraction Rules

- Do not commit secrets, `.env` files, OAuth files, service account files,
  private keys, tokens, runtime data, logs, backups, incident artifacts, or local
  machine paths.
- Do not reintroduce public-site modules, player records, market, donations,
  Wrapped pages, portfolio tools, finance tools, proxy modules, or other private
  CORA-live surfaces.
- Keep arbitrary RCON command execution, RCON password generation, and browser
  terminal/PTTY access disabled unless a private fork deliberately reintroduces
  them with tests and documentation.
- Use placeholders and deterministic fake fixtures for docs, screenshots, tests,
  and examples.

## Before Opening A Pull Request

Run the public extraction checks:

```bash
python3 scripts/check_public_extract.py
python3 -m pytest tests/test_public_extract_scope.py
```

If behavior changed, also run the relevant targeted tests and, when practical,
the full test suite:

```bash
SECRET_KEY=replace-with-a-long-random-secret ENABLED_MODULES=minecraft_admin \
  python3 -m pytest
```

## Documentation Assets

Screenshots and examples must be public-safe. Crop or redact account identity,
private navigation, live domains, player identifiers, runtime logs, incident
details, secrets, backups, and local paths before adding assets to the
repository.

Do not add raw live screenshots to the repository. Keep only the reviewed,
redacted asset that is intended for public documentation.
