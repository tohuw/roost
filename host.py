"""Host election and menu aggregation.

Exactly one process draws the shared menu. That process is elected by an
exclusive lock on a single file: whoever takes it is the host, and the kernel
releases it if that process dies for any reason, so a stale lock is never
possible. This is the ``flock`` guard the launcher used for its single-instance
check, ported forward — the mechanism was already precisely the "host election
via a single lock" the shared-menubar proposal asked for.

Which *raven* leads the menu is a separate question, and it is answered by data
rather than by code here: a raven declares ``host_priority`` in its descriptor and
Appistry sorts by it. Huginn declares a higher priority than Muninn and therefore
leads when both are present; when it is absent, Muninn's section simply sorts
first and the same menu runs standalone. Appistry does not know either name.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import menu_spec
import paths
import raven_client
import ravens
import sanitize
from menu_spec import RavenMenu

_IS_WINDOWS = sys.platform == "win32"

if not _IS_WINDOWS:
    import fcntl
else:  # pragma: no cover - exercised on Windows only
    fcntl = None

log = logging.getLogger(__name__)

HOST_LOCK_NAME = "menubar.lock"


def host_lock_path() -> Path:
    return paths.APPISTRY_DIR / HOST_LOCK_NAME


class HostLock:
    """An exclusive, process-lifetime lock electing this process as the host.

    Held open for the process lifetime; closing it releases the lock. On POSIX
    this is ``flock``, which the kernel drops on process death — so unlike a PID
    file there is no stale-lock case to reason about. On Windows the same
    guarantee comes from an exclusive open of the file.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or host_lock_path()
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Return True if this process is now the host, False if another already is."""
        if self._handle is not None:
            return True
        paths.secure_dir(self.path.parent)
        if _IS_WINDOWS:
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_posix(self) -> bool:
        raw_fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(raw_fd, "r+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return False
        self._handle = handle
        self._record_pid()
        return True

    def _acquire_windows(self) -> bool:  # pragma: no cover - Windows only
        import msvcrt

        try:
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError:
            return False
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        self._record_pid()
        return True

    def _record_pid(self) -> None:
        """Write this process's PID into the lock file for diagnostics.

        The lock itself is what enforces exclusivity; this is only so a user
        looking at the file can tell which process is hosting.
        """
        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.write(str(os.getpid()))
            self._handle.flush()
        except OSError:
            log.debug("Could not record host pid in %s", self.path, exc_info=True)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.close()
        except OSError:
            pass
        self._handle = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


def holder_pid(path: Path | None = None) -> int | None:
    """Return the PID recorded in the host lock, if any and if still alive."""
    target = path or host_lock_path()
    try:
        raw = target.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if ravens.pid_is_alive(pid) else None


# ── Aggregation ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MenuModel:
    """Everything the shared menu shows, ready to render.

    ``menus`` is ordered exactly as the menu should be drawn. The renderer walks
    it and draws labels; it does not sort, filter, or reinterpret.
    """

    menus: tuple[RavenMenu, ...] = ()

    @property
    def any_available(self) -> bool:
        return any(menu.available for menu in self.menus)

    @property
    def badge_total(self) -> int:
        return sum(menu.spec.badge for menu in self.menus if menu.available)

    def signature(self) -> tuple:
        """A hashable summary used to skip idle rebuilds."""
        return tuple(menu.signature() for menu in self.menus)

    def find(self, name: str) -> RavenMenu | None:
        for menu in self.menus:
            if menu.name == name:
                return menu
        return None


def build_menu(raven: "ravens.Raven", *, timeout: float = raven_client.MENU_TIMEOUT) -> RavenMenu:
    """Fetch and parse one raven's menu, or describe why it could not be.

    Never raises. An unreachable, slow, or misbehaving raven produces a
    :class:`RavenMenu` with a ``reason``, which the renderer draws as a disabled
    section — the failure is visible, bounded, and not contagious.
    """
    if isinstance(raven, ravens.UnavailableRaven):
        return menu_spec.unavailable(raven)

    descriptor = raven.descriptor
    display = sanitize.sanitize_label(descriptor.display) or descriptor.name
    try:
        payload = raven_client.fetch_menu(descriptor, timeout=timeout)
    except raven_client.RavenRequestError as exc:
        return RavenMenu(
            name=descriptor.name,
            display=display,
            reason=sanitize.sanitize_label(exc.reason) or "Could not be reached.",
            descriptor=descriptor,
        )
    except Exception:
        # A client bug must not take the whole menu down with it. Log the detail;
        # show the user something honest and generic.
        log.warning(
            "Unexpected failure fetching the menu for raven %s",
            sanitize.safe_for_log(descriptor.name),
            exc_info=True,
        )
        return RavenMenu(
            name=descriptor.name,
            display=display,
            reason="Could not be read.",
            descriptor=descriptor,
        )

    spec = menu_spec.parse_menu(payload)
    return RavenMenu(
        name=descriptor.name,
        # A raven may retitle its own section through the menu payload; fall
        # back to the descriptor's display name when it does not.
        display=spec.title or display,
        spec=spec,
        descriptor=descriptor,
    )


def build_model(
    directory: Path | None = None,
    *,
    timeout: float = raven_client.MENU_TIMEOUT,
) -> MenuModel:
    """Discover every raven and build the whole menu model.

    Order comes from :func:`ravens.discover`, which sorts by the ``host_priority``
    each raven declares. Appistry contributes no opinion about which raven should
    lead — hardcoding one would be the same mistake as a hardcoded catalog id.
    """
    discovered = ravens.discover(directory)
    return MenuModel(tuple(build_menu(raven, timeout=timeout) for raven in discovered))


def activate(menu: RavenMenu, item: menu_spec.MenuItem) -> str | None:
    """Act on a clicked menu item.

    Returns a URL for the caller to open, or None when the action was forwarded
    to the raven. Appistry does not interpret ``item.action_id``; it hands it back
    to the raven that published it, under that raven's own credential.
    """
    if menu.descriptor is None or not item.clickable:
        return None
    if item.url:
        return raven_client.open_url(menu.descriptor, item.url)
    try:
        raven_client.send_action(menu.descriptor, item.action_id)
    except raven_client.RavenRequestError as exc:
        log.warning(
            "Raven %s refused action: %s",
            sanitize.safe_for_log(menu.name),
            sanitize.safe_for_log(exc.reason),
        )
    return None
