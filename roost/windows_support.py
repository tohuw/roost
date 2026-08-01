"""Windows installation, startup, environment, and tray helpers.

What is left here is only what a *status* tray needs: install and remove the two
Roost shortcuts (login startup and Start Menu), keep the process's environment
in step with the user's Environment registry values, and start or stop the tray
process. There are no per-app shortcuts and no icon conversion for them, because
there are no apps to launch — the ravens are long-running daemons that publish
descriptors, and the tray reports on them rather than starting them.

Windows-only packages are imported inside functions on purpose, so the shared
unit suite can exercise the pure path and argument logic on any platform.
"""

from __future__ import annotations

import logging
import ntpath
import os
import subprocess
import sys
import time
from pathlib import Path

from roost import help_server
from roost import paths

_STARTUP_SHORTCUT = "Roost.lnk"
_TRAY_PID_FILE = "windows-tray.pid"

#: How the tray process is launched, and therefore the token ``stop_tray`` looks
#: for in a candidate command line.
#:
#: ``-m roost.windows_tray`` rather than a path to ``windows_tray.py`` does double
#: duty. It is required — the tray is a package module now — and it also makes the
#: PID verification here mutually exclusive with the separate internal Appistry's,
#: which looks for a bare ``windows_tray.py`` argument. Both projects ship a file
#: by that name, so a recycled PID landing in either tool's PID file could
#: otherwise have made one terminate the other's tray. Neither tool's check
#: matches the other's command line now.
TRAY_MODULE = "roost.windows_tray"
_TRAY_ARGV = ("-m", TRAY_MODULE)
_WINDOWS_CREATE_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)
_managed_environment: dict[str, str] = {}
log = logging.getLogger(__name__)


def is_windows() -> bool:
    return sys.platform == "win32"


def _require_windows() -> None:
    if not is_windows():
        raise RuntimeError("This operation is only available on Windows")


def _environment_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else fallback


def start_menu_programs_dir() -> Path:
    appdata = _environment_path("APPDATA", Path.home() / "AppData" / "Roaming")
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def startup_dir() -> Path:
    return start_menu_programs_dir() / "Startup"


def shortcuts_dir() -> Path:
    return start_menu_programs_dir() / "Roost"


def tray_pid_path() -> Path:
    return paths.STATE_DIR / _TRAY_PID_FILE


def write_tray_pid() -> None:
    """Record the tray's PID, owner-only.

    Only ``stop_tray`` reads this, and it verifies the command line before
    signalling — so the file is a hint, not an authority. It is still written
    0600: on a shared machine another local user has no business enumerating
    which of this user's processes is the tray.
    """
    paths.atomic_write_text(tray_pid_path(), str(os.getpid()))


def _venv_executable(repo_dir: Path, *, windowed: bool = False) -> Path:
    name = "pythonw.exe" if windowed else "python.exe"
    return repo_dir / ".venv" / "Scripts" / name


def _shortcut_arguments(parts: list[str]) -> str:
    """Quote a trusted argv list with the Windows CreateProcess quoting rules."""
    return subprocess.list2cmdline(parts)


def _create_shortcut(
    path: Path,
    *,
    target: Path,
    arguments: str,
    working_directory: Path,
    description: str,
    icon: Path | None = None,
) -> Path:
    """Create a .lnk through WScript.Shell without invoking a command shell."""
    _require_windows()
    import win32com.client  # type: ignore[import-not-found]

    path.parent.mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(path))
    shortcut.TargetPath = str(target)
    shortcut.Arguments = arguments
    shortcut.WorkingDirectory = str(working_directory)
    shortcut.Description = description
    if icon is not None:
        shortcut.IconLocation = f"{icon},0"
    shortcut.Save()
    return path


def install_shortcuts(repo_dir: Path) -> tuple[Path, Path]:
    """Install the login-start and Start Menu shortcuts for the tray."""
    target = _venv_executable(repo_dir, windowed=True)
    args = _shortcut_arguments(list(_TRAY_ARGV))
    icon = prepare_tray_icon()
    startup = _create_shortcut(
        startup_dir() / _STARTUP_SHORTCUT,
        target=target,
        arguments=args,
        working_directory=repo_dir,
        description="Start Roost when you sign in",
        icon=icon,
    )
    menu = _create_shortcut(
        shortcuts_dir() / _STARTUP_SHORTCUT,
        target=target,
        arguments=args,
        working_directory=repo_dir,
        description="Open Roost",
        icon=icon,
    )
    return startup, menu


def prepare_tray_icon() -> Path | None:
    """Convert the configured tray icon to an ICO for the Windows shortcuts.

    A ``.lnk`` needs an ICO; the checked-in assets are PNG. The conversion is
    best-effort and returns None on any failure — a shortcut with the default
    Python icon is a cosmetic problem, while refusing to create the shortcut
    would mean the tray never starts at login.
    """
    from roost import icons

    choice = icons.resolve()
    if choice is None:
        return None
    if choice.path.suffix.lower() == ".ico":
        return choice.path
    destination = paths.ensure_state_dir() / "tray-icon.ico"
    try:
        from PIL import Image

        with Image.open(choice.path) as image:
            image.convert("RGBA").save(
                destination,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)],
            )
        return destination
    except (ImportError, OSError, ValueError):
        # ImportError included deliberately: Pillow is a Windows-only pin, and
        # this function is also reachable from the shared unit suite and from an
        # install that has not finished resolving dependencies. A missing Pillow
        # must degrade to "no custom shortcut icon", not abort the install.
        destination.unlink(missing_ok=True)
        return None


def uninstall_shortcuts() -> None:
    (startup_dir() / _STARTUP_SHORTCUT).unlink(missing_ok=True)
    (shortcuts_dir() / _STARTUP_SHORTCUT).unlink(missing_ok=True)
    try:
        shortcuts_dir().rmdir()
    except OSError:
        log.debug("Roost Start Menu folder is not empty", exc_info=True)
    (paths.STATE_DIR / "tray-icon.ico").unlink(missing_ok=True)


# ── User PATH and environment ─────────────────────────────────────────────────

def _normalise_path_entry(value: str) -> str:
    expanded = os.path.expandvars(value.strip().strip('"'))
    return os.path.normcase(os.path.normpath(expanded))


def _path_contains(path_value: str, candidate: Path) -> bool:
    expected = _normalise_path_entry(str(candidate))
    return any(
        _normalise_path_entry(item) == expected
        for item in path_value.split(os.pathsep)
        if item.strip()
    )


def _broadcast_environment_change() -> None:
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]

        win32api.SendMessageTimeout(
            win32con.HWND_BROADCAST,
            win32con.WM_SETTINGCHANGE,
            0,
            "Environment",
            win32con.SMTO_ABORTIFHUNG,
            5000,
        )
    except Exception:
        log.debug("Could not broadcast Windows environment change", exc_info=True)


def add_cli_dir_to_user_path(bin_dir: Path) -> bool:
    """Add Roost's venv Scripts directory to the current user's PATH."""
    _require_windows()
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        if _path_contains(current, bin_dir):
            return False
        updated = os.pathsep.join(item for item in (current, str(bin_dir)) if item)
        winreg.SetValueEx(key, "Path", 0, value_type, updated)
    os.environ["PATH"] = os.pathsep.join(
        item for item in (os.environ.get("PATH", ""), str(bin_dir)) if item
    )
    _broadcast_environment_change()
    return True


def remove_cli_dir_from_user_path(bin_dir: Path) -> bool:
    _require_windows()
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return False
        expected = _normalise_path_entry(str(bin_dir))
        entries = [item for item in current.split(os.pathsep) if item.strip()]
        retained = [item for item in entries if _normalise_path_entry(item) != expected]
        if len(retained) == len(entries):
            return False
        winreg.SetValueEx(key, "Path", 0, value_type, os.pathsep.join(retained))
    _broadcast_environment_change()
    return True


def _read_registry_environment() -> dict[str, str]:
    _require_windows()
    import winreg

    values: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            count = winreg.QueryInfoKey(key)[1]
            for index in range(count):
                name, value, value_type = winreg.EnumValue(key, index)
                if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    values[name] = os.path.expandvars(str(value))
    except OSError:
        log.debug("Could not read the user Environment registry key", exc_info=True)
    return values


def _read_registry_path() -> str:
    """Return the effective machine + user PATH stored in the Windows registry."""
    _require_windows()
    import winreg

    entries: list[str] = []
    locations = (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    )
    for hive, location in locations:
        try:
            with winreg.OpenKey(hive, location) as key:
                value, value_type = winreg.QueryValueEx(key, "Path")
                if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and value:
                    entries.append(os.path.expandvars(str(value)))
        except OSError:
            continue
    return os.pathsep.join(entries)


def refresh_user_environment() -> None:
    """Pick up user Environment registry changes without restarting the tray.

    A raven's descriptor directory can be relocated with ``RAVENS_STATE_DIR``. If
    the user sets that after the tray started, the tray would keep watching the
    old location until the next sign-in, so the value is re-read here.
    """
    if not is_windows():
        return
    current = _read_registry_environment()
    for key, previous in tuple(_managed_environment.items()):
        if key.casefold() == "path":
            continue
        if key not in current and os.environ.get(key) == previous:
            os.environ.pop(key, None)
            _managed_environment.pop(key, None)
    for key, value in current.items():
        if key.casefold() == "path":
            continue
        if (
            key in _managed_environment
            or key not in os.environ
            or os.environ.get(key) == value
        ):
            os.environ[key] = value
            _managed_environment[key] = value
    effective_path = _read_registry_path()
    if effective_path:
        os.environ["PATH"] = effective_path
        _managed_environment["PATH"] = effective_path


# ── Tray process ──────────────────────────────────────────────────────────────

def tray_is_running() -> bool:
    """Return True if a tray process is answering on its recorded help port.

    The port file alone proves nothing — a crashed tray leaves it behind — so the
    endpoint is actually probed, and the reply must identify Roost rather than
    whatever unrelated service inherited the port.
    """
    import json
    import urllib.error
    import urllib.request

    port = help_server.active_port()
    if port is None:
        return False
    try:
        # Fixed loopback URL; the port was range-checked by active_port().
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=0.5
        ) as response:
            payload = json.loads(response.read(1024))
        return payload == {"service": "roost", "ok": True}
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False


def start_tray(repo_dir: Path, *, wait: bool = True) -> bool:
    """Start the Windows tray without a console window."""
    _require_windows()
    if tray_is_running():
        return True
    target = _venv_executable(repo_dir, windowed=True)
    try:
        # The interpreter path derives from this trusted installation directory,
        # and cwd is what makes the package importable under -m.
        proc = subprocess.Popen(
            [str(target), *_TRAY_ARGV],
            cwd=str(repo_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_WINDOWS_CREATE_FLAGS,
        )
    except OSError:
        return False
    if wait:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if tray_is_running():
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.1)
        _terminate_spawned_process(proc)
        return False
    return True


def _terminate_spawned_process(proc) -> None:
    """Terminate a tray child that never established its help endpoint."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except OSError:
            log.debug("Could not reap failed Windows tray process", exc_info=True)
    except OSError:
        log.debug("Could not terminate failed Windows tray process", exc_info=True)


def stop_tray() -> bool:
    """Stop only a verified Roost tray process.

    The PID comes from a file, so it is not trusted: the command line is checked
    before anything is signalled. A PID file is also the one place a recycled PID
    can do real damage, so a non-positive value is refused outright — ``-1``
    would address every process this user can signal.
    """
    path = tray_pid_path()
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        import psutil
    except ImportError:
        # Without psutil there is no way to confirm the PID names the tray, and
        # signalling an unverified PID is exactly the mistake this guards.
        log.warning("psutil is unavailable; refusing to signal an unverified PID")
        return False
    if pid <= 0:
        log.warning("Refusing a non-positive PID in the tray PID file")
        path.unlink(missing_ok=True)
        return False
    try:
        proc = psutil.Process(pid)
        # ntpath, not pathlib: these are always Windows command lines, and
        # pathlib on a POSIX host does not treat a backslash as a separator — so
        # the whole argument would come back as the "filename" and the check
        # would silently never match. Using ntpath keeps this verification
        # exercisable by the shared unit suite, which is the point of the
        # module's platform-neutral logic.
        command = [ntpath.basename(part).casefold() for part in proc.cmdline()]
        # The dotted module name, not a bare filename: see TRAY_MODULE. This is
        # what keeps the check from ever matching the other Appistry's tray.
        if TRAY_MODULE not in command:
            path.unlink(missing_ok=True)
            return False
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
    except psutil.NoSuchProcess:
        path.unlink(missing_ok=True)
        return True
    except psutil.AccessDenied:
        return False
