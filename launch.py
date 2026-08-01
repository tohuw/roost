"""
launch.py

Dedicated-window support for Appistry-owned launches.

Yggdrasil apps can present their loopback web UI in a dedicated native window
(via the shared ``ygg_shell.py`` pywebview host) instead of a browser tab. This
is opt-in; the default remains the browser.

When Appistry starts an app it mints a per-launch secret and injects it into the
app server's environment (see ``process.start``). Appistry then owns the window:
it reads that same persisted secret back and opens ``ygg_shell.py`` from its own
interpreter, so apps do not each need their own Python/pywebview. Apps that run
standalone (no Appistry) keep falling back to their own launcher/shell.

This module is deliberately dependency-light and importable by ``appistry.py``,
``menubar.py`` and ``windows_tray.py`` without introducing an import cycle.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import process
import registry
from registry import AppEntry

log = logging.getLogger(__name__)

# ygg_shell.py exits with this code when no OS webview backend is available, so
# the caller can fall back to the browser rather than leaving the user with no UI.
_NO_BACKEND_EXIT = 3

# Icon file types ygg_shell can present as a native window/app icon.
_WINDOW_ICON_TYPES = {".png", ".icns", ".ico", ".jpg", ".jpeg", ".gif", ".bmp"}


def resolve_launch_mode() -> str:
    """Decide how a launch presents the UI.

    Resolution order (first match wins):
      1. ``YGG_LAUNCH_MODE`` environment variable.
      2. First line of ``~/.yggdrasil/launch-mode``.
      3. Default ``"shell"``.

    ``"shell"`` opens the dedicated native window; any other value keeps the
    browser behavior. Matches the per-app resolution already used by
    standalone apps so a single ``~/.yggdrasil/launch-mode``
    toggles both Appistry-owned and standalone paths.
    """
    env_mode = os.environ.get("YGG_LAUNCH_MODE", "").strip().lower()
    if env_mode:
        return env_mode
    try:
        mode_file = Path.home() / ".yggdrasil" / "launch-mode"
        if mode_file.is_file():
            lines = mode_file.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip():
                return lines[0].strip().lower()
    except OSError:
        pass
    return "shell"


def is_shell_mode() -> bool:
    """Return True when the resolved launch mode is the dedicated window."""
    return resolve_launch_mode() == "shell"


def _appistry_dir() -> Path:
    return Path(__file__).resolve().parent


def _shell_interpreter() -> str:
    """Return a Python interpreter that can run Appistry's ``ygg_shell.py``.

    Prefer Appistry's own virtualenv Python (where pywebview is installed) so the
    window always has a working backend, regardless of how Appistry itself was
    invoked. Fall back to ``sys.executable``.
    """
    base = _appistry_dir()
    if sys.platform == "win32":
        venv_python = base / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = base / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def _resolve_window_icon(entry: AppEntry) -> Path | None:
    """Return a safe, existing icon path for entry, or None.

    Mirrors the containment checks used elsewhere: a relative icon must stay
    under the app's cwd and carry a supported image extension. Absolute icon
    paths are honored as-is (the publisher registered them) but must exist.
    """
    if not entry.icon:
        return None
    icon = Path(entry.icon)
    if icon.suffix.lower() not in _WINDOW_ICON_TYPES:
        return None
    if icon.is_absolute():
        return icon if icon.is_file() else None
    try:
        base = Path(entry.cwd).resolve()
        resolved = (base / icon).resolve()
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


def _fallback_to_browser(entry: AppEntry) -> None:
    webbrowser.open(f"http://localhost:{entry.port}")


def open_dedicated_window(
    entry: AppEntry,
    secret_mode: str = "fragment",
    block: bool = False,
) -> int:
    """Open ``entry``'s loopback UI in a dedicated native window via ygg_shell.

    Appistry owns the window: it reads the persisted per-launch secret (minted by
    ``process.start``) and passes it to the shell, which carries it to the app as
    a URL fragment (default) or ``?ygg_launch=`` query (``secret_mode="query"``,
    for apps whose server reads the secret off the document request, such as a
    prebuilt SPA).

    ``block=True`` waits for the window to close (used by the ``window`` CLI
    command); ``block=False`` returns immediately after spawning and watches for
    the no-backend fallback on a daemon thread (used by the tray menus so the UI
    loop is never blocked).

    Returns the shell exit code when blocking (or 0 when spawned non-blocking /
    a browser fallback was used), and 1 if the shell could not be spawned at all.
    """
    shell_script = _appistry_dir() / "ygg_shell.py"
    if not shell_script.is_file():
        log.warning("ygg_shell.py not found at %s — falling back to browser", shell_script)
        _fallback_to_browser(entry)
        return 0

    cmd = [
        _shell_interpreter(),
        str(shell_script),
        "--url", f"http://127.0.0.1:{entry.port}",
        "--title", entry.name,
        "--no-activate",
    ]
    if secret_mode == "query":
        cmd.append("--secret-in-query")
    icon = _resolve_window_icon(entry)
    if icon is not None:
        cmd += ["--icon", str(icon)]

    # Hand the persisted per-launch secret to the child via its environment.
    # The shell reads YGG_LAUNCH_SECRET and appends it to the URL. A copy is used
    # so Appistry's own environment is never mutated by opening a window.
    env = os.environ.copy()
    secret = process.read_launch_secret(entry.id)
    if secret:
        env["YGG_LAUNCH_SECRET"] = secret
    else:
        # No secret persisted (e.g. app started outside Appistry). The window
        # still opens; the server simply stays in its permissive/browser mode.
        env.pop("YGG_LAUNCH_SECRET", None)

    try:
        proc = subprocess.Popen(cmd, env=env)
    except OSError as exc:
        log.warning("Could not launch dedicated window (%s) — falling back to browser", exc)
        _fallback_to_browser(entry)
        return 1

    def _wait_and_fallback() -> int:
        code = proc.wait()
        if code == _NO_BACKEND_EXIT:
            log.info("ygg_shell reported no webview backend — opening browser instead")
            _fallback_to_browser(entry)
        return code

    if block:
        return _wait_and_fallback()

    threading.Thread(
        target=_wait_and_fallback,
        name=f"appistry-window-{entry.id}",
        daemon=True,
    ).start()
    return 0
