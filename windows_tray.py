"""The Windows system tray.

The Windows counterpart to :mod:`menubar`, and just as thin: it turns
:mod:`tray`'s rows into pystray widgets and forwards clicks. Both trays consume
the same rows from the same platform-neutral modules, so the menu cannot differ
between platforms except in how a row is drawn.

pystray differs from rumps in two ways that shape this file. It has no template
icon concept — it renders the RGBA bitmap literally, which is why :mod:`icons`
hands Windows the colour variant — and its menu is rebuilt wholesale rather than
mutated, so a refresh replaces ``Icon.menu`` and calls ``update_menu``.
"""

from __future__ import annotations

import logging
import signal
import threading
import traceback
import webbrowser
from pathlib import Path

import help_server
import host
import icons
import paths
import tray
import windows_support
from tray import RowKind

HERE = Path(__file__).resolve().parent
log = logging.getLogger(__name__)

#: Matches the macOS tray's refresh rate so a raven's status goes stale at the
#: same speed on both platforms.
POLL_SECONDS = 5


def _tray_image():
    """Load the configured tray icon as an RGBA bitmap for pystray.

    pystray needs a decoded image rather than a path, and it has no template
    concept: the bitmap is drawn literally. :func:`icons.resolve` already selects
    the colour variant on this platform for that reason.
    """
    from PIL import Image

    choice = icons.resolve()
    if choice is None:
        raise RuntimeError("No tray icon could be resolved")
    with Image.open(choice.path) as image:
        return image.convert("RGBA")


class AppistryWindowsTray:
    """The tray. Renders rows; interprets nothing."""

    def __init__(self):
        import pystray

        self._pystray = pystray
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._signature = None
        self._model = host.MenuModel()
        self._icon = pystray.Icon(
            "appistry", _tray_image(), "Ravens", menu=self._build_menu()
        )

    # ── Rendering ────────────────────────────────────────────────────────────

    def _build_menu(self, model: "host.MenuModel | None" = None):
        Menu = self._pystray.Menu
        self._model = model if model is not None else host.build_model()
        rows = tray.build_rows(self._model)
        self._signature = tray.signature(rows)
        return Menu(*[self._render(row) for row in rows])

    def _render(self, row: tray.Row):
        Menu = self._pystray.Menu
        MenuItem = self._pystray.MenuItem

        if row.kind is RowKind.SEPARATOR:
            return Menu.SEPARATOR

        if row.kind is RowKind.ITEM and row.enabled:
            return MenuItem(row.label, self._callback(self._activate, row))

        if row.kind is RowKind.HOST and row.children:
            return MenuItem(
                row.label,
                Menu(*[
                    MenuItem(
                        child.label,
                        self._callback(self._host_action, child.action),
                        # pystray evaluates `checked` at draw time, so it is
                        # bound to the row's own value rather than re-derived.
                        checked=self._checker(child.checked),
                        radio=True,
                    )
                    for child in row.children
                ]),
            )

        if row.kind is RowKind.HOST:
            return MenuItem(row.label, self._callback(self._host_action, row.action))

        # A raven name, a section title, a reason, or an item the raven marked
        # unavailable: shown, never clickable.
        return MenuItem(row.label, None, enabled=False)

    @staticmethod
    def _callback(function, *bound):
        def invoke(_icon, _item):
            function(*bound)

        return invoke

    @staticmethod
    def _checker(value: bool):
        def is_checked(_item):
            return value

        return is_checked

    def _refresh(self, model: "host.MenuModel | None" = None) -> None:
        with self._state_lock:
            self._icon.menu = self._build_menu(model)
            self._icon.update_menu()

    # ── Actions ──────────────────────────────────────────────────────────────

    def _activate(self, row: tray.Row) -> None:
        menu = self._model.find(row.raven)
        if menu is None or row.item is None:
            return
        url = host.activate(menu, row.item)
        if url:
            webbrowser.open(url)
        self._refresh()

    def _host_action(self, action: str) -> None:
        if action == "help":
            webbrowser.open(help_server.url())
        elif action == "quit":
            self._shutdown()
        elif action.startswith("icon:"):
            icons.set_icon(action[len("icon:"):])
            try:
                self._icon.icon = _tray_image()
            except (OSError, RuntimeError, ValueError):
                log.warning("Could not load the selected tray icon", exc_info=True)
            self._refresh()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _poll(self) -> None:
        while not self._stop_event.wait(POLL_SECONDS):
            try:
                windows_support.refresh_user_environment()
                model = host.build_model()
                if tray.signature(tray.build_rows(model)) != self._signature:
                    self._refresh(model)
            except Exception:
                # The poll thread must outlive any single failed refresh; if it
                # dies the menu silently freezes at its last contents.
                log.error("Windows tray poll failed:\n%s", traceback.format_exc())

    def _setup(self, icon) -> None:
        icon.visible = True
        threading.Thread(
            target=self._poll, name="appistry-windows-poll", daemon=True
        ).start()

    def _shutdown(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        help_server.shutdown()
        windows_support.tray_pid_path().unlink(missing_ok=True)
        self._icon.stop()

    def run(self) -> None:
        windows_support.refresh_user_environment()
        windows_support.write_tray_pid()
        self._icon.run(setup=self._setup)


def main() -> int:
    if not windows_support.is_windows():
        raise SystemExit("windows_tray.py is only available on Windows")

    paths.appistry_dir()
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        paths.APPISTRY_DIR / "menubar.log", maxBytes=512_000, backupCount=1
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)

    # The same single-lock host election the macOS tray uses, so "exactly one
    # process draws the menu" is one mechanism rather than a per-platform guess.
    lock = host.HostLock()
    if not lock.acquire():
        if lock.failure == host.UNWRITABLE:
            log.error("Appistry cannot start: %s", lock.reason)
            return 1
        return 0

    tray_app = None
    try:
        tray_app = AppistryWindowsTray()

        def stop_for_signal(_signum, _frame):
            if tray_app is not None:
                tray_app._shutdown()

        signal.signal(signal.SIGTERM, stop_for_signal)
        signal.signal(signal.SIGINT, stop_for_signal)
        tray_app.run()
        return 0
    except Exception:
        logging.critical(traceback.format_exc())
        raise
    finally:
        windows_support.tray_pid_path().unlink(missing_ok=True)
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
