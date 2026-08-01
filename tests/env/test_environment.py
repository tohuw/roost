"""Environment tests: the real installation, with real dependencies.

Everything else in the suite is hermetic. This module verifies that the venv
actually resolves the platform's tray dependencies and that the real help server
starts, answers over real HTTP, and shuts down cleanly — the things a mock cannot
tell you.

Run: pytest tests/env -v
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

APP_DIR = Path(__file__).resolve().parents[2]
VENV_PYTHON = (
    APP_DIR / ".venv" / "Scripts" / "python.exe"
    if sys.platform == "win32"
    else APP_DIR / ".venv" / "bin" / "python"
)
PORT_FILE = Path.home() / ".appistry" / "menubar-http-port"

# A fresh, isolated help-server instance. The module's start/shutdown are plain
# function calls, so no signal plumbing is needed.
_SERVER_SCRIPT = """
import sys, time
sys.path.insert(0, {app_dir!r})
import help_server

port = help_server.start()
print(f"PORT {{port}}", flush=True)
try:
    while True:
        time.sleep(0.2)
except KeyboardInterrupt:
    pass
finally:
    help_server.shutdown()
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

    Probing first avoids clobbering the real ``~/.appistry/menubar-http-port``
    that a live tray depends on — and the host lock means a second tray could not
    start anyway.
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
    """Probe for a running instance first; only spawn a new one if needed.

    A spawned instance runs with an isolated HOME so it never touches the real
    ``~/.appistry`` state of whatever installation is running on this machine.
    """
    existing = _running_instance()
    if existing:
        yield existing
        return

    env = _isolated_process_env(tmp_path_factory.mktemp("appistry-home"))
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
        raise AssertionError("The help server did not report a port in time")

    url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            if _get(url, timeout=1.0)[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        proc.kill()
        raise AssertionError("The help server did not become ready in time")

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("The help server did not shut down cleanly")


# ── Installation ─────────────────────────────────────────────────────────────

def test_venv_exists():
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
        [str(VENV_PYTHON), "-c", platform_imports], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"Core import failed: {result.stderr.strip()}\n"
        "Run: .venv/bin/pip install -r requirements.txt"
    )


def test_the_shared_raven_layer_imports():
    """Every module both trays depend on must import with no display present."""
    result = subprocess.run(
        [str(VENV_PYTHON), "-c",
         "import sys; sys.path.insert(0, '.'); "
         "import ravens, menu_spec, raven_client, host, icons, paths, "
         "sanitize, tray, help_server"],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


def test_the_real_tray_module_imports():
    """The platform's tray entry point imports with installed dependencies."""
    modules = (
        "import windows_support, windows_tray"
        if sys.platform == "win32"
        else "import menubar"
    )
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", modules],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


def test_the_real_tray_icon_loads():
    """The checked-in asset must actually decode: nothing rasterizes at runtime."""
    script = (
        "import sys; sys.path.insert(0, '.'); import icons\n"
        "choice = icons.resolve()\n"
        "assert choice is not None, 'no icon resolved'\n"
        "assert choice.path.is_file(), choice.path\n"
        "assert choice.path.stat().st_size > 0\n"
    )
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", script],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


def test_cli_help_exits_zero():
    result = subprocess.run(
        [str(VENV_PYTHON), str(APP_DIR / "appistry.py"), "--help"],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    assert result.returncode == 0, result.stderr.strip()


def test_cli_ravens_exits_without_crash(tmp_path):
    """An empty (or absent) descriptor directory is a valid, reportable state."""
    env = _isolated_process_env(tmp_path)
    env["RAVENS_STATE_DIR"] = str(tmp_path / "ravens")
    result = subprocess.run(
        [str(VENV_PYTHON), str(APP_DIR / "appistry.py"), "ravens"],
        capture_output=True, text=True, cwd=str(APP_DIR), env=env,
    )
    assert result.returncode == 0, result.stderr.strip()
    assert "Descriptor directory" in result.stdout


# ── Real server lifecycle ────────────────────────────────────────────────────

def test_server_starts_and_root_responds(help_server_url):
    status, body = _get(help_server_url)
    assert status == 200
    assert b"Appistry Help" in body


def test_server_identifies_appistry(help_server_url):
    """Distinguishes a live tray from an unrelated service on a stale port."""
    status, body = _get(f"{help_server_url}/api/status")
    assert status == 200
    assert json.loads(body) == {"service": "appistry", "ok": True}


def test_the_real_server_rejects_a_foreign_host(help_server_url):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get(help_server_url, headers={"Host": "attacker.example.com"})
    assert exc_info.value.code == 400
    exc_info.value.close()


def test_the_real_server_rejects_any_origin(help_server_url):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get(help_server_url, headers={"Origin": "https://evil.example"})
    assert exc_info.value.code == 403
    exc_info.value.close()


def test_server_still_alive_before_teardown(help_server_url):
    """Proves the server did not crash mid-run.

    Clean shutdown is verified by the fixture's teardown (the subprocess exits
    without needing SIGKILL) when this module owns the spawned instance.
    """
    assert _get(help_server_url)[0] == 200
