"""
Environment tests for Appistry.

Verifies the installation works — real dependencies, no mocks. Starts the
actual menubar help server (or reuses one already running on this machine),
hits it over real HTTP, and verifies clean shutdown.

Run: pytest tests/env/ -v
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

APP_DIR      = Path(__file__).resolve().parents[2]
VENV_PYTHON  = (
    APP_DIR / ".venv" / "Scripts" / "python.exe"
    if sys.platform == "win32"
    else APP_DIR / ".venv" / "bin" / "python"
)
PORT_FILE    = Path.home() / ".appistry" / "menubar-http-port"

# The subprocess entry point for a fresh, isolated help-server instance.
# Runs menubar's real server start/shutdown functions in-process — no signal
# plumbing needed since they're plain function calls, not a CLI.
_SERVER_SCRIPT = """
import sys, time
sys.path.insert(0, {app_dir!r})
import menubar

port = menubar._help_server_start()
print(f"PORT {{port}}", flush=True)
try:
    while True:
        time.sleep(0.2)
except KeyboardInterrupt:
    pass
finally:
    menubar._help_server_shutdown()
"""


def _get(url: str, timeout: float = 5.0, headers: dict | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, resp.read()


def _isolated_process_env(isolated_home: Path, *, platform: str = sys.platform) -> dict:
    """Return an environment whose profile paths cannot reach the real user."""
    env = os.environ.copy()
    env["HOME"] = str(isolated_home)
    if platform == "win32":
        env["USERPROFILE"] = str(isolated_home)
        env["APPDATA"] = str(isolated_home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(isolated_home / "AppData" / "Local")
    return env


def _running_instance() -> str | None:
    """Return the URL of an already-running Appistry help server, if any.

    Probing first (per tq-2 rule #2) avoids both a bind conflict — Appistry's
    platform-native instance guard means a second live tray process can't start
    anyway, and clobbering the real ~/.appistry/menubar-http-port that a
    live instance depends on for `appistry open`.
    """
    if not PORT_FILE.exists():
        return None
    try:
        port = int(PORT_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    url = f"http://127.0.0.1:{port}"
    try:
        status, _ = _get(url, timeout=1.0)
        return url if status == 200 else None
    except Exception:
        return None


@pytest.fixture(scope="module")
def help_server_url(tmp_path_factory):
    """
    Probe for a running instance first; only spawn a new one if needed.

    A spawned instance runs with an isolated HOME so it never touches the
    real ~/.appistry state (registry, PID files, or the live port file) of
    whatever Appistry installation happens to be running on this machine.
    """
    existing = _running_instance()
    if existing:
        yield existing
        return

    isolated_home = tmp_path_factory.mktemp("appistry-home")
    env = _isolated_process_env(isolated_home)

    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-u", "-c", _SERVER_SCRIPT.format(app_dir=str(APP_DIR))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )

    port = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"Help server process exited early: {proc.stderr.read()}"
            )
        line = proc.stdout.readline()
        if line.startswith("PORT "):
            port = int(line.split()[1])
            break
    if port is None:
        proc.kill()
        raise AssertionError("Appistry help server did not report a port in time")

    url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            status, _ = _get(url, timeout=1.0)
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        proc.kill()
        raise AssertionError("Appistry help server did not become ready in time")

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("Appistry help server did not shut down cleanly")


# ── Installation checks (no server needed) ────────────────────────────────

def test_venv_exists():
    """The virtual environment must be present."""
    assert VENV_PYTHON.exists(), (
        f"No venv at {VENV_PYTHON}. "
        "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    )


def test_windows_subprocess_env_isolates_all_profile_paths(tmp_path):
    env = _isolated_process_env(tmp_path, platform="win32")

    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)
    assert env["APPDATA"] == str(tmp_path / "AppData" / "Roaming")
    assert env["LOCALAPPDATA"] == str(tmp_path / "AppData" / "Local")


def test_core_imports():
    """The active platform's tray and shared runtime dependencies import."""
    platform_imports = (
        "import pystray, PIL, win32com.client, psutil, nh3"
        if sys.platform == "win32"
        else "import rumps, nh3"
    )
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", platform_imports],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Core import failed: {result.stderr.strip()}\n"
        "Run: .venv/bin/pip install -r requirements.txt"
    )


def test_native_search_field_constructs():
    """The real AppKit search control used by the menu can be constructed."""
    if sys.platform == "win32":
        pytest.skip("Windows uses the tkinter search window instead of AppKit")
    script = """
import menubar
controller = menubar._SearchFieldController.alloc().initWithApp_(None)
item, field = menubar._make_search_menu_item(controller, "san")
assert field.stringValue() == "san"
assert field.placeholderString() == "Search running apps"
assert field.accessibilityLabel() == "Search running apps"
assert str(field.action()) == "search:"
assert field.delegate() == controller
assert item._menuitem.view() is not None
"""
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", script],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows tray smoke test")
def test_windows_tray_runtime_imports():
    """The real Windows tray entry point imports with installed dependencies."""
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", "import menubar, windows_support, windows_tray"],
        capture_output=True,
        text=True,
        cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


def test_registry_module_importable():
    """registry.py and process.py import cleanly from the venv."""
    result = subprocess.run(
        [str(VENV_PYTHON), "-c",
         "import sys; sys.path.insert(0, '.'); import registry, process"],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


def test_cli_help_exits_zero():
    """appistry.py --help exits 0 and does not crash."""
    result = subprocess.run(
        [str(VENV_PYTHON), str(APP_DIR / "appistry.py"), "--help"],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


def test_cli_list_exits_without_crash():
    """appistry list exits 0 (empty registry is valid)."""
    result = subprocess.run(
        [str(VENV_PYTHON), str(APP_DIR / "appistry.py"), "list"],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


# ── Real server lifecycle ──────────────────────────────────────────────────

def test_server_starts_and_root_responds(help_server_url):
    """The real help server starts and its root page responds HTTP 200."""
    status, body = _get(help_server_url)
    assert status == 200
    assert b"Appistry Help" in body


def test_server_identifies_appistry_control_service(help_server_url):
    """The status endpoint distinguishes Appistry from an unrelated stale-port service."""
    status, body = _get(f"{help_server_url}/api/status")
    assert status == 200
    assert json.loads(body) == {"service": "appistry", "ok": True}


# ── Graceful shutdown ───────────────────────────────────────────────────────

def test_server_still_alive_before_teardown(help_server_url):
    """
    The server must still be reachable at this point in the suite — proves
    it did not crash mid-run. Clean shutdown itself is verified by the
    help_server_url fixture's teardown (subprocess exits without needing
    SIGKILL) when this test module owns the spawned instance.
    """
    status, _ = _get(help_server_url)
    assert status == 200
