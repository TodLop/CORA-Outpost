from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _read_template(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_minecraft_admin_template_links_to_setup_workspace_once():
    admin_template = _read_template("app/templates/admin/minecraft.html")

    assert admin_template.count('/minecraft/admin/setup') == 1
    assert "Setup" in admin_template


def test_minecraft_setup_workspace_template_is_guarded_and_safe():
    setup_template = _read_template("app/templates/admin/setup.html")

    for snippet in (
        "Server Setup Workspace",
        'href="/minecraft/admin"',
        "Live service guardrail",
        "Guided setup",
        "/minecraft/admin/api/minecraft/setup/defaults",
        "/minecraft/admin/api/minecraft/setup/preview",
        "/minecraft/admin/api/minecraft/setup/create-server",
        "/minecraft/admin/api/minecraft/paper-targets",
        "createExecuteEndpoint",
        "setup_create_execute_endpoint|tojson",
        "folderPickerEndpoint",
        "setup_choose_folder_endpoint|tojson",
        "canExecuteSetupCreate",
        "can_execute_setup_create",
        "Choose Folder",
        "Manual path fallback",
        "Preview Setup",
        "Creation Preflight",
        "Check Creation Readiness",
        "createPreflightEndpoint",
        "checkCreationPreflight()",
        "Create Server",
        "executeCreateServer()",
        "canExecuteCreateServer()",
        "X-CORA-Setup-Intent",
        "creationPreflightDraftFingerprint",
        "draftFingerprint()",
        "markDraftChanged()",
        "schedulePreview()",
        "previewDebounceTimer",
        "previewRequestId",
        "creationPreflightRequestId",
        "Server files created",
        "createdServerDraftChanged",
        "submitted draft before the latest edits",
        "The profile remains inactive and the server was not started",
        "Owner or manager-admin authorization is required",
        "Minecraft EULA accepted for future creation",
        "eula_accepted",
        "ready_for_execution",
        "Future Execution Policy",
        "Policy gate pending",
        "Profile activation",
        "creationPreflightStatusText",
        "checkingCreationPreflight",
        "Install Plan",
        "Preview before execution",
        "planned_artifacts",
        "non_actions",
        "installPlanReadyText",
        "start.sh Preview",
        "server.properties Preview",
    ):
        assert snippet in setup_template

    forbidden_fragments = (
        "<form",
        "/minecraft/admin/api/minecraft/setup/create-server/execute",
        "Register Existing Profile",
        "registerExistingProfile",
        "registeredProfile",
        "registeringProfile",
        "/minecraft/admin/api/minecraft/server-profiles",
        "Profile metadata saved",
        "only saves profile metadata",
        "/activate",
        "method: 'DELETE'",
        "DELETE",
        "/minecraft/admin/api/minecraft/server-directory",
        "/minecraft/admin/api/minecraft/server/start",
        "/minecraft/admin/api/minecraft/server/stop",
        "/minecraft/admin/api/minecraft/server/restart",
        "/minecraft/admin/api/minecraft/server/recover",
        "/minecraft/admin/api/minecraft/server/command",
        "/minecraft/admin/api/minecraft/server/enable-rcon",
        "/minecraft/admin/api/minecraft/check-updates",
        "/minecraft/admin/api/minecraft/update-with-restart",
        "/minecraft/admin/api/minecraft/update-automation/run",
        "/minecraft/admin/api/minecraft/update/",
        "/minecraft/admin/api/minecraft/upgrade-preflight",
        "/minecraft/admin/api/minecraft/upgrade-manifests",
        "/minecraft/admin/api/minecraft/backup-scheduler/trigger",
        "/minecraft/admin/api/minecraft/rcon",
        "createServer()",
    )
    for fragment in forbidden_fragments:
        assert fragment not in setup_template
    assert '@input="markDraftChanged(); previewSetup()"' not in setup_template

    match = re.search(
        r'@click="checkCreationPreflight\(\)"(?P<attrs>.*?)class=',
        setup_template,
        re.DOTALL,
    )
    assert match, "creation preflight control should be present"
    disabled_expression = match.group("attrs")
    assert "ready_for_creation" not in disabled_expression
    assert "!String(draft.server_directory || '').trim()" in disabled_expression

    mark_method = re.search(
        r"markDraftChanged\(\) \{(?P<body>.*?)\n        \},\n        schedulePreview",
        setup_template,
        re.DOTALL,
    )
    assert mark_method, "draft mutation invalidation method should be present"
    mark_body = mark_method.group("body")
    for snippet in (
        "clearTimeout(this.previewDebounceTimer)",
        "this.loadingPreview = false",
        "this.checkingCreationPreflight = false",
        "this.creationPreflight = null",
        "this.creationPreflightDraftFingerprint = ''",
        "this.createdServerResult = null",
        "this.createdServerDraftChanged = false",
        "this.previewRequestId += 1",
        "this.creationPreflightRequestId += 1",
    ):
        assert snippet in mark_body

    create_button = re.search(
        r'@click="executeCreateServer\(\)"(?P<attrs>.*?)class=',
        setup_template,
        re.DOTALL,
    )
    assert create_button, "create-server execution control should be present"
    assert ':disabled="!canExecuteCreateServer()"' in create_button.group("attrs")

    can_execute = re.search(
        r"canExecuteCreateServer\(\) \{(?P<body>.*?)\n        \},\n        createServerGateText",
        setup_template,
        re.DOTALL,
    )
    assert can_execute, "create execution gate should be present"
    can_execute_body = can_execute.group("body")
    for snippet in (
        "this.canExecuteSetupCreate",
        "this.createExecuteEndpoint",
        "!this.creatingServer",
        "!this.checkingCreationPreflight",
        "this.creationPreflight?.ready_for_execution",
        "this.creationPreflightDraftFingerprint",
        "this.creationPreflightDraftFingerprint === this.draftFingerprint()",
        "this.draft.eula_accepted",
    ):
        assert snippet in can_execute_body

    execute_method = re.search(
        r"async executeCreateServer\(\) \{(?P<body>.*?)\n        \},\n        installPlanReadyText",
        setup_template,
        re.DOTALL,
    )
    assert execute_method, "create execution method should be present"
    execute_body = execute_method.group("body")
    assert "this.canExecuteCreateServer()" in execute_body
    assert "this.createExecuteEndpoint" in execute_body
    assert "'X-CORA-Setup-Intent': 'create-server'" in execute_body
    assert "body: JSON.stringify(this.draft)" in execute_body
    assert "const draftStillCurrent = fingerprint === this.draftFingerprint()" in execute_body
    assert "this.createdServerDraftChanged = !draftStillCurrent" in execute_body
    assert "The submitted create request finished after the draft changed" in execute_body
    assert "if (fingerprint !== this.draftFingerprint()) return" not in execute_body

    fingerprint_method = re.search(
        r"draftFingerprint\(\) \{(?P<body>.*?)\n        \},\n        async loadPaperTargets",
        setup_template,
        re.DOTALL,
    )
    assert fingerprint_method, "draft fingerprint should be present"
    fingerprint_body = fingerprint_method.group("body")
    for field in (
        "profile_name",
        "server_directory",
        "expected_players",
        "memory_max_mb",
        "use_aikar_flags",
        "eula_accepted",
        "minecraft_version",
        "paper_version",
        "paper_filename",
        "server_properties",
        "server_port",
    ):
        assert field in fingerprint_body

    preflight_method = re.search(
        r"async checkCreationPreflight\(\) \{(?P<body>.*?)\n        \},\n        canExecuteCreateServer",
        setup_template,
        re.DOTALL,
    )
    assert preflight_method, "creation preflight method should be present"
    preflight_body = preflight_method.group("body")
    assert "const requestId = ++this.creationPreflightRequestId" in preflight_body
    assert "const fingerprint = this.draftFingerprint()" in preflight_body
    assert "requestId !== this.creationPreflightRequestId" in preflight_body
    assert "fingerprint !== this.draftFingerprint()" in preflight_body
    assert "this.creationPreflightDraftFingerprint = fingerprint" in preflight_body

    preview_method = re.search(
        r"async previewSetup\(force = false\) \{(?P<body>.*?)\n        \},\n        async checkCreationPreflight",
        setup_template,
        re.DOTALL,
    )
    assert preview_method, "preview method should be present"
    preview_body = preview_method.group("body")
    assert "const requestId = ++this.previewRequestId" in preview_body
    assert "const fingerprint = this.draftFingerprint()" in preview_body
    assert "requestId !== this.previewRequestId" in preview_body
    assert "fingerprint !== this.draftFingerprint()" in preview_body
