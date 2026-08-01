"""Windows installation, shortcut, environment, and tray helpers for Appistry.

This module deliberately imports Windows-only packages inside functions so the
shared unit suite can import and exercise its pure path/argument logic on macOS.
"""

from __future__ import annotations

import json
import logging
import os
# Process launches in this module use fixed local executables and argv lists.
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import registry
from registry import AppEntry


_STARTUP_SHORTCUT = "Appistry.lnk"
_TRAY_PID_FILE = "windows-tray.pid"
_CONTROL_PORT_FILE = "menubar-http-port"
_MAX_ICON_PIXELS = 16_777_216
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


def appistry_shortcuts_dir() -> Path:
    return start_menu_programs_dir() / "Appistry"


def registered_shortcut_path(entry: AppEntry) -> Path:
    """Return a contained Start Menu shortcut path for a registry entry."""
    safe_name = registry.bundle_name_for(entry.name, entry.id)
    safe_id = registry.validate_app_id(entry.id)
    base = appistry_shortcuts_dir().resolve()
    path = (base / f"{safe_name} ({safe_id}).lnk").resolve()
    path.relative_to(base)
    return path


def tray_pid_path() -> Path:
    return registry.APPISTRY_DIR / _TRAY_PID_FILE


def _venv_executable(appistry_dir: Path, *, windowed: bool = False) -> Path:
    name = "pythonw.exe" if windowed else "python.exe"
    return appistry_dir / ".venv" / "Scripts" / name


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


def _safe_icon_source(entry: AppEntry) -> Path | None:
    if not entry.icon:
        return None
    source = Path(entry.icon)
    if not source.is_absolute():
        base = Path(entry.cwd).resolve()
        source = (base / source).resolve()
        try:
            source.relative_to(base)
        except ValueError:
            return None
    else:
        source = source.resolve()
    if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}:
        return None
    try:
        if not source.is_file() or source.stat().st_size > 10 * 1024 * 1024:
            return None
    except OSError:
        return None
    return source


def _prepare_shortcut_icon(entry: AppEntry) -> Path | None:
    source = _safe_icon_source(entry)
    if source is None:
        return None
    icon_dir = registry.APPISTRY_DIR / "shortcut-icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    destination = icon_dir / f"{registry.validate_app_id(entry.id)}.ico"
    if source.suffix.lower() == ".ico":
        import shutil

        shutil.copy2(source, destination)
        return destination
    try:
        from PIL import Image

        with Image.open(source) as image:
            if image.width * image.height > _MAX_ICON_PIXELS:
                return None
            image.convert("RGBA").save(
                destination,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)],
            )
        return destination
    except (OSError, ValueError):
        destination.unlink(missing_ok=True)
        return None


def build_registered_shortcut(entry: AppEntry, appistry_dir: Path) -> Path:
    """Create a Start Menu launcher that starts (if needed) and opens an app."""
    safe_id = registry.validate_app_id(entry.id)
    remove_registered_shortcut(entry)
    return _create_shortcut(
        registered_shortcut_path(entry),
        target=_venv_executable(appistry_dir, windowed=True),
        arguments=_shortcut_arguments([str(appistry_dir / "appistry.py"), "launch", safe_id]),
        working_directory=appistry_dir,
        description=f"Open {entry.name} with Appistry",
        icon=_prepare_shortcut_icon(entry),
    )


def remove_registered_shortcut(entry: AppEntry) -> None:
    safe_id = registry.validate_app_id(entry.id)
    directory = appistry_shortcuts_dir()
    if directory.is_dir():
        for shortcut in directory.glob(f"* ({safe_id}).lnk"):
            try:
                shortcut.resolve().relative_to(directory.resolve())
            except ValueError:
                continue
            shortcut.unlink(missing_ok=True)
    icon = registry.APPISTRY_DIR / "shortcut-icons" / f"{safe_id}.ico"
    icon.unlink(missing_ok=True)


def install_appistry_shortcuts(appistry_dir: Path) -> tuple[Path, Path]:
    """Install the login-start and Start Menu shortcuts for the tray app."""
    target = _venv_executable(appistry_dir, windowed=True)
    args = _shortcut_arguments([str(appistry_dir / "windows_tray.py")])
    icon = _prepare_appistry_icon(appistry_dir)
    startup = _create_shortcut(
        startup_dir() / _STARTUP_SHORTCUT,
        target=target,
        arguments=args,
        working_directory=appistry_dir,
        description="Start Appistry when you sign in",
        icon=icon,
    )
    menu = _create_shortcut(
        appistry_shortcuts_dir() / _STARTUP_SHORTCUT,
        target=target,
        arguments=args,
        working_directory=appistry_dir,
        description="Open Appistry",
        icon=icon,
    )
    return startup, menu


def _prepare_appistry_icon(appistry_dir: Path) -> Path | None:
    source = appistry_dir / "appistry_icon.png"
    if not source.is_file():
        return None
    destination = registry.APPISTRY_DIR / "appistry_icon.ico"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.convert("RGBA").save(
                destination,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)],
            )
        return destination
    except (OSError, ValueError):
        destination.unlink(missing_ok=True)
        return None


def uninstall_shortcuts() -> None:
    (startup_dir() / _STARTUP_SHORTCUT).unlink(missing_ok=True)
    (appistry_shortcuts_dir() / _STARTUP_SHORTCUT).unlink(missing_ok=True)
    for entry in registry.load():
        remove_registered_shortcut(entry)
    try:
        appistry_shortcuts_dir().rmdir()
    except OSError:
        log.debug("Appistry Start Menu folder is not empty", exc_info=True)
    (registry.APPISTRY_DIR / "appistry_icon.ico").unlink(missing_ok=True)


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
    """Add Appistry's venv Scripts directory to the current user's PATH."""
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

    paths: list[str] = []
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
                    paths.append(os.path.expandvars(str(value)))
        except OSError:
            continue
    return os.pathsep.join(paths)


def refresh_user_environment() -> None:
    """Pick up user Environment registry changes without restarting the tray."""
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


def control_server_running() -> bool:
    path = registry.APPISTRY_DIR / _CONTROL_PORT_FILE
    try:
        port = int(path.read_text(encoding="utf-8").strip())
        if not 1 <= port <= 65535:
            return False
        url = f"http://127.0.0.1:{port}/api/status"
        # The URL is fixed to loopback and the port is range-checked above.
        with urllib.request.urlopen(
            url,
            timeout=0.5,
        ) as response:
            payload = json.loads(response.read(1024))
        return payload == {"service": "appistry", "ok": True}
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False


def start_tray(appistry_dir: Path, *, wait: bool = True) -> bool:
    """Start the Windows tray without a console window."""
    _require_windows()
    if control_server_running():
        return True
    target = _venv_executable(appistry_dir, windowed=True)
    try:
        # Both paths are derived from this trusted installation directory.
        proc = subprocess.Popen(
            [str(target), str(appistry_dir / "windows_tray.py")],
            cwd=str(appistry_dir),
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
            if control_server_running():
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.1)
        _terminate_spawned_process(proc)
        return False
    return True


def _terminate_spawned_process(proc) -> None:
    """Terminate a tray child that failed to establish its control endpoint."""
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
    """Stop only a verified Appistry Windows tray process."""
    path = tray_pid_path()
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
        command = [Path(part).name.casefold() for part in proc.cmdline()]
        if "windows_tray.py" not in command:
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


class NamedMutex:
    """Process-lifetime Windows named mutex used by the tray single-instance guard."""

    def __init__(self, name: str = r"Local\AppistryWindowsTray"):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        _require_windows()
        import win32api  # type: ignore[import-not-found]
        import win32event  # type: ignore[import-not-found]
        import winerror  # type: ignore[import-not-found]

        self.handle = win32event.CreateMutex(None, False, self.name)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            win32api.CloseHandle(self.handle)
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            import win32api  # type: ignore[import-not-found]

            win32api.CloseHandle(self.handle)
        finally:
            self.handle = None
