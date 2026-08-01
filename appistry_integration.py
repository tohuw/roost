"""
appistry_integration.py

Drop this file into your project and call `setup_appistry()` from your
first-run or setup flow. Adjust the APP_* constants for your app.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Configure these for your app ──────────────────────────────────────────────
# CONFIGURE BEFORE USE — these are example defaults. Change all four values to
# match your app before calling register() or setup_appistry().

APP_NAME    = "Appistry"
APP_COMMAND = (
    ".venv\\Scripts\\python.exe appistry.py"
    if sys.platform == "win32"
    else ".venv/bin/python appistry.py"
)
APP_PORT    = 0                        # Appistry has no web UI; set to your app's port
APP_ICON    = "appistry_icon.png"      # browser-renderable path relative to APP_CWD
APP_CWD     = Path(__file__).resolve().parent  # project root

APPISTRY_REPO    = "https://github.com/tohuw/appistry"
APPISTRY_PATH    = Path(os.environ["APPISTRY_HOME"]) if os.environ.get("APPISTRY_HOME") \
                   else Path.home() / ".local" / "share" / "appistry"
APPISTRY_BINARY  = (
    APPISTRY_PATH / ".venv" / "Scripts" / "appistry.exe"
    if sys.platform == "win32"
    else APPISTRY_PATH / "appistry"
)

# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_appistry() -> Path | None:
    """Return the path to the appistry binary, or None if not found."""
    on_path = shutil.which("appistry")
    if on_path:
        return Path(on_path)
    if APPISTRY_BINARY.exists():
        return APPISTRY_BINARY
    return None


def _looks_like_appistry_install(path: Path) -> bool:
    """True if path looks like a prior Appistry clone, not an unrelated directory."""
    return all(
        (path / marker).exists()
        for marker in (".git", "appistry", "appistry.py")
    )


def _clone_appistry() -> bool:
    """Clone the Appistry repo.

    APPISTRY_HOME is environment-controlled and may point at any existing
    directory (including the user's home directory), so we never recursively
    delete it — only an existing path that already looks like an Appistry
    checkout is treated as an idempotent success. Anything else refuses.
    """
    if APPISTRY_PATH.exists():
        if _looks_like_appistry_install(APPISTRY_PATH):
            return True
        print(
            f"  Refusing to replace existing non-Appistry path: {APPISTRY_PATH}",
            file=sys.stderr,
        )
        return False
    print("  Cloning Appistry…")
    result = subprocess.run(
        ["git", "clone", APPISTRY_REPO, str(APPISTRY_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  Clone failed: {result.stderr.strip()}")
        return False
    return True


def _install_appistry() -> bool:
    """Run appistry install. Returns True on success."""
    installer = (
        [sys.executable, str(APPISTRY_PATH / "appistry.py"), "install"]
        if sys.platform == "win32"
        else [str(APPISTRY_BINARY), "install"]
    )
    result = subprocess.run(
        installer,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  Appistry install failed: {result.stderr.strip()}")
        return False
    print("  Appistry installed and running.")
    return True


# ── Public API ────────────────────────────────────────────────────────────────

def offer_install(auto: bool = False) -> bool:
    """
    Install Appistry if it isn't present.

    If `auto` is True, installs silently without prompting. If False (default),
    asks the user first. Returns True if Appistry is available after this call,
    False if the user declined or installation failed.
    """
    if _find_appistry():
        return True

    if not auto:
        print()
        print("Appistry is not installed.")
        print("It provides a desktop tray launcher so you can start and stop")
        print(f"{APP_NAME} without a terminal window, with a branded wait page while it starts.")
        print()
        print(f"Install it now? (clones {APPISTRY_REPO} to {APPISTRY_PATH})")
        answer = input("  [y/N] ").strip().lower()
        if answer != "y":
            print(f"Skipping Appistry. You can install it later by running:")
            print(f"  git clone {APPISTRY_REPO} {APPISTRY_PATH}")
            if sys.platform == "win32":
                print(f'  "{sys.executable}" "{APPISTRY_PATH / "appistry.py"}" install')
            else:
                print(f"  {APPISTRY_BINARY} install")
            return False

    print("  Installing Appistry…")
    if not _clone_appistry():
        return False
    if not _install_appistry():
        return False
    return True


def register(port: int | None = None) -> bool:
    """
    Register this app with Appistry.

    Safe to call on every launch — registration is idempotent. Pass `port`
    to override APP_PORT (useful when the app hunts a free port at startup).
    Appistry uses the current registered port and APP_ICON for its launch
    wait page before redirecting the browser to the app.
    Returns True on success, False if Appistry is unavailable or registration
    failed. Either way, the caller should continue normally.
    """
    binary = _find_appistry()
    if not binary:
        return False

    effective_port = port if port is not None else APP_PORT
    cmd = [
        str(binary), "register",
        "--name",    APP_NAME,
        "--cwd",     str(APP_CWD),
        "--command", APP_COMMAND,
        "--port",    str(effective_port),
    ]
    if APP_ICON:
        cmd += ["--icon", APP_ICON]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        msg = result.stderr.strip()
        print(f"  Appistry registration failed: {msg}")
        if "GitHub remote" in msg:
            print("  Add a GitHub remote with: git remote add origin <url>")
        return False
    return True


def setup_appistry(cwd: str | None = None, auto: bool = False) -> None:
    """
    Full setup flow: install if needed, then register.

    Pass `auto=True` to install Appistry without prompting (use when your
    app requires Appistry). Pass `cwd` to override APP_CWD for registration.
    """
    global APP_CWD
    if cwd is not None:
        APP_CWD = Path(cwd)
    if not offer_install(auto=auto):
        return   # user declined or install failed — soft skip
    register()
