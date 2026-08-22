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

from roost import help_server
from roost import launcher
from roost import host
from roost import icons
from roost import menu_spec
from roost import paths
from roost import tray
from roost import windows_support
from roost import windows_toast
from roost.tray import RowKind

HERE = Path(__file__).resolve().parent
log = logging.getLogger(__name__)

#: Matches the macOS tray's refresh rate so a bird's status goes stale at the
#: same speed on both platforms.
POLL_SECONDS = 5

#: What Windows shows when the tray icon is hovered. The app's name, not a
#: description of the menu behind it.
TOOLTIP = "Roost"


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


class RoostWindowsTray:
    """The tray. Renders rows; interprets nothing."""

    def __init__(self):
        import pystray

        self._pystray = pystray
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._signature = None
        self._model = host.MenuModel()
        # None until the first build, which must establish a baseline rather
        # than notify -- see _notify_new_attention.
        self._attention = None
        # Built before the icon, because constructing the icon builds the menu,
        # and building the menu is what sends toasts.
        self._toaster = windows_toast.Toaster(self._toast_icon())
        self._icon = pystray.Icon(
            # The pystray name is the tray's OS-level identity on Windows. It must
            # not be "appistry": the separate internal Appistry ships its own
            # Windows tray under that name, and both can be running.
            #
            # The third argument is the hover tooltip, and it names *this app*,
            # not what the menu happens to contain. "Birds" read as the name of
            # some other program to anyone hovering over it -- the same identity
            # confusion the Taskbar entry had when it said "Python".
            "roost", _tray_image(), TOOLTIP, menu=self._build_menu()
        )

    # ── Rendering ────────────────────────────────────────────────────────────

    def _build_menu(self, model: "host.MenuModel | None" = None):
        Menu = self._pystray.Menu
        self._model = model if model is not None else host.build_model()
        self._notify_new_attention(self._model)
        rows = tray.build_rows(self._model)
        self._signature = tray.signature(rows)
        return Menu(*[self._render(row) for row in rows])

    @staticmethod
    def _toast_icon() -> str | None:
        """The file the notification centre should show beside our name."""
        choice = icons.resolve()
        return str(choice.path) if choice is not None else None

    def _notify_new_attention(self, model: "host.MenuModel") -> None:
        """Toast every item that just became attention-worthy.

        Fires nothing on the very first build (host.newly_attention's None
        case): a freshly-started tray must not open with a burst of toasts for
        every session that already needed attention before Roost was running.
        That first call also happens before ``self._icon`` exists yet -- it
        builds the menu passed into ``pystray.Icon(...)`` -- so the empty
        result here is what keeps this from reaching ``self._icon`` too early.

        A real toast is preferred and the tray icon's balloon is the fallback,
        never both: see :mod:`roost.windows_toast` for what the balloon cannot
        do. The fallback still reaches ``self._icon``, so it stays behind the
        same first-build guard it always did.
        """
        current = host.attention_state(model, self._attention)
        for entry in host.newly_attention(self._attention, current):
            message = (f"{entry.item.label} — {entry.item.detail}"
                       if entry.item.detail else entry.item.label)
            if not self._toaster.show(
                entry.display, message,
                on_click=self._click_handler(entry),
            ):
                self._icon.notify(message, title=entry.display)
        self._attention = current

    def _click_handler(self, entry: "host.AttentionItem"):
        """What a click on ``entry``'s toast does: exactly what its row does.

        The item is the one the toast was drawn from, and the bird is looked up
        in the model as it stands when the click arrives -- the descriptor is
        how the action is addressed and credentialled, so the current one is the
        only one worth sending to. A bird that has gone away since is simply not
        found, and the click does nothing.
        """
        def clicked() -> None:
            self._act(entry.bird, entry.item)

        return clicked

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

        # A bird name, a section title, a reason, or an item the bird marked
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
        if row.item is None:
            return
        self._act(row.bird, row.item)

    def _act(self, bird: str, item: "menu_spec.MenuItem") -> None:
        """Forward one item's action to the bird that published it.

        Shared by the menu and by a toast, so a click reaches the same place
        whichever surface it came from.
        """
        menu = self._model.find(bird)
        if menu is None:
            return
        url = host.activate(menu, item)
        if url:
            webbrowser.open(url)
        self._refresh()

    def _host_action(self, action: str) -> None:
        if action == "help":
            webbrowser.open(help_server.url())
        elif action == "quit":
            self._shutdown()
        elif action.startswith("start:"):
            self._start_bird(action[len("start:"):])

    def _start_bird(self, name: str) -> None:
        """Ask the supervisor to start a stopped bird, then refresh.

        The refresh is unconditional: the supervisor accepting the request is
        not the same as the bird being up. Its descriptor is what proves that,
        and the next poll reads it.
        """
        spec = self._model.launch_spec(name)
        if spec is None:
            return
        ok, reason = launcher.start(spec)
        if not ok:
            log.warning("Could not start %s: %s", name, reason)
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
            target=self._poll, name="roost-windows-poll", daemon=True
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
        raise SystemExit(f"{windows_support.TRAY_MODULE} is only available on Windows")

    paths.ensure_state_dir()
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        paths.log_path(), maxBytes=512_000, backupCount=1
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)

    # The same single-lock host election the macOS tray uses, so "exactly one
    # process draws the menu" is one mechanism rather than a per-platform guess.
    lock = host.HostLock()
    if not lock.acquire():
        if lock.failure == host.UNWRITABLE:
            log.error("Roost cannot start: %s", lock.reason)
            return 1
        return 0

    tray_app = None
    try:
        tray_app = RoostWindowsTray()

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
