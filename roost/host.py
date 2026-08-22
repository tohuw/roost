"""Host election and menu aggregation.

Exactly one process draws the shared menu. That process is elected by an
exclusive lock on a single file: whoever takes it is the host, and the kernel
releases it if that process dies for any reason, so a stale lock is never
possible. This is the ``flock`` guard the launcher used for its single-instance
check, ported forward — the mechanism was already precisely the "host election
via a single lock" the shared-menubar proposal asked for.

Which *bird* leads the menu is a separate question, and it is answered by data
rather than by code here: a bird declares ``host_priority`` in its descriptor and
Roost sorts by it. Huginn declares a higher priority than Muninn and therefore
leads when both are present; when it is absent, Muninn's section simply sorts
first and the same menu runs standalone. Roost does not know either name.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from roost import launcher
from roost import menu_spec
from roost import paths
from roost import bird_client
from roost import birds
from roost import sanitize
from roost.menu_spec import BirdMenu

_IS_WINDOWS = sys.platform == "win32"

if not _IS_WINDOWS:
    import fcntl
else:  # pragma: no cover - exercised on Windows only
    fcntl = None

# Where the Windows lock byte sits: past any PID the file will ever hold, so the
# mandatory lock never covers the recorded PID. Locking a byte beyond end-of-file
# is legal and is the documented way to get a whole-file lock without the data.
_LOCK_SENTINEL_OFFSET = 1 << 30

log = logging.getLogger(__name__)

#: The host lock's filename. Named for this project, not for the platform surface
#: it draws: the separate internal Appistry also elects a single tray with a file
#: called ``.menubar.lock``, and while it keeps that file in its own install tree
#: rather than in a shared state directory, relying on that to keep the two apart
#: would make our exclusivity depend on a detail of someone else's code.
HOST_LOCK_NAME = "roost.lock"


def host_lock_path() -> Path:
    return paths.STATE_DIR / HOST_LOCK_NAME


#: Set on a failed :meth:`HostLock.acquire` when the lock file itself could not
#: be created or opened, as opposed to another process legitimately holding it.
#: The two cases need different handling: the second is the normal outcome for a
#: duplicate launch and should exit quietly, while the first means this machine
#: cannot host at all and the user has to be told why.
UNWRITABLE = "unwritable"
CONTENDED = "contended"


class HostLock:
    """An exclusive, process-lifetime lock electing this process as the host.

    Held open for the process lifetime; closing it releases the lock. On POSIX
    this is ``flock``, which the kernel drops on process death — so unlike a PID
    file there is no stale-lock case to reason about. On Windows the same
    guarantee comes from an exclusive open of the file.

    The lock lives under Roost's own state directory at mode 0600, not beside
    the code. A lock file inside the install tree is wrong twice over: a
    read-only or shared install directory makes it uncreatable (which used to
    take the whole tray down with an uncaught ``PermissionError``), and a
    world-readable mode publishes the host's PID to every local user. Both the
    directory creation and the open are guarded here, so an unusable lock path
    reports :data:`UNWRITABLE` and the caller decides — it never raises into a
    tray startup path.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or host_lock_path()
        self._handle = None
        #: Why the last :meth:`acquire` failed — ``UNWRITABLE`` or ``CONTENDED``.
        self.failure = ""
        #: A human-readable form of :attr:`failure`, safe to log or show.
        self.reason = ""

    @property
    def held(self) -> bool:
        return self._handle is not None

    def _fail(self, failure: str, reason: str) -> bool:
        self.failure = failure
        self.reason = reason
        return False

    def acquire(self) -> bool:
        """Return True if this process is now the host.

        Returns False both when another process already holds the lock and when
        the lock file cannot be created; :attr:`failure` distinguishes them.
        """
        if self._handle is not None:
            return True
        self.failure = ""
        self.reason = ""
        try:
            paths.secure_dir(self.path.parent)
        except OSError as exc:
            return self._fail(
                UNWRITABLE,
                f"The state directory {self.path.parent} is not writable "
                f"({exc.__class__.__name__}).",
            )
        if _IS_WINDOWS:
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_posix(self) -> bool:
        try:
            raw_fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            # A read-only or otherwise unusable state directory must not crash
            # the tray on startup; it has to be reportable.
            return self._fail(
                UNWRITABLE,
                f"The host lock at {self.path} could not be opened "
                f"({exc.__class__.__name__}).",
            )
        handle = os.fdopen(raw_fd, "r+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return self._fail(
                CONTENDED, "Another Roost process is already hosting the menu."
            )
        self._handle = handle
        self._record_pid()
        return True

    def _acquire_windows(self) -> bool:  # pragma: no cover - Windows only
        import msvcrt

        try:
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError as exc:
            return self._fail(
                UNWRITABLE,
                f"The host lock at {self.path} could not be opened "
                f"({exc.__class__.__name__}).",
            )
        # Lock a byte far past the PID rather than byte 0. Unlike flock, which is
        # advisory, msvcrt.locking is *mandatory*: a locked byte 0 cannot be read
        # by anyone, so the lock would hide the very PID it was recording and
        # holder_pid() would report no host at all.
        try:
            handle.seek(_LOCK_SENTINEL_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return self._fail(
                CONTENDED, "Another Roost process is already hosting the menu."
            )
        handle.seek(0)
        self._handle = handle
        self._record_pid()
        return True

    def _record_pid(self) -> None:
        """Write this process's PID into the lock file for diagnostics.

        The lock itself is what enforces exclusivity; this is only so a user
        looking at the file can tell which process is hosting.
        """
        # A lock file left behind by an older build (which created it 0644 inside
        # the repo) keeps its mode when reopened, so re-restrict it every time
        # rather than trusting the mode the ``os.open`` above asked for.
        paths.restrict_to_owner(self.path)
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
    return pid if birds.pid_is_alive(pid) else None


# ── Aggregation ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MenuModel:
    """Everything the shared menu shows, ready to render.

    ``menus`` is ordered exactly as the menu should be drawn. The renderer walks
    it and draws labels; it does not sort, filter, or reinterpret.
    """

    menus: tuple[BirdMenu, ...] = ()

    @property
    def any_available(self) -> bool:
        return any(menu.available for menu in self.menus)

    def launch_spec(self, name: str) -> "launcher.LaunchSpec | None":
        """The launch spec for a bird by name, if it published one.

        Resolved from the model the menu was drawn from, not re-read from disk:
        the row exists because *this* model said it could be started, and
        re-reading between draw and click is how a click acts on something the
        user never saw.
        """
        for menu in self.menus:
            if menu.name == name:
                return menu.launch
        return None

    @property
    def badge_total(self) -> int:
        return sum(menu.spec.badge for menu in self.menus if menu.available)

    def signature(self) -> tuple:
        """A hashable summary used to skip idle rebuilds."""
        return tuple(menu.signature() for menu in self.menus)

    def find(self, name: str) -> BirdMenu | None:
        for menu in self.menus:
            if menu.name == name:
                return menu
        return None


def build_menu(bird: "birds.Bird", *, timeout: float = bird_client.MENU_TIMEOUT) -> BirdMenu:
    """Fetch and parse one bird's menu, or describe why it could not be.

    Never raises. An unreachable, slow, or misbehaving bird produces a
    :class:`BirdMenu` with a ``reason``, which the renderer draws as a disabled
    section — the failure is visible, bounded, and not contagious.
    """
    if isinstance(bird, birds.UnavailableBird):
        return menu_spec.unavailable(bird)

    descriptor = bird.descriptor
    display = sanitize.sanitize_label(descriptor.display) or descriptor.name
    try:
        payload = bird_client.fetch_menu(descriptor, timeout=timeout)
    except bird_client.BirdRequestError as exc:
        return BirdMenu(
            name=descriptor.name,
            display=display,
            reason=sanitize.sanitize_label(exc.reason) or "Could not be reached.",
            descriptor=descriptor,
        )
    except Exception:
        # A client bug must not take the whole menu down with it. Log the detail;
        # show the user something honest and generic.
        log.warning(
            "Unexpected failure fetching the menu for bird %s",
            sanitize.safe_for_log(descriptor.name),
            exc_info=True,
        )
        return BirdMenu(
            name=descriptor.name,
            display=display,
            reason="Could not be read.",
            descriptor=descriptor,
        )

    spec = menu_spec.parse_menu(payload)
    return BirdMenu(
        name=descriptor.name,
        # A bird may retitle its own section through the menu payload; fall
        # back to the descriptor's display name when it does not.
        display=spec.title or display,
        spec=spec,
        descriptor=descriptor,
    )


def build_model(
    directory: Path | None = None,
    *,
    timeout: float = bird_client.MENU_TIMEOUT,
) -> MenuModel:
    """Discover every bird and build the whole menu model.

    Order comes from :func:`birds.discover`, which sorts by the ``host_priority``
    each bird declares. Roost contributes no opinion about which bird should
    lead — hardcoding one would be the same mistake as a hardcoded catalog id.
    """
    discovered = birds.discover(directory)
    return MenuModel(tuple(build_menu(bird, timeout=timeout) for bird in discovered))


def activate(menu: BirdMenu, item: menu_spec.MenuItem) -> str | None:
    """Act on a clicked menu item.

    Returns a URL for the caller to open, or None when the action was forwarded
    to the bird. Roost does not interpret ``item.action_id``; it hands it back
    to the bird that published it, under that bird's own credential.
    """
    if menu.descriptor is None or not item.clickable:
        return None
    if item.url:
        return bird_client.open_url(menu.descriptor, item.url)
    try:
        bird_client.send_action(menu.descriptor, item.action_id)
    except bird_client.BirdRequestError as exc:
        log.warning(
            "Bird %s refused action: %s",
            sanitize.safe_for_log(menu.name),
            sanitize.safe_for_log(exc.reason),
        )
    return None


#: An attention-styled item at one poll, keyed by (bird name, section id,
#: item id-or-label) rather than position -- so a menu reflow, or an
#: unrelated item appearing earlier in the same bird, never makes an
#: unrelated item look like it just changed.
AttentionKey = tuple[str, str, str]


class AttentionItem(NamedTuple):
    """One attention-styled item, with enough context to act on it.

    ``bird`` is the name :meth:`MenuModel.find` takes and ``display`` is what to
    call it on screen; both are here because a toast has to do both jobs. It
    titles itself with the display name, and a click on it has to reach the same
    :func:`activate` a click on the menu row would -- which needs the bird the
    item came from, since the item alone does not say.
    """

    bird: str
    display: str
    item: menu_spec.MenuItem


def attention_state(model: MenuModel) -> dict[AttentionKey, AttentionItem]:
    """Every attention-styled item currently shown, keyed stably.

    A separator carries no identity and an unavailable bird contributes only a
    reason, not items, so both are skipped. The key falls back to the item's
    own label when it has no action id, since a purely informational
    attention item (no action, no url) is still real and still worth a toast.
    """
    state: dict[AttentionKey, AttentionItem] = {}
    for menu in model.menus:
        if not menu.available:
            continue
        for section in menu.spec.sections:
            for item in section.items:
                if item.separator or item.style != "attention":
                    continue
                key = (menu.name, section.id, item.action_id or item.label)
                state[key] = AttentionItem(menu.name, menu.display, item)
    return state


def newly_attention(
    previous: dict[AttentionKey, AttentionItem] | None,
    current: dict[AttentionKey, AttentionItem],
) -> list[AttentionItem]:
    """Items that just became attention-worthy, in this poll's order.

    ``previous`` is ``None`` exactly once, before any menu has ever been read.
    That case must return nothing: every item already needing attention at
    startup would otherwise fire a toast at once, which reads as noise rather
    than as the signal a toast is supposed to be.
    """
    if previous is None:
        return []
    return [pair for key, pair in current.items() if key not in previous]
