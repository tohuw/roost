"""Windows-host integration smoke tests for the real native behaviour.

These need a real Windows session and real COM/pystray, so they are skipped
everywhere else. They cover the two things the hermetic suite cannot: a shortcut
really round-trips through WScript.Shell, and the host lock really excludes a
second tray process on this platform's locking primitive.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import host
from roost import paths
from roost import windows_support

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows host required")


def test_real_startup_shortcut_round_trip(tmp_path, monkeypatch):
    """Create and inspect a real WScript.Shell shortcut without touching the profile."""
    import win32com.client

    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "roost")
    repo_dir = tmp_path / "Roost Home"
    repo_dir.mkdir()

    startup, menu = windows_support.install_shortcuts(repo_dir)

    assert startup.is_file()
    assert menu.is_file()
    shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(startup))
    expected_target = repo_dir / ".venv" / "Scripts" / "pythonw.exe"
    assert os.path.normcase(os.path.abspath(shortcut.TargetPath)) == os.path.normcase(
        os.path.abspath(expected_target)
    )
    assert windows_support.TRAY_MODULE in shortcut.Arguments
    assert "windows_tray.py" not in shortcut.Arguments


def test_the_host_lock_excludes_a_second_tray(tmp_path):
    """One process draws the menu; the second must be refused, not crash."""
    path = tmp_path / "roost.lock"
    first, second = host.HostLock(path), host.HostLock(path)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        assert second.failure == host.CONTENDED
        first.release()
        assert second.acquire() is True
    finally:
        first.release()
        second.release()


def test_the_host_lock_is_released_when_the_holder_exits(tmp_path):
    """The OS drops the lock on process death, so there is no stale-lock case."""
    path = tmp_path / "roost.lock"
    script = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "from pathlib import Path\n"
        "from roost import host\n"
        f"lock = host.HostLock(Path({str(path)!r}))\n"
        "assert lock.acquire() is True\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)

    lock = host.HostLock(path)
    try:
        assert lock.acquire() is True
    finally:
        lock.release()


def test_the_real_tray_image_decodes(tmp_path, monkeypatch):
    """pystray needs a decoded bitmap; the checked-in colour PNG must load."""
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "roost")
    from roost import windows_tray

    image = windows_tray._tray_image()

    assert image.mode == "RGBA"
    assert image.width > 0 and image.height > 0


def test_userprofile_redirects_path_home_in_a_real_child(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["APPDATA"] = str(tmp_path / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(tmp_path / "AppData" / "Local")

    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        check=True, capture_output=True, text=True, env=env,
    )

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_a_windows_descriptor_directory_resolves_under_localappdata(tmp_path, monkeypatch):
    """The path rule is the contract both birds follow, so pin it on the real OS."""
    from roost import birds

    monkeypatch.delenv("BIRDS_STATE_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    assert birds.state_dir() == tmp_path / "AppData" / "Local" / "Birds"
