# Public Extraction Architecture

CORA-Outpost is a sanitized Minecraft admin extraction from a larger private
codebase. The public repository keeps the admin and staff operation surfaces,
but excludes public-site, economy, market, player-record, Wrapped, portfolio,
finance, proxy, terminal, runtime-data, and incident surfaces.

## Module Boundary

```mermaid
flowchart TD
    App["FastAPI app factory"] --> Registry["Fail-closed module registry"]
    Registry --> Admin["minecraft_admin<br/>enabled by default"]
    Registry -. "explicit opt-in" .-> Runtime["minecraft_runtime<br/>startup hooks only"]
    Registry -. "not present" .-> Excluded["public site, economy, market,<br/>player records, Wrapped, proxy"]

    Admin --> AdminRoutes["Admin routes"]
    Admin --> StaffRoutes["Staff routes"]
    Admin --> DocsRoutes["Plugin and backend docs"]

    AdminRoutes --> Setup["Setup workspace"]
    AdminRoutes --> Ops["Operations dashboard"]
    StaffRoutes --> Staff["Staff workflows"]
```

`ENABLED_MODULES` defaults to `minecraft_admin`. Broad values such as `all` or
`*` are ignored so accidental environment values cannot mount excluded modules.

## Trust Boundaries

- Browser sessions enter through FastAPI session middleware and trusted-host
  checks.
- Admin routes require Minecraft admin access.
- Staff routes require staff/admin access and, for narrower actions, RBAC
  permissions.
- Setup creation is stricter than setup preview: execute requires owner/current
  manager-admin access, `application/json`, and `X-CORA-Setup-Intent:
  create-server`.
- Runtime hooks are disabled by default and only start if `minecraft_runtime`
  is explicitly enabled.

## Setup Boundary

```mermaid
flowchart LR
    Draft["Setup draft"] --> Preview["Preview API<br/>no writes"]
    Draft --> Preflight["Create preflight<br/>no writes"]
    Preflight --> Execute["Execute API<br/>owner/current manager-admin"]
    Execute --> Target["Missing or empty target folder"]
    Execute --> Profile["Inactive profile metadata<br/>RCON disabled"]

    Preview -. "must not call" .-> Live["Live server process"]
    Preflight -. "must not call" .-> Live
    Execute -. "must not call" .-> Lifecycle["start/stop/restart/recover/RCON"]
```

The setup workspace is intentionally not a general file manager and not an
existing-folder registration wizard. It creates a new Paper server layout only
after preflight approves the current draft.

## Data Hygiene

The public repository must not include runtime or local operator data:

- `.env`, OAuth files, tokens, service accounts, private keys
- Minecraft logs, backups, live server folders, generated Paper jars
- Setup claim files, staging ledgers, `eula.txt`, generated `server.properties`
- Local absolute paths and live identity defaults
- Private modules from the larger codebase

`scripts/check_public_extract.py` scans tracked and untracked public candidates
for blocked paths, file names, module names, route names, sensitive text
patterns, and unexpected git remotes.
