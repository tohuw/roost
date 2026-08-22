"""Tests for the Windows toast sender.

Everything Windows-only in :mod:`roost.windows_toast` is imported inside a
function, so this file fakes the WinRT packages and ``winreg`` and runs on any
platform, like the rest of ``tests/unit``. What is being tested is the contract
the tray depends on: a toast that carries its click back to a callable, a
registration written where Windows looks for it, and every failure ending in
"fall back to the balloon" rather than in an exception out of the tray.
"""

import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import windows_toast


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeXmlDocument:
    def __init__(self):
        self.xml = ""

    def load_xml(self, xml):
        self.xml = xml


class _FakeToastNotification:
    def __init__(self, document):
        self.document = document
        self.tag = ""
        self.activated = []
        self.dismissed = []
        self.failed = []

    def add_activated(self, handler):
        self.activated.append(handler)

    def add_dismissed(self, handler):
        self.dismissed.append(handler)

    def add_failed(self, handler):
        self.failed.append(handler)


class _FakeNotifier:
    def __init__(self, error=None):
        self.shown = []
        self.error = error

    def show(self, notification):
        if self.error is not None:
            raise self.error
        self.shown.append(notification)


class _FakeManager:
    def __init__(self, notifier):
        self.notifier = notifier
        self.requested = []

    def create_toast_notifier_with_id(self, app_id):
        self.requested.append(app_id)
        if isinstance(self.notifier, Exception):
            raise self.notifier
        return self.notifier


class _FakeRegistry:
    """Enough of ``winreg`` to record what a registration would write."""

    HKEY_CURRENT_USER = object()
    KEY_WRITE = 0x20006
    REG_SZ = 1

    def __init__(self, error=None):
        self.values = {}
        self.deleted = []
        self.error = error

    def CreateKeyEx(self, root, path, reserved, access):  # noqa: N802 - winreg's name
        if self.error is not None:
            raise self.error
        registry = self

        class _Key:
            path_ = path

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        key = _Key()
        registry.values.setdefault(path, {})
        return key

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[key.path_][name] = value

    def DeleteKey(self, root, path):  # noqa: N802
        if self.error is not None:
            raise self.error
        self.deleted.append(path)


def _install_winrt(monkeypatch, manager, *, broken=False):
    """Put a fake WinRT package tree where the lazy imports will find it."""
    if broken:
        for name in ("winrt.windows.ui.notifications", "winrt.windows.data.xml.dom"):
            monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError
        return
    notifications = types.ModuleType("winrt.windows.ui.notifications")
    notifications.ToastNotification = _FakeToastNotification
    notifications.ToastNotificationManager = manager
    dom = types.ModuleType("winrt.windows.data.xml.dom")
    dom.XmlDocument = _FakeXmlDocument
    for name, module in (
        ("winrt.windows.ui.notifications", notifications),
        ("winrt.windows.data.xml.dom", dom),
    ):
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def registry(monkeypatch):
    fake = _FakeRegistry()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


# ── The payload ───────────────────────────────────────────────────────────────

class TestToastXml:
    def test_both_lines_are_present(self):
        xml = windows_toast.toast_xml("Huginn", "alpha: waiting for permission")
        assert "<text>Huginn</text>" in xml
        assert "<text>alpha: waiting for permission</text>" in xml

    def test_a_control_character_cannot_break_the_document(self):
        """XML 1.0 has no representation for one, escaped or not.

        A label carrying one does not render wrong -- it makes the payload
        unparseable, and the toast never appears at all. The menu sanitises
        on the way in; this is the assertion that the toast does not depend
        on that.
        """
        bell, escape_byte = chr(7), chr(27)
        noisy = f"alpha{bell}: {escape_byte}[31mwaiting"

        xml = windows_toast.toast_xml("Huginn", noisy)

        assert bell not in xml
        assert escape_byte not in xml
        # The colour sequence goes whole, rather than leaving a bare "[31m".
        assert "<text>alpha: waiting</text>" in xml

    def test_a_very_long_detail_does_not_become_a_very_long_toast(self):
        xml = windows_toast.toast_xml("Huginn", "x" * 5000)

        assert len(xml) < 1000

    def test_text_from_a_bird_cannot_break_the_document(self):
        """A worktree path with an ampersand in it is a real menu label."""
        xml = windows_toast.toast_xml("Huginn", "C:/work/r&d <alpha>")
        assert "r&amp;d &lt;alpha&gt;" in xml
        assert "<alpha>" not in xml


# ── Registration ──────────────────────────────────────────────────────────────

class TestRegister:
    def test_the_app_id_is_written_where_windows_reads_it(self, registry):
        assert windows_toast.register("C:/roost/bird.ico") is True
        written = registry.values[rf"Software\Classes\AppUserModelId\{windows_toast.APP_ID}"]
        assert written == {"DisplayName": "Roost", "IconUri": "C:/roost/bird.ico"}

    def test_no_icon_writes_no_icon_value(self, registry):
        windows_toast.register(None)
        written = registry.values[rf"Software\Classes\AppUserModelId\{windows_toast.APP_ID}"]
        assert "IconUri" not in written

    def test_a_registry_that_refuses_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "winreg", _FakeRegistry(error=OSError("denied")))
        assert windows_toast.register() is False

    def test_unregistering_removes_the_key(self, registry):
        windows_toast.register()
        windows_toast.unregister()
        assert registry.deleted == [
            rf"Software\Classes\AppUserModelId\{windows_toast.APP_ID}"
        ]

    def test_unregistering_something_absent_is_not_an_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "winreg", _FakeRegistry(error=FileNotFoundError()))
        windows_toast.unregister()


# ── Showing ───────────────────────────────────────────────────────────────────

class TestToaster:
    def test_a_machine_without_the_winrt_packages_falls_back(self, monkeypatch, registry):
        _install_winrt(monkeypatch, None, broken=True)
        toaster = windows_toast.Toaster()
        assert toaster.available is False
        assert toaster.show("Huginn", "alpha") is False

    def test_a_refused_registration_falls_back(self, monkeypatch):
        """Without the app id, a toast has no identity to be shown under."""
        monkeypatch.setitem(sys.modules, "winreg", _FakeRegistry(error=OSError("denied")))
        _install_winrt(monkeypatch, _FakeManager(_FakeNotifier()))
        assert windows_toast.Toaster().available is False

    def test_a_notifier_that_cannot_be_created_falls_back(self, monkeypatch, registry):
        _install_winrt(monkeypatch, _FakeManager(OSError("element not found")))
        assert windows_toast.Toaster().available is False

    def test_a_toast_is_shown_under_roosts_own_app_id(self, monkeypatch, registry):
        manager = _FakeManager(_FakeNotifier())
        _install_winrt(monkeypatch, manager)
        toaster = windows_toast.Toaster("C:/roost/bird.ico")

        assert toaster.show("Huginn", "alpha: waiting") is True
        assert manager.requested == [windows_toast.APP_ID]
        shown = manager.notifier.shown[0]
        assert "<text>alpha: waiting</text>" in shown.document.xml
        assert shown.tag

    def test_a_failed_show_falls_back_without_disabling_the_next_one(
        self, monkeypatch, registry
    ):
        """Notifications off, or a focus session, is a per-toast condition."""
        notifier = _FakeNotifier(error=OSError("no"))
        _install_winrt(monkeypatch, _FakeManager(notifier))
        toaster = windows_toast.Toaster()

        assert toaster.show("Huginn", "alpha") is False
        assert toaster.available is True

    def test_clicking_a_toast_runs_the_callback(self, monkeypatch, registry):
        manager = _FakeManager(_FakeNotifier())
        _install_winrt(monkeypatch, manager)
        toaster = windows_toast.Toaster()
        clicked = threading.Event()
        toaster.show("Huginn", "alpha", on_click=clicked.set)

        manager.notifier.shown[0].activated[0](None, None)

        assert clicked.wait(5), "the click never reached the callback"

    def test_a_callback_that_raises_cannot_take_the_tray_down(
        self, monkeypatch, registry
    ):
        manager = _FakeManager(_FakeNotifier())
        _install_winrt(monkeypatch, manager)
        toaster = windows_toast.Toaster()
        done = threading.Event()

        def boom():
            done.set()
            raise RuntimeError("bird refused")

        toaster.show("Huginn", "alpha", on_click=boom)
        manager.notifier.shown[0].activated[0](None, None)
        assert done.wait(5)

    def test_a_dismissed_toast_is_no_longer_held(self, monkeypatch, registry):
        manager = _FakeManager(_FakeNotifier())
        _install_winrt(monkeypatch, manager)
        toaster = windows_toast.Toaster()
        toaster.show("Huginn", "alpha")

        manager.notifier.shown[0].dismissed[0](None, None)

        assert toaster._live == {}

    def test_the_held_toasts_are_bounded(self, monkeypatch, registry):
        """Nobody clicks the hundredth toast; holding it forever is a leak."""
        manager = _FakeManager(_FakeNotifier())
        _install_winrt(monkeypatch, manager)
        toaster = windows_toast.Toaster()
        for index in range(windows_toast._MAX_LIVE + 5):
            toaster.show("Huginn", f"alpha {index}")

        assert len(toaster._live) == windows_toast._MAX_LIVE
