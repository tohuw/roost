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

import ctypes
import logging
import ntpath
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from roost import help_server
from roost import paths

_STARTUP_SHORTCUT = "Roost.lnk"

#: The tray's PID file. Prefixed, like every other file this project writes: the
#: separate internal Appistry writes a ``windows-tray.pid`` of its own, and while
#: the two state directories are disjoint, reusing the basename is the thing that
#: would turn any future directory sharing into one tray reading the other's PID.
_TRAY_PID_FILE = "roost-windows-tray.pid"

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


#: Windows names a tray entry after the *executable's* ``FileDescription``,
#: falling back to the filename when there is none. The tray runs on the base
#: interpreter, whose FileDescription is "Python", so Settings > Taskbar listed
#: Roost as "Python". Confirmed by reading HKCU\Control Panel\NotifyIconSettings,
#: which records the interpreter's path rather than Roost's.
#:
#: **Not "Roost.exe".** That is pip's console script for this very package, and
#: Windows paths are case-insensitive, so staging the copy silently overwrote
#: the ``roost`` command with a windowed interpreter. Every CLI invocation then
#: "succeeded" while doing nothing: a GUI interpreter given a subcommand treats
#: it as a script path, finds no such file, and writes the error to a console
#: that does not exist. The shown name comes from the resource below, never from
#: this filename, so the two needs do not actually compete.
BRANDED_LAUNCHER = "RoostTray.exe"

#: Names ``branded_launcher`` must never stage onto, case-folded. These are
#: pip's console scripts for this distribution; a copy over one of them removes
#: the command it implements, and nothing about that failure is visible.
_RESERVED_LAUNCHER_NAMES = frozenset({"roost.exe", "roost-script.py", "roost"})


def _base_interpreter(repo_dir: Path) -> Path | None:
    r"""The real interpreter behind the venv, not its trampoline.

    uv builds ``Scripts\pythonw.exe`` as a trampoline that re-execs the base
    interpreter, and it is the *child* that owns the tray icon -- which is why
    renaming the trampoline would change nothing. ``pyvenv.cfg`` names the home.
    """
    config = repo_dir / ".venv" / "pyvenv.cfg"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "home":
            candidate = Path(value.strip()) / "pythonw.exe"
            return candidate if candidate.exists() else None
    return None


#: Language and codepage the version resource is written under: neutral, with
#: the Unicode codepage. Exactly what CPython's own interpreters ship, which is
#: the arrangement the shell is already known to read correctly here.
_VERSION_LANG, _VERSION_CODEPAGE = 0x0000, 0x04B0

#: The name the shell shows. Not "Roost.exe" -- with no version resource at all
#: the shell falls back to the filename *including its extension*, which is what
#: Taskbar settings displayed while this stripped the resource instead of
#: replacing it.
BRANDED_DESCRIPTION = "Roost"


def _pad4(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 4)


def _version_block(key: str, value: bytes = b"", *, wtype: int = 1,
                   value_words: int = 0, children: bytes = b"") -> bytes:
    """One node of a VS_VERSIONINFO tree, length-prefixed and 32-bit aligned.

    The two-byte ``wLength`` placeholder is part of ``body`` from the start so
    every alignment offset is measured from the structure's own beginning, which
    is what the format specifies. Computing padding without it is the easy way
    to produce a blob that parses on one machine and not the next.
    """
    body = struct.pack("<HHH", 0, value_words, wtype)
    body += key.encode("utf-16-le") + b"\x00\x00"
    body = _pad4(body)
    body += value
    if children:
        body = _pad4(body)
        body += children
    return struct.pack("<H", len(body)) + body[2:]


def _version_string(name: str, text: str) -> bytes:
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    # For a text value wValueLength counts characters, not bytes.
    return _pad4(_version_block(name, encoded, wtype=1, value_words=len(text) + 1))


def _version_resource(description: str, version: str) -> bytes:
    """Build a VS_VERSIONINFO saying this executable is called ``description``."""
    parts = [int(p) for p in re.findall(r"\d+", version)[:4]]
    parts += [0] * (4 - len(parts))
    high = (parts[0] << 16) | parts[1]
    low = (parts[2] << 16) | parts[3]
    fixed = struct.pack(
        "<LLLLLLLLLLLLL",
        0xFEEF04BD,   # dwSignature
        0x00010000,   # dwStrucVersion
        high, low,    # dwFileVersionMS / LS
        high, low,    # dwProductVersionMS / LS
        0x3F, 0,      # dwFileFlagsMask, dwFileFlags
        0x00000004,   # dwFileOS   = VOS__WINDOWS32
        0x00000001,   # dwFileType = VFT_APP
        0,            # dwFileSubtype
        0, 0,         # dwFileDateMS / LS
    )
    fields = {
        "CompanyName": description,
        "FileDescription": description,   # the one the shell actually shows
        "FileVersion": version,
        "InternalName": description,
        "OriginalFilename": BRANDED_LAUNCHER,
        "ProductName": description,
        "ProductVersion": version,
    }
    strings = b"".join(_version_string(k, v) for k, v in fields.items())
    table = _pad4(_version_block(
        f"{_VERSION_LANG:04x}{_VERSION_CODEPAGE:04x}", children=strings))
    string_info = _pad4(_version_block("StringFileInfo", children=table))
    translation = _pad4(_version_block(
        "Translation", struct.pack("<HH", _VERSION_LANG, _VERSION_CODEPAGE),
        wtype=0, value_words=4))
    var_info = _pad4(_version_block("VarFileInfo", children=translation))
    return _version_block("VS_VERSION_INFO", fixed, wtype=0,
                          value_words=len(fixed),
                          children=string_info + var_info)


def _write_version_resource(path: Path, description: str, version: str) -> bool:
    """Give a PE a version resource naming it ``description``.

    Replaces RT_VERSION rather than deleting it. Deleting was the first attempt
    and it half-worked: with no version resource the shell falls back to the
    *filename*, so Taskbar settings read "Roost.exe" instead of "Roost". The
    fallback is the shell's, so the only way to drop the extension is to stop
    relying on the fallback and say the name outright.

    ``bDeleteExistingResources=False``, so the application manifest survives --
    it carries the DPI awareness and UAC settings the interpreter needs, and
    dropping it would change how the tray runs in order to fix what it is
    called.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.UpdateResourceW.restype = wintypes.BOOL
    kernel32.UpdateResourceW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.WORD, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel32.EndUpdateResourceW.restype = wintypes.BOOL
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]

    rt_version = ctypes.cast(ctypes.c_void_p(16), wintypes.LPCWSTR)
    first_entry = ctypes.cast(ctypes.c_void_p(1), wintypes.LPCWSTR)
    blob = _version_resource(description, version)

    handle = kernel32.BeginUpdateResourceW(str(path), False)
    if not handle:
        return False
    written = kernel32.UpdateResourceW(
        handle, rt_version, first_entry, _VERSION_LANG, blob, len(blob))
    committed = kernel32.EndUpdateResourceW(handle, not written)
    return bool(written and committed and file_description(path) == description)


def file_description(path: Path) -> str | None:
    """What the shell will call this executable, or None if it carries no name."""
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    version.VerQueryValueW.argtypes = [
        wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]

    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    value, length = wintypes.LPVOID(), wintypes.UINT()
    block = f"\\StringFileInfo\\{_VERSION_LANG:04x}{_VERSION_CODEPAGE:04x}\\FileDescription"
    if version.VerQueryValueW(buffer, block, ctypes.byref(value),
                              ctypes.byref(length)) and length.value:
        return ctypes.wstring_at(value.value, length.value - 1)
    return None


def _repo_version(repo_dir: Path) -> str:
    """Roost's version, from the same VERSION file pyproject reads.

    Only fills in FileVersion and ProductVersion, which nothing in the tray
    surfaces -- FileDescription is the field that shows. So an unreadable
    VERSION file is a cosmetic loss, not a reason to leave the launcher unnamed.
    """
    try:
        return (repo_dir / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def branded_launcher(repo_dir: Path) -> Path | None:
    """A copy of the interpreter that Windows will call "Roost". None on failure.

    A copy placed in ``Scripts`` still finds the venv: that is exactly how a
    classic venv is laid out, and CPython looks for ``pyvenv.cfg`` next to the
    executable's directory. So this changes the tray's shell identity without
    changing what runs.

    Best-effort throughout. Every failure returns None and the caller falls back
    to ``pythonw.exe``, because a tray that starts under the wrong name is
    strictly better than one that does not start.
    """
    if not is_windows():
        return None
    if BRANDED_LAUNCHER.casefold() in _RESERVED_LAUNCHER_NAMES:
        # Refusing here costs the tray its name; not refusing costs the user
        # their `roost` command, silently. The guard is deliberately at the
        # write, not only at the constant, because the constant is exactly the
        # kind of thing a later change edits without knowing what shares that
        # directory.
        log.debug("Refusing to stage the launcher over a console script")
        return None
    source = _base_interpreter(repo_dir)
    if source is None:
        return None
    target = repo_dir / ".venv" / "Scripts" / BRANDED_LAUNCHER
    try:
        # Restaged when the interpreter has been upgraded underneath us, and
        # when an existing copy is not named yet -- the first version of this
        # stripped the resource instead of writing one, and those copies are on
        # disk already, showing "Roost.exe".
        fresh = target.exists() and target.stat().st_mtime >= source.stat().st_mtime
        if fresh and file_description(target) == BRANDED_DESCRIPTION:
            return target
        shutil.copy2(source, target)
    except OSError:
        # Windows holds an executable's image open while it runs, so restaging
        # fails outright whenever a tray is already up -- which is exactly when
        # this runs, since starting a second tray is what asks for the path.
        # An existing copy is still a working interpreter, merely misnamed until
        # the next start, and that beats falling back to pythonw.exe and being
        # called "Python" again.
        log.debug("Could not stage the branded launcher", exc_info=True)
        return target if target.exists() else None
    if not _write_version_resource(target, BRANDED_DESCRIPTION,
                                   _repo_version(repo_dir)):
        # It still runs; it just says "Python" again. Better than not starting.
        log.debug("Could not name the version resource on %s", target)
    return target


def _venv_executable(repo_dir: Path, *, windowed: bool = False) -> Path:
    if windowed:
        # The tray: this is the process Windows names in Settings > Taskbar, so
        # its shell identity is the whole reason the branded copy exists.
        branded = branded_launcher(repo_dir)
        if branded is not None:
            return branded
        return repo_dir / ".venv" / "Scripts" / "pythonw.exe"
    return repo_dir / ".venv" / "Scripts" / "python.exe"


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

def _running_tray_pid() -> int | None:
    """The tray's PID, if that PID is alive and really is the tray.

    A pure query, which is why this does not share :func:`stop_tray`'s copy of
    the same verification: that one prunes the PID file and refuses outright
    when it cannot verify, because it is about to *signal* something. Here the
    only question is whether a tray exists.

    PID reuse is covered the same way it is there — by checking the command
    line rather than trusting the number — and psutil raising for a dead PID is
    what makes the file's staleness detectable at all.
    """
    try:
        pid = int(tray_pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process(pid)
        command = [ntpath.basename(part).casefold() for part in proc.cmdline()]
    except Exception:  # psutil.Error and the OS errors it wraps
        return None
    return pid if TRAY_MODULE in command else None


def tray_is_running() -> bool:
    """Return True if a Roost tray process exists.

    **The help endpoint cannot be the only answer.** ``help_server`` is started
    lazily, by the Help menu item, so a freshly started tray has no port at all
    and probing one always failed. ``start_tray`` waits on this, so it concluded
    the tray had not started and terminated the perfectly healthy process it had
    just launched — ``roost install`` and ``roost ui`` reported "failed to
    start" every time, with an empty log, because nothing had actually crashed.

    So a verified live tray process is the primary answer, and the endpoint
    probe stays as a second opinion for the case the PID file is missing but a
    tray is up (an older tray, or a lost state file). The port file alone still
    proves nothing — a crashed tray leaves it behind — so when it is used the
    reply must identify Roost rather than whatever inherited the port.
    """
    import json
    import urllib.error
    import urllib.request

    if _running_tray_pid() is not None:
        return True

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
