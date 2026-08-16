"""The macOS menu bar tray.

This module is only the rumps/AppKit rendering of :mod:`tray`'s rows. It decides
nothing about what the menu contains: it walks the rows, makes an ``NSMenuItem``
per row, and forwards clicks. Every question of *what* to show — which ravens,
in what order, with which labels, and which of them are unusable and why — is
answered by :mod:`ravens`, :mod:`menu_spec`, :mod:`host`, and :mod:`tray`, all of
which are platform-neutral and shared with the Windows tray.

Keeping it that thin is the point. The previous design in this repository had
each tray assemble its own menu from raw state, and the two drifted until each
had separately hardcoded a special case for one participant's id — the exact
coupling this architecture exists to prevent.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: The directory containing the ``roost`` package. launchd does not source a
#: shell profile, so ``PYTHONPATH`` cannot be relied on; the agent runs
#: ``-m roost.menubar`` from this directory. Inserting it here as well keeps the
#: module runnable as a plain file path (``python roost/menubar.py``) for anyone
#: debugging outside the launch agent — running the file puts *this* directory on
#: ``sys.path``, not its parent, so ``import roost`` would otherwise fail.
_PACKAGE_ROOT = HERE.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

import logging
import os
import subprocess
import webbrowser

_IS_MACOS = sys.platform == "darwin"

if _IS_MACOS:
    import rumps
else:  # pragma: no cover - importability shim only
    class _RumpsAppShim:
        pass

    class _RumpsShim:
        App = _RumpsAppShim

        @staticmethod
        def timer(_seconds):
            return lambda function: function

        @staticmethod
        def quit_application():
            raise RuntimeError("rumps is unavailable on this platform")

    rumps = _RumpsShim()

from roost import help_server
from roost import launcher
from roost import host
from roost import icons
from roost import paths
from roost import sanitize
from roost import tray
from roost.tray import RowKind

log = logging.getLogger(__name__)

#: How often the menu is rebuilt from the ravens' descriptors and menus. Each
#: poll makes one bounded HTTP call per live raven, so this is also the rate at
#: which a raven's status can go stale on screen.
POLL_SECONDS = 5

_ZSHENV_PATH = Path.home() / ".zshenv"
_zshenv_mtime: float | None = None
_zshenv_managed_keys: set[str] = set()


# ── Environment bootstrap ─────────────────────────────────────────────────────
# launchd does not source shell init files, so variables set in ~/.zshenv are
# invisible to this process. Roost itself needs almost nothing from them, but
# a raven's descriptor directory can be relocated with RAVENS_STATE_DIR, and a
# user who sets that in ~/.zshenv would otherwise find the tray looking in a
# different place than the raven publishes to.

def _source_zshenv() -> dict[str, str]:
    if not _IS_MACOS:
        return {}
    try:
        result = subprocess.run(
            ["zsh", "-c", "source ~/.zshenv 2>/dev/null; env -0"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return {}
    env = {}
    for item in result.stdout.split("\0"):
        if "=" in item:
            key, _, value = item.partition("=")
            env[key] = value
    return env


def _load_zshenv() -> None:
    global _zshenv_mtime
    if not _ZSHENV_PATH.exists():
        return
    for key, value in _source_zshenv().items():
        if key not in os.environ:
            os.environ[key] = value
            _zshenv_managed_keys.add(key)
    _zshenv_mtime = _ZSHENV_PATH.stat().st_mtime


def refresh_zshenv() -> None:
    """Re-source ``~/.zshenv`` if it changed since the last check.

    Only keys previously sourced from zshenv (or new keys not already present)
    are touched, so a launchd-provided value this process did not set is never
    clobbered.
    """
    global _zshenv_mtime
    if not _ZSHENV_PATH.exists():
        return
    try:
        mtime = _ZSHENV_PATH.stat().st_mtime
    except OSError:
        return
    if mtime == _zshenv_mtime:
        return
    _zshenv_mtime = mtime
    for key, value in _source_zshenv().items():
        if key in _zshenv_managed_keys or key not in os.environ:
            os.environ[key] = value
            _zshenv_managed_keys.add(key)


# ── Helpers ───────────────────────────────────────────────────────────────────

def notify(title: str, message: str) -> None:
    """Fire a macOS notification, escaping text that came from a raven.

    The message is interpolated into an AppleScript source string, so a quote or
    backslash in it would otherwise terminate the literal and let raven-supplied
    text become script. It is sanitised first (control characters cannot reach
    here) and then escaped for the literal.
    """
    safe_title = sanitize.sanitize_label(title).replace("\\", "\\\\").replace('"', '\\"')
    safe_message = sanitize.sanitize_label(message).replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{safe_message}" with title "{safe_title}"'],
        capture_output=True,
    )


def _icon_kwargs() -> dict:
    """Return the rumps icon arguments for the configured tray icon.

    ``template`` matters: macOS keeps only the alpha channel of a template image
    and tints it for light mode, dark mode, and menu highlight. A colour file
    passed as a template renders as a flat silhouette, so :mod:`icons` decides
    per choice and this only carries the decision through.
    """
    choice = icons.resolve()
    if choice is None:
        log.warning("No tray icon could be resolved; falling back to a title")
        return {"title": "Ravens"}
    return {"icon": str(choice.path), "template": choice.template}


# ── App ───────────────────────────────────────────────────────────────────────

class RoostApp(rumps.App):
    """The tray. Renders rows; interprets nothing."""

    def __init__(self):
        super().__init__("", quit_button=None, **_icon_kwargs())
        self._signature = None
        self._model = host.MenuModel()
        self._build_menu()

    # ── Rendering ────────────────────────────────────────────────────────────

    def _build_menu(self, model: "host.MenuModel | None" = None) -> None:
        self._model = model if model is not None else host.build_model()
        rows = tray.build_rows(self._model)
        items = [self._render(row) for row in rows]
        self.menu.clear()
        self.menu.update(items)
        self._signature = tray.signature(rows)

    def _render(self, row: tray.Row):
        """Turn one row into a rumps menu item, or None for a separator."""
        if row.kind is RowKind.SEPARATOR:
            return None

        item = rumps.MenuItem(row.label)

        if row.kind is RowKind.ITEM and row.enabled:
            item.set_callback(self._make_activate(row))
        elif row.kind is RowKind.HOST and row.children:
            for child in row.children:
                child_item = rumps.MenuItem(child.label)
                child_item.set_callback(self._make_host_action(child.action))
                child_item.state = 1 if child.checked else 0
                item.add(child_item)
        elif row.kind is RowKind.HOST:
            item.set_callback(self._make_host_action(row.action))
        else:
            # A raven name, a section title, a reason, or an item the raven
            # marked unavailable. Deliberately inert: a row that looks clickable
            # and does nothing is worse than one that admits it is not.
            item.set_callback(None)
        return item

    def _make_activate(self, row: tray.Row):
        """Return a callback that hands one clicked row back to its own raven."""
        def activate(_sender):
            menu = self._model.find(row.raven)
            if menu is None or row.item is None:
                return
            url = host.activate(menu, row.item)
            if url:
                webbrowser.open(url)
            # A click can change what the raven wants to show, so rebuild rather
            # than waiting out the poll interval.
            self._build_menu()
        return activate

    def _make_host_action(self, action: str):
        def invoke(_sender):
            self._host_action(action)
        return invoke

    def _host_action(self, action: str) -> None:
        if action == "help":
            webbrowser.open(help_server.url())
        elif action == "quit":
            self._quit()
        elif action.startswith("start:"):
            self._start_raven(action[len("start:"):])

    def _start_raven(self, name: str) -> None:
        """Ask the supervisor to start a stopped raven, then refresh.

        The refresh is unconditional: the supervisor returning 0 means it
        accepted the request, not that the raven is up. Its descriptor is what
        proves that, and the next poll reads it.
        """
        spec = self._model.launch_spec(name)
        if spec is None:
            return
        ok, reason = launcher.start(spec)
        if not ok:
            log.warning("Could not start %s: %s", sanitize.safe_for_log(name), reason)
        self._refresh()

    def _apply_icon(self) -> None:
        """Re-read the configured icon and put it in the menu bar.

        ``template`` is set before ``icon`` because rumps applies the template
        flag when the image is loaded; setting it afterwards leaves the previous
        rendering in place until something else reloads the image.
        """
        kwargs = _icon_kwargs()
        if "icon" in kwargs:
            self.template = kwargs["template"]
            self.icon = kwargs["icon"]
        else:
            self.icon = None
            self.title = kwargs.get("title", "")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @rumps.timer(POLL_SECONDS)
    def _poll(self, _sender):
        """Refresh the menu from the ravens, skipping an unchanged rebuild."""
        try:
            refresh_zshenv()
            model = host.build_model()
            rows = tray.build_rows(model)
            if tray.signature(rows) != self._signature:
                self._build_menu(model)
        except Exception:
            # The poll runs inside the AppKit run loop. An exception escaping it
            # stops the timer, which freezes the menu at whatever it last showed
            # with no indication that it has stopped updating.
            log.warning("Menu refresh failed", exc_info=True)

    def _quit(self) -> None:
        help_server.shutdown()
        _HOST_LOCK.release()
        rumps.quit_application()


# ── Entry point ───────────────────────────────────────────────────────────────

_HOST_LOCK = host.HostLock()


def main() -> int:
    """Run the tray, or exit quietly if another process is already hosting."""
    if not _IS_MACOS:  # pragma: no cover - guarded by the caller
        from roost.windows_tray import main as windows_main

        return windows_main()

    paths.ensure_state_dir()
    logging.getLogger().setLevel(logging.WARNING)
    _install_log_handler()

    if not _HOST_LOCK.acquire():
        if _HOST_LOCK.failure == host.UNWRITABLE:
            # Not a duplicate launch: this machine cannot host at all, and the
            # user gets no tray icon to explain it. Say so where they will see it.
            log.error("Roost cannot start: %s", _HOST_LOCK.reason)
            notify("Roost", _HOST_LOCK.reason)
            return 1
        return 0

    try:
        # Suppress the Dock icon for non-framework Python builds, where
        # LSUIElement in Info.plist alone is not enough. Must happen before
        # rumps creates the run loop, but after NSApplication exists.
        from AppKit import NSApplication, NSApplicationActivationPolicyProhibited

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyProhibited
        )
        RoostApp().run()
        return 0
    finally:
        _HOST_LOCK.release()


def _install_log_handler() -> None:
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        paths.ensure_state_dir() / paths.LOG_NAME, maxBytes=512_000, backupCount=1
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)


if __name__ == "__main__":
    import traceback

    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        logging.critical(traceback.format_exc())
        raise
