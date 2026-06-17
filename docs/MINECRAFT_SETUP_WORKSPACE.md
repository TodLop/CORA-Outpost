# Minecraft Setup Workspace

The setup workspace is the public-safe path for preparing a new local Paper
server profile. It is designed for careful first-run setup, not for broad file
management or existing-folder registration.

## What It Does

- Lets an admin draft a profile name, target folder, Paper build, memory budget,
  Aikar flags, EULA acceptance, and `server.properties` values.
- Shows a side-effect-free preview of `start.sh`, `server.properties`, planned
  artifacts, warnings, and non-actions.
- Runs a side-effect-free create preflight that must approve the current draft
  before execution.
- Lets only the owner or a current manager-admin execute creation.
- Creates an inactive server profile with `rcon_enabled=false` and
  `set_active=false`.

## What It Does Not Do

- It does not start, stop, restart, recover, back up, update, or activate the
  server.
- It does not run arbitrary RCON commands or generate RCON passwords.
- It does not register existing server folders from the setup workspace.
- It does not write during preview or preflight.
- It does not delete unknown or pre-existing files in the target folder.

## API Contract

| Endpoint | Access | Side effects | Purpose |
| --- | --- | --- | --- |
| `GET /minecraft/admin/setup` | Minecraft admin | None | Render workspace without live server reads |
| `GET /minecraft/admin/api/minecraft/setup/defaults` | Minecraft admin | None | Return safe defaults and host memory summary |
| `POST /minecraft/admin/api/minecraft/setup/choose-folder` | Owner/current manager-admin | Native picker only | Return a selected folder path without persisting it |
| `POST /minecraft/admin/api/minecraft/setup/preview` | Minecraft admin | None | Build preview from draft JSON |
| `POST /minecraft/admin/api/minecraft/setup/create-server` | Minecraft admin | None | Build create preflight from draft JSON |
| `POST /minecraft/admin/api/minecraft/setup/create-server/execute` | Owner/current manager-admin | Target folder + profile metadata | Create files and inactive profile |

The execute endpoint additionally requires:

- `Content-Type: application/json`
- `X-CORA-Setup-Intent: create-server`
- A draft that still matches a successful preflight on the client side
- A missing or empty target folder
- EULA acceptance for writing `eula.txt`

Legacy global admins can still type an absolute path manually for side-effect-free
preview and preflight checks, but native folder selection and execution stay
owner/current manager-admin only.

## Execution Safety

Execution uses claim and staging markers inside the target folder so partial
attempts can be detected and cleaned safely. Cleanup removes only artifacts
created by the same setup attempt and refuses to remove unknown files.

Generated artifacts:

- selected Paper jar
- `start.sh`
- `server.properties`
- `eula.txt`
- inactive server profile metadata

The created profile remains inactive. RCON is disabled by default.

## Publication Hygiene

Do not commit generated setup output. That includes Paper jars, local server
folders, `server.properties`, `eula.txt`, `.cora-setup-claim.json`,
`.cora-setup-staging`, or any path that reveals a local machine layout.

Before publishing, run:

```bash
python3 scripts/check_public_extract.py
python3 -m pytest tests/test_public_extract_scope.py
python3 -m pytest tests/test_minecraft_setup_preview.py tests/test_minecraft_setup_access.py tests/test_admin_theme_templates.py
```
