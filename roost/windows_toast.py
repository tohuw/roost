"""Windows toasts the notification centre keeps, and clicks that reach us.

``pystray.Icon.notify`` draws the *tray icon's* balloon (``Shell_NotifyIcon``
with ``NIF_INFO``). Windows 11 renders it in the toast style, so it looks like
the real thing, but it is not one in the two ways that matter: the shell shows
it and forgets it, so a toast that appeared while the screen was locked is gone
with no record in the notification centre, and it carries no activation, so a
click dismisses it and nothing else happens. Both were reported together, and
they have one cause -- the balloon belongs to an icon, not to an application
Windows knows by name.

A real toast needs an **AppUserModelID**: the identity Windows files a
notification under, shows in Settings > Notifications, and hands back on a
click. A packaged app gets one from its manifest and a shipped desktop app from
a Start Menu shortcut; a shortcut is not usable here, because the tray must keep
working for someone who installed Roost before this file existed and has not
re-run ``roost install``. The remaining registration is the one under
``HKCU\\Software\\Classes\\AppUserModelId`` -- per-user, unprivileged, and read
by the same notification platform. :func:`register` writes it, and
``roost uninstall`` removes it.

Activation is delivered **in-process**, to the ``Activated`` event on the
notification object this process created. That is the whole reason
:class:`Toaster` keeps a reference to every toast it has shown: drop it and the
event has nowhere to arrive. It also bounds what a click can reach -- a toast
clicked out of the notification centre after the tray has exited quietly does
nothing, rather than starting something. Roost registers no protocol handler and
no COM activator, so nothing outside this process can invoke a Roost action.

Everything Windows-only is imported inside a function, as in
:mod:`roost.windows_support`, so the shared unit suite can exercise this file on
any platform.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Callable
from xml.sax.saxutils import escape

from roost import sanitize

log = logging.getLogger(__name__)

#: Roost's AppUserModelID. ``Company.Product``, the shape Windows documents, and
#: prefixed like every other name this project owns -- the separate internal
#: Appistry may register one of its own on the same machine, and two apps sharing
#: an id share a notification history and a Settings entry.
APP_ID = "tohuw.Roost"

#: What the notification centre and Settings call us. Not "Birds" and not the
#: menu's contents: this names *this app*, the same identity rule the tray
#: tooltip follows.
DISPLAY_NAME = "Roost"

#: The registry path, under HKEY_CURRENT_USER, that makes ``APP_ID`` a name
#: Windows will accept from an unpackaged process.
_APP_ID_KEY = rf"Software\Classes\AppUserModelId\{APP_ID}"

#: How many shown toasts keep a reference so their ``Activated`` event can still
#: be delivered. Old entries are dropped oldest-first; a toast that has scrolled
#: this far down the notification centre has been there for hours, and the tray
#: is not the place to hold an unbounded history of them.
_MAX_LIVE = 32

#: Windows rejects a tag longer than this, and rejects a toast whose tag it
#: rejects -- so it is truncated here rather than discovered as a failed show.
_MAX_TAG = 64

#: How much of a message survives into the toast. Windows truncates the second
#: line to a couple of rendered lines anyway; this bounds what is built, so a
#: bird that publishes a very long detail cannot turn each toast into a large
#: XML document.
MAX_MESSAGE = 300


def register(icon_path: str | None = None) -> bool:
    """Make ``APP_ID`` an application Windows will accept a toast from.

    Idempotent, and cheap enough to call on every tray start: the values are
    rewritten rather than compared, because a half-written key from an
    interrupted earlier run is indistinguishable from a correct one until it
    fails at show time. Returns False if the key could not be written, which is
    the tray's cue to fall back to the balloon rather than to show nothing.
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover - not Windows
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _APP_ID_KEY, 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, DISPLAY_NAME)
            if icon_path:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path))
    except OSError as exc:
        log.warning("Could not register the Roost application id: %s", exc)
        return False
    return True


def unregister() -> None:
    """Remove the registration. Never fails: absence is the wanted end state."""
    try:
        import winreg
    except ImportError:  # pragma: no cover - not Windows
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _APP_ID_KEY)
    except OSError:
        pass


def toast_xml(title: str, message: str) -> str:
    """The ToastGeneric payload for one two-line toast.

    Both strings are sanitised and then escaped, in that order and for two
    different reasons -- the same pair :func:`roost.menubar.notify` applies
    before handing text to AppleScript. Sanitising is what removes the control
    characters XML 1.0 has no representation for at all: one of those in a
    label would not be *rendered* wrong, it would make the document
    unparseable and the toast would simply never appear. Escaping is what keeps
    an ampersand in a worktree path from doing the same thing.

    A bird's menu has already been through the same sanitiser by the time it
    reaches here. This is the boundary that must not depend on that.
    """
    safe_title = escape(sanitize.sanitize_label(title))
    safe_message = escape(sanitize.sanitize_label(message, MAX_MESSAGE))
    return (
        '<toast activationType="foreground">'
        '<visual><binding template="ToastGeneric">'
        f"<text>{safe_title}</text>"
        f"<text>{safe_message}</text>"
        "</binding></visual></toast>"
    )


class Toaster:
    """Shows toasts under Roost's own application id, and routes their clicks.

    Unavailable is a normal state, not an error: the WinRT packages are a
    Windows-only requirement that an existing install may not have yet, and the
    caller is expected to fall back to the tray balloon. Every failure path here
    ends in ``available`` going False or :meth:`show` returning False -- a tray
    must not die of a notification.
    """

    def __init__(self, icon_path: str | None = None):
        self._icon_path = str(icon_path) if icon_path else None
        self._lock = threading.Lock()
        # Kept so each toast's Activated event has somewhere to arrive; see the
        # module docstring. Ordered so the oldest is the one evicted.
        self._live: "OrderedDict[str, object]" = OrderedDict()
        self._sequence = 0
        self._notifier = None
        self._toast_type = None
        self._xml_type = None
        self._available = self._load()

    @property
    def available(self) -> bool:
        return self._available

    def _load(self) -> bool:
        try:
            from winrt.windows.data.xml.dom import XmlDocument
            from winrt.windows.ui.notifications import (
                ToastNotification,
                ToastNotificationManager,
            )
        except (ImportError, OSError) as exc:
            # OSError covers the WinRT runtime failing to initialise, which is
            # what an import on a non-Windows or headless machine looks like.
            log.info("Windows toasts unavailable, using the tray balloon: %s", exc)
            return False
        if not register(self._icon_path):
            return False
        try:
            self._notifier = ToastNotificationManager.create_toast_notifier_with_id(APP_ID)
        except OSError as exc:
            log.warning("Could not create a toast notifier: %s", exc)
            return False
        self._toast_type = ToastNotification
        self._xml_type = XmlDocument
        return True

    def show(
        self,
        title: str,
        message: str,
        *,
        on_click: Callable[[], None] | None = None,
    ) -> bool:
        """Show one toast. Returns False if the caller should fall back.

        ``on_click`` runs on a thread of its own. The event arrives on a WinRT
        thread that Windows expects back promptly, and the work behind a click
        is a call to a bird over HTTP -- bounded, but not instant.
        """
        if not self._available:
            return False
        tag = ""
        try:
            document = self._xml_type()
            document.load_xml(toast_xml(title, message))
            notification = self._toast_type(document)
            tag = self._next_tag()
            notification.tag = tag
            notification.add_activated(self._activation_handler(tag, on_click))
            notification.add_dismissed(self._retire_handler(tag))
            notification.add_failed(self._retire_handler(tag))
            self._remember(tag, notification)
            self._notifier.show(notification)
        except OSError as exc:
            # A show can fail for reasons that are none of the tray's business:
            # notifications off for this app, a user in a focus session, the
            # platform mid-restart. One failure must not cost every later toast,
            # so ``available`` is left alone and this one falls back.
            log.warning("Toast failed, using the tray balloon: %s", exc)
            self._forget(tag)
            return False
        return True

    def _next_tag(self) -> str:
        with self._lock:
            self._sequence += 1
            return f"roost-{self._sequence}"[:_MAX_TAG]

    def _remember(self, tag: str, notification: object) -> None:
        with self._lock:
            self._live[tag] = notification
            while len(self._live) > _MAX_LIVE:
                self._live.popitem(last=False)

    def _forget(self, tag: str) -> None:
        with self._lock:
            self._live.pop(tag, None)

    def _activation_handler(self, tag: str, on_click: Callable[[], None] | None):
        def activated(_sender, _args):
            self._forget(tag)
            if on_click is None:
                return
            threading.Thread(
                target=self._run_click, args=(on_click,),
                name="roost-toast-click", daemon=True,
            ).start()

        return activated

    def _retire_handler(self, tag: str):
        def retired(_sender, _args):
            self._forget(tag)

        return retired

    @staticmethod
    def _run_click(on_click: Callable[[], None]) -> None:
        try:
            on_click()
        except Exception:
            # This thread is the last frame between a click and the tray's log;
            # an exception here would otherwise be printed to a process with no
            # console and lost.
            log.exception("Toast click failed")
