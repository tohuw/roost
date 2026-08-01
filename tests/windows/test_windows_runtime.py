"""Windows-host integration smoke tests for native Appistry behavior."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import process
import registry
import windows_support
from registry import AppEntry


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows host required")


def _entry(tmp_path: Path, command: str) -> AppEntry:
    return AppEntry(
        id="windows-smoke",
        name="Windows Smoke",
        cwd=str(tmp_path),
        command=command,
        port=8765,
        github_url="https://github.com/example/windows-smoke",
    )


def test_real_start_menu_shortcut_round_trip(tmp_path, monkeypatch):
    """Create and inspect a real WScript.Shell shortcut without touching the profile."""
    import win32com.client

    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr(registry, "APPISTRY_DIR", tmp_path / ".appistry")
    appistry_dir = tmp_path / "Appistry Home"
    appistry_dir.mkdir()
    entry = _entry(tmp_path, "unused.exe")

    shortcut_path = windows_support.build_registered_shortcut(entry, appistry_dir)

    assert shortcut_path.is_file()
    shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(
        str(shortcut_path)
    )
    expected_target = appistry_dir / ".venv" / "Scripts" / "pythonw.exe"
    assert os.path.normcase(os.path.abspath(shortcut.TargetPath)) == os.path.normcase(
        os.path.abspath(expected_target)
    )
    assert "appistry.py" in shortcut.Arguments
    assert shortcut.Arguments.endswith("launch windows-smoke")


def test_named_mutex_enforces_single_windows_tray_instance():
    name = rf"Local\AppistryWindowsSmoke-{uuid.uuid4()}"
    first = windows_support.NamedMutex(name)
    second = windows_support.NamedMutex(name)
    after_release = windows_support.NamedMutex(name)

    try:
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert after_release.acquire() is True
    finally:
        first.release()
        second.release()
        after_release.release()


def test_userprofile_redirects_path_home_in_real_windows_child(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["APPDATA"] = str(tmp_path / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(tmp_path / "AppData" / "Local")

    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_real_windows_process_lifecycle(tmp_path, monkeypatch):
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    command = subprocess.list2cmdline([sys.executable, str(sleeper)])
    entry = _entry(tmp_path, command)
    state_dir = tmp_path / ".appistry"
    monkeypatch.setattr(process, "APPISTRY_DIR", state_dir)
    monkeypatch.setattr(process, "PIDS_DIR", state_dir / "pids")
    monkeypatch.setattr(process, "SECRETS_DIR", state_dir / "secrets")

    try:
        assert process.start(entry) is True
        assert process.is_running(entry.id) is True
        assert process.stop(entry.id) is True
        assert process.is_running(entry.id) is False
    finally:
        if process.is_running(entry.id):
            process.stop(entry.id)
