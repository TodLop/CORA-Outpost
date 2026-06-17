"""Native folder picker helpers shared by admin-only local workflows."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path


class FolderPickerCancelled(ValueError):
    """Raised when the native folder picker is cancelled by the user."""


class FolderPickerUnavailable(RuntimeError):
    """Raised when no native folder picker is available on this host."""


def choose_directory_with_native_dialog(prompt: str) -> str:
    """Open a native directory picker on the server host and return the selected path."""
    system = platform.system()
    if system == "Darwin":
        script = (
            "POSIX path of (choose folder with prompt "
            f"{_applescript_string(prompt)})"
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if result.returncode == 1 and "-128" in (result.stderr or ""):
            raise FolderPickerCancelled("Folder selection was cancelled.")
        raise RuntimeError((result.stderr or "macOS folder picker failed.").strip())

    if system == "Windows":
        escaped_prompt = prompt.replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '{escaped_prompt}'
$dialog.ShowNewFolderButton = $false
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
    Write-Output $dialog.SelectedPath
}} else {{
    Write-Output '__CANCELLED__'
}}
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "Windows folder picker failed.").strip())
        selected = result.stdout.strip()
        if not selected or selected == "__CANCELLED__":
            raise FolderPickerCancelled("Folder selection was cancelled.")
        return selected

    for command in (
        ["zenity", "--file-selection", "--directory", "--title", prompt],
        ["kdialog", "--getexistingdirectory", str(Path.home())],
    ):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except FileNotFoundError:
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode in {1, 2}:
            raise FolderPickerCancelled("Folder selection was cancelled.")
        raise RuntimeError((result.stderr or "Linux folder picker failed.").strip())

    raise FolderPickerUnavailable(
        "Native folder picker is not available on this server. Enter the folder path manually."
    )


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
