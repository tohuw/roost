"""Filesystem locations and owner-only write helpers shared across Appistry.

Appistry's own state lives under ``~/.appistry``. Everything written there is
created owner-only: the directory holds the host lock, the resolved tray-icon
choice, and the help server's port file, and on a shared machine none of those
should be legible — let alone writable — to another local user.

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

APPISTRY_DIR = Path.home() / ".appistry"


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
    directory Appistry writes its own state into, so the mode is applied
    explicitly afterwards.
    """
    path.mkdir(parents=True, exist_ok=True)
    restrict_to_owner(path)
    return path


def appistry_dir() -> Path:
    """Return Appistry's own state directory, creating it owner-only."""
    return secure_dir(APPISTRY_DIR)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` owner-only, replacing it atomically.

    A reader — including a second Appistry process, or a raven inspecting the
    host's state — must never observe a half-written file, so the content is
    staged in a sibling temp file that is chmodded *before* it is moved into
    place. That ordering matters: creating the final file first and chmodding
    after would leave a window where it is world-readable.
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
