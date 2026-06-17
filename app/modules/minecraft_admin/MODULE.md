# MODULE: minecraft_admin

## Purpose
Public-safe Minecraft operation surfaces: setup workspace, admin panel, staff
panel, plugin docs, and backend runbook docs.

## Routes
- `/minecraft/admin*`
- `/minecraft/staff*`
- `/minecraft/plugins*`
- `/minecraft/backend-docs*`

## Dependencies
- `app/services/minecraft_*`
- `app/services/dashboard.py`
- `app/services/plugin_docs.py`
- `app/services/backend_docs.py`
- Multiple templates under `app/templates/admin`, `app/templates/staff`, `app/templates/plugins`

## Auth & RBAC
- Admin routes: Minecraft admin only
- Setup execute route: owner/current manager-admin only, with JSON body and explicit `X-CORA-Setup-Intent: create-server`
- Staff routes: staff/admin with RBAC permissions

## Data & State
- Uses Minecraft server process state and local data files for operations views
- Setup preview and preflight are side-effect free and do not read live server state
- Setup execute writes only generated server artifacts into the approved missing or empty target folder and creates inactive profile metadata with RCON disabled
- Runtime data, logs, backups, OAuth files, local server folders, setup claim files, staging ledgers, and generated Paper jars must not be committed

## Feature Flags
- Controlled by `ENABLED_MODULES` via slug `minecraft_admin`

## Operational Runbook
- If module is offline, verify Minecraft process and RCON status
- Check RBAC settings for staff access issues
- For setup failures, inspect the API response cleanup field before touching target folders manually

## Test Matrix
- Admin dashboard and staff panel permission boundaries
- Setup workspace access, side-effect-free preview/preflight, guarded execute, inactive profile creation, and stale setup marker cleanup
- Plugin docs pages and backend docs access control
- Disabled module unmounts all listed prefixes

## Ownership
- Product owner: CORA-Outpost public Minecraft admin extraction
