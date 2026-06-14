# MODULE: minecraft_admin

## Purpose
Minecraft operation surfaces: admin panel, staff panel, dashboard, plugin docs, and backend runbook docs.

## Routes
- `/minecraft/admin*`
- `/minecraft/staff*`
- `/minecraft/admin*`
- `/minecraft/plugins*`
- `/minecraft/backend-docs*`

## Dependencies
- `app/services/minecraft_*`
- `app/services/dashboard.py`
- `app/services/plugin_docs.py`
- `app/services/backend_docs.py`
- Multiple templates under `app/templates/admin`, `app/templates/staff`, `app/templates/plugins`

## Auth & RBAC
- Admin routes: admin only
- Staff routes: staff/admin with RBAC permissions

## Data & State
- Uses Minecraft server process state and local data files
- Taskboard image store: `/dashboard-images`

## Feature Flags
- Controlled by `ENABLED_MODULES` via slug `minecraft_admin`

## Operational Runbook
- If module is offline, verify Minecraft process and RCON status
- Check RBAC settings for staff access issues

## Test Matrix
- Admin dashboard and staff panel permission boundaries
- Taskboard CRUD, plugin docs pages, and backend docs access control
- Disabled module unmounts all listed prefixes

## Ownership
- Product owner: Near Outpost server operations
