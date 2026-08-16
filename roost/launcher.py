"""Asking the OS to start a raven that has stopped.

Roost used to have no answer here, and the menu said so badly: both ravens
greyed out, "Not running (its recorded process is gone)", and nothing to click.
The stated reason was that a stopped daemon withdraws its descriptor so the host
cannot see it to offer a row — but that is only true of a *clean* shutdown. A
kill, a crash, or a power cut leaves the descriptor exactly where it was, which
is precisely the case that produced the useless menu.

The real constraint is narrower, and it survives: **Roost must never execute a
command named in a descriptor.** The descriptor directory is writable by
anything running as this user, and Roost is one process shared across every
raven, so "the file says which program to run" is the write-then-execute path
this project hardened against.

So a descriptor names an *identifier*, never a command:

    "launch": {"kind": "launchd",     "id": "is.tohuw.huginn"}
    "launch": {"kind": "systemd",     "id": "huginn.service"}
    "launch": {"kind": "windows-run", "id": "HuginnDaemon"}

Roost hands that identifier to the platform's own supervisor and lets it decide
what to run. The command lives in launchd's plist, systemd's unit, or the
``Run`` key — all of them written by the raven's own ``install-agent``, which
the user ran deliberately. The worst a forged descriptor achieves is starting a
service the user already installed. Roost still owns no lifecycle; it is only
asking the thing that does.

A raven that publishes no ``launch`` gets a disabled row naming the command that
would install one, which is at least a dead end with directions.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: The supervisors Roost knows how to ask. A descriptor naming anything else is
#: refused at parse time rather than passed through to a shell.
KINDS = ("launchd", "systemd", "windows-run")

#: Identifiers are handed to a supervisor as a single argv element, so they are
#: constrained to what those supervisors actually accept: reverse-DNS labels,
#: unit names, registry value names. No spaces, no separators, no quoting
#: characters -- nothing that could read as an argument or a path.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MAX_ID_LENGTH = 128

#: How long to wait on the supervisor. Starting is asynchronous everywhere --
#: the supervisor forks and returns -- so this bounds a wedged tool, not a slow
#: daemon.
TIMEOUT_SECONDS = 10.0


class LaunchError(ValueError):
    """A descriptor's launch block is unusable. Never raised at click time."""


@dataclass(frozen=True)
class LaunchSpec:
    """A validated request to ask one supervisor to start one service."""

    kind: str
    id: str


def parse(raw: object) -> LaunchSpec | None:
    """Validate a descriptor's ``launch`` block. None when absent.

    Raises :class:`LaunchError` for a block that is present and wrong, so the
    descriptor validator can refuse it the way it refuses any other malformed
    field. Absent is not an error: ``launch`` is optional, and a raven that
    predates it must keep working.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LaunchError("launch must be an object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise LaunchError(f"launch.kind must be one of {', '.join(KINDS)}")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise LaunchError("launch.id must be a non-empty string")
    if len(identifier) > MAX_ID_LENGTH:
        raise LaunchError(f"launch.id must be {MAX_ID_LENGTH} characters or fewer")
    if not _ID_RE.fullmatch(identifier):
        raise LaunchError("launch.id must be a plain service identifier")
    return LaunchSpec(kind=kind, id=identifier)


def _run(argv: list[str]) -> tuple[bool, str]:
    """Run a supervisor command. Never raises; never uses a shell."""
    try:
        # encoding rather than text=True: the latter decodes with the *locale*
        # encoding, and what is being decoded is a supervisor's error message,
        # which is localised and not necessarily representable in it. A
        # UnicodeDecodeError is neither SubprocessError nor OSError, so it would
        # escape both handlers below and take the menu action with it.
        # errors="replace" because a mangled glyph in a reason beats no reason.
        completed = subprocess.run(
            argv, capture_output=True, timeout=TIMEOUT_SECONDS,
            encoding="utf-8", errors="replace", shell=False, check=False,
        )
    except FileNotFoundError:
        return False, f"{argv[0]} is not available on this system"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"{argv[0]} failed ({exc.__class__.__name__})"
    if completed.returncode != 0:
        # The supervisor's own stderr, trimmed. It is the only thing that can
        # say *why*, and it is not descriptor-derived.
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return False, detail[0][:200] if detail else f"exited {completed.returncode}"
    return True, ""


def _start_windows_run(value_name: str) -> tuple[bool, str]:
    """Start whatever the user's own ``Run`` entry says, reading it from the OS.

    The command comes from the registry, not from the descriptor: the descriptor
    only names *which* autostart entry to trigger. Windows already executes this
    exact string at every sign-in, so triggering it grants nothing new.
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows only
        return False, "the registry is not available on this system"

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            command, _kind = winreg.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return False, "no start-at-login entry is installed"
    except OSError as exc:
        return False, f"the Run key could not be read ({exc.__class__.__name__})"

    if not isinstance(command, str) or not command.strip():
        return False, "the start-at-login entry is empty"

    try:
        # shell=False with a string is the documented Windows form: the value is
        # a command line, and CreateProcess parses it -- no shell involved.
        subprocess.Popen(
            command, shell=False, close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
        )
    except (OSError, ValueError) as exc:
        return False, f"could not start it ({exc.__class__.__name__})"
    return True, ""


def start(spec: LaunchSpec) -> tuple[bool, str]:
    """Ask the supervisor to start this service. Returns ``(ok, reason)``.

    Never raises. A failure is a row that stays greyed with a reason, which is
    the same contract every other unavailable raven already has.
    """
    if spec.kind == "launchd":
        # kickstart starts it whether or not it is loaded, which "start" alone
        # does not; -k restarts a stuck one rather than reporting success.
        return _run(["launchctl", "kickstart", "-k",
                     f"gui/{os.getuid()}/{spec.id}"])
    if spec.kind == "systemd":
        return _run(["systemctl", "--user", "start", spec.id])
    if spec.kind == "windows-run":
        return _start_windows_run(spec.id)
    return False, f"unknown launch kind {spec.kind!r}"


def supported_here(spec: LaunchSpec) -> bool:
    """Is this supervisor the one this machine actually runs?

    A descriptor copied between machines can name launchd on Linux. Offering a
    row that cannot possibly work is worse than not offering one.
    """
    if spec.kind == "launchd":
        return sys.platform == "darwin"
    if spec.kind == "systemd":
        return sys.platform.startswith("linux")
    if spec.kind == "windows-run":
        return os.name == "nt"
    return False
