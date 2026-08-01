"""Filesystem locations and owner-only write helpers shared across Roost.

Roost's own state lives in a directory named after this project and nothing else.
Everything written there is created owner-only: the directory holds the host lock,
the resolved tray-icon choice, the help server's port file, and the log, and on a
shared machine none of those should be legible — let alone writable — to another
local user.

**Why the name matters.** This project was forked from a separate internal tool
called Appistry, which owns ``~/.appistry`` and is still in daily use. Its
``registry.toml``, ``pids/``, and ``secrets/`` live there. Roost therefore keeps
its state somewhere else entirely and never reads or writes anything under
``~/.appistry``, so both can run at once on the same account.

The location follows the same platform rule :mod:`roost.ravens` applies to the
shared descriptor directory, because "put your state where the platform says" is
not a per-module opinion:

- Windows: ``%LOCALAPPDATA%\\Roost`` (``~\\AppData\\Local\\Roost`` if unset)
- POSIX: ``$XDG_STATE_HOME/roost``, falling back to ``~/.local/state/roost``

Note the asymmetry with the *ravens* directory: that one is a cross-project
contract Huginn and Muninn also write to, so its name is fixed and not ours to
change. This one is private to Roost and sits beside it.

**There is deliberately no migration from ~/.appistry.** An earlier build of this
project did write its state there, so the question is real, and the answer is to
start clean:

- The only files that build owned are an icon preference, a lock, a log, an
  ephemeral port file, and a PID file. Every one is either derived state that is
  rebuilt on the next launch or a single user preference that takes one command to
  set again. There is nothing there worth the risk of moving.
- The risk of moving is not symmetric with the benefit. ``~/.appistry`` also holds
  the internal tool's ``registry.toml``, ``pids/``, and ``secrets/``, which are
  live data for software in daily use. A migration is a delete-and-recreate on
  paths inside another program's state directory, and one wrong filename there
  corrupts a working tool to save a user from re-picking an icon.
- Two of the filenames are genuinely ambiguous. Both projects wrote
  ``menubar-http-port`` and ``menubar.log`` into that directory under the same
  names, so given only a filesystem there is no way to tell whose a given file is.
  A migration would have to either guess or leave them, and guessing is how the
  bad outcome above happens.

So nothing under ``~/.appistry`` is read, moved, or removed — not even the files
this project once wrote. A user upgrading past the rename gets a default icon
choice and, at worst, a few kilobytes of orphaned files they may delete by hand.
That is documented in README.md under "Coexistence".

The 0600-file / 0700-directory handling here is ported from the launcher's
per-launch secret store, which is the only part of that store the status host
still needs.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
log = logging.getLogger(__name__)

#: The directory name under the platform's state root. Deliberately not
#: ``appistry``: see the module docstring.
STATE_DIR_NAME = "Roost" if _IS_WINDOWS else "roost"


def _resolve_state_dir() -> Path:
    """Return the platform-appropriate location for Roost's own state.

    Resolved once at import, like the ``Path.home()`` lookup it replaces. A
    process that has already chosen where its lock and port file live must not
    change its mind halfway through because an environment variable moved.
    """
    if _IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / STATE_DIR_NAME
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / STATE_DIR_NAME


#: Roost's own state directory. Tests replace this attribute; production code
#: reads it rather than recomputing the location.
STATE_DIR = _resolve_state_dir()

#: The tray's log file. Named for this project rather than for the platform
#: surface it happens to draw ("menubar.log"), so that a log sitting next to an
#: unrelated tool's is still attributable at a glance.
LOG_NAME = "roost.log"


def restrict_to_owner(path: Path) -> None:
    """Best-effort: make ``path`` readable/writable only by the current user.

    POSIX honors chmod bits directly. Windows ignores them (NTFS uses ACLs, not
    mode bits), so this replaces the DACL with one entry granting the owner full
    control and nothing else.
    """
    if _IS_WINDOWS:
        try:
            import ntsecuritycon
            import win32security

            user, _, _ = win32security.LookupAccountName("", os.getlogin())
            dacl = win32security.ACL()
            dacl.AddAccessAllowedAce(
                win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user
            )
            sd = win32security.SECURITY_DESCRIPTOR()
            sd.SetSecurityDescriptorDacl(1, dacl, False)
            win32security.SetFileSecurity(
                str(path), win32security.DACL_SECURITY_INFORMATION, sd
            )
        except Exception:
            log.debug("Could not restrict ACL for %s", path, exc_info=True)
    else:
        mode = 0o700 if path.is_dir() else 0o600
        try:
            path.chmod(mode)
        except OSError:
            pass


def secure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the current user.

    ``mkdir(mode=...)`` is masked by umask, which is not good enough for a
    directory Roost writes its own state into, so the mode is applied explicitly
    afterwards.
    """
    path.mkdir(parents=True, exist_ok=True)
    restrict_to_owner(path)
    return path


def ensure_state_dir() -> Path:
    """Return Roost's own state directory, creating it owner-only."""
    return secure_dir(STATE_DIR)


def log_path() -> Path:
    """Return the tray's log file path inside Roost's own state directory."""
    return STATE_DIR / LOG_NAME


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` owner-only, replacing it atomically.

    A reader — including a second Roost process, or a raven inspecting the host's
    state — must never observe a half-written file, so the content is staged in a
    sibling temp file that is chmodded *before* it is moved into place. That
    ordering matters: creating the final file first and chmodding after would
    leave a window where it is world-readable.
    """
    secure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        restrict_to_owner(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
