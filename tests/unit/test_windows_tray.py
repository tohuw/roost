"""Tests for the Windows tray's rendering of the shared rows.

The point of these tests is parity: the Windows tray and the macOS tray consume
the same rows from :mod:`tray`, so the menu must not differ between platforms
except in how a row is drawn. The last test in this file asserts that directly by
building both menus from one model and comparing the labels.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import host
from roost import icons
from roost import menu_spec
from roost import ravens
from roost import tray
from roost import windows_tray
from roost.tray import RowKind


class _FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = list(items)


class _FakeMenuItem:
    def __init__(self, text, action=None, *, enabled=True, checked=None, radio=False):
        self.text = text
        self.action = action
        self.enabled = enabled
        self.checked = checked
        self.radio = radio


class _FakePystray:
    Menu = _FakeMenu
    MenuItem = _FakeMenuItem


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_tray.paths, "STATE_DIR", tmp_path)
    return tmp_path


def _descriptor(name="huginn", port=47100):
    return ravens.RavenDescriptor(
        name=name, display=name.title(), api_version=1, min_api=1, max_api=1,
        pid=1, port=port, token_path=None, token_header="", endpoints={},
        host_priority=0, started=None, path=Path(f"/tmp/{name}.json"),
    )


def _live_menu(name="huginn", *labels, badge=0):
    items = tuple(
        menu_spec.MenuItem(label=label, action_id=f"act:{label}") for label in labels
    )
    return menu_spec.RavenMenu(
        name=name, display=name.title(),
        spec=menu_spec.MenuSpec(badge=badge, sections=(
            menu_spec.MenuSection(id="s", title="Sessions", items=items),
        )),
        descriptor=_descriptor(name),
    )


def _tray(model=None):
    """Build the tray without pystray, PIL, or a real Windows session."""
    instance = windows_tray.RoostWindowsTray.__new__(
        windows_tray.RoostWindowsTray
    )
    instance._pystray = _FakePystray
    instance._signature = None
    instance._model = model if model is not None else host.MenuModel()
    instance._state_lock = threading.RLock()
    instance._stop_event = threading.Event()
    return instance


def _texts(menu):
    return [item.text for item in menu.items if item is not _FakeMenu.SEPARATOR]


# ── Rendering ─────────────────────────────────────────────────────────────────

class TestRendering:
    def test_a_separator_row_renders_as_the_pystray_separator(self):
        assert _tray()._render(tray.Row(RowKind.SEPARATOR)) is _FakeMenu.SEPARATOR

    def test_an_enabled_item_is_clickable(self):
        row = tray.Row(RowKind.ITEM, label="Approve", raven="huginn", enabled=True,
                       item=menu_spec.MenuItem(label="Approve", action_id="a"))
        assert _tray()._render(row).action is not None

    def test_a_disabled_item_is_shown_but_inert(self):
        row = tray.Row(RowKind.ITEM, label="Approve", enabled=False,
                       item=menu_spec.MenuItem(label="Approve"))
        item = _tray()._render(row)
        assert item.enabled is False
        assert item.action is None
        assert item.text == "Approve"

    @pytest.mark.parametrize("kind", [RowKind.RAVEN, RowKind.REASON, RowKind.SECTION])
    def test_structural_rows_are_shown_and_inert(self, kind):
        item = _tray()._render(tray.Row(kind, label="Text"))
        assert item.enabled is False
        assert item.text == "Text"


    def test_the_whole_menu_is_built_from_the_shared_rows(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "Approve"),))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)
        menu = _tray()._build_menu()
        texts = _texts(menu)
        assert "Huginn" in texts
        assert "Approve" in texts
        assert tray.HELP_LABEL in texts
        assert tray.QUIT_LABEL in texts

    def test_an_unavailable_raven_renders_with_its_reason(self, monkeypatch):
        model = host.MenuModel((
            menu_spec.RavenMenu(name="muninn", display="Muninn",
                                reason="Is not answering."),
        ))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)
        texts = _texts(_tray()._build_menu())
        assert "Muninn" in texts
        assert "Is not answering." in texts

    def test_no_ravens_at_all_says_so(self, monkeypatch):
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: host.MenuModel())
        assert tray.NO_RAVENS_LABEL in _texts(_tray()._build_menu())


# ── Activation ────────────────────────────────────────────────────────────────

class TestActivation:
    def test_a_click_is_forwarded_to_the_publishing_raven(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "Approve"),))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)
        seen = []
        monkeypatch.setattr(
            host, "activate",
            lambda menu, item: seen.append((menu.name, item.action_id)) or None,
        )
        instance = _tray(model)
        instance._refresh = lambda *_a: None
        instance._activate(tray.Row(
            RowKind.ITEM, label="Approve", raven="huginn", enabled=True,
            item=model.menus[0].spec.sections[0].items[0],
        ))
        assert seen == [("huginn", "act:Approve")]

    def test_a_url_result_is_opened(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "Console"),))
        monkeypatch.setattr(host, "activate", lambda *_a: "http://127.0.0.1:47100/")
        opened = []
        monkeypatch.setattr(windows_tray.webbrowser, "open", opened.append)
        instance = _tray(model)
        instance._refresh = lambda *_a: None
        instance._activate(tray.Row(
            RowKind.ITEM, label="Console", raven="huginn", enabled=True,
            item=model.menus[0].spec.sections[0].items[0],
        ))
        assert opened == ["http://127.0.0.1:47100/"]

    def test_a_click_on_a_vanished_raven_does_nothing(self, monkeypatch):
        monkeypatch.setattr(
            host, "activate",
            lambda *_a: pytest.fail("activate must not run for a missing raven"),
        )
        instance = _tray(host.MenuModel())
        instance._refresh = lambda *_a: None
        instance._activate(tray.Row(
            RowKind.ITEM, label="Gone", raven="huginn", enabled=True,
            item=menu_spec.MenuItem(label="Gone", action_id="a"),
        ))

    def test_help_opens_the_local_help_page(self, monkeypatch):
        opened = []
        monkeypatch.setattr(windows_tray.help_server, "url",
                            lambda: "http://127.0.0.1:1/")
        monkeypatch.setattr(windows_tray.webbrowser, "open", opened.append)
        _tray()._host_action("help")
        assert opened == ["http://127.0.0.1:1/"]


    def test_an_unloadable_icon_does_not_break_the_tray(self, monkeypatch):
        """A bad image must not take the menu down; the old bitmap stays."""
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: host.MenuModel())
        monkeypatch.setattr(
            windows_tray, "_tray_image",
            lambda: (_ for _ in ()).throw(OSError("cannot identify image")),
        )
        instance = _tray()
        instance._icon = MagicMock()
        instance._host_action(f"icon:{icons.DEFAULT_ICON}")  # must not raise


# ── Lifecycle ─────────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_quit_stops_the_help_server_and_removes_the_pid_file(self, tmp_path):
        windows_tray.windows_support.tray_pid_path().write_text("1234", encoding="utf-8")
        instance = _tray()
        instance._icon = MagicMock()
        instance._shutdown()
        assert windows_tray.windows_support.tray_pid_path().exists() is False
        assert instance._stop_event.is_set() is True
        instance._icon.stop.assert_called_once_with()

    def test_quit_stops_no_raven(self):
        """There is no Quit All: the ravens are daemons the tray does not own.

        The tray spawns nothing and signals nothing. It does *install* SIGTERM
        and SIGINT handlers so it can close its own window cleanly, which is the
        opposite concern — receiving a signal, not sending one.
        """
        source = Path(windows_tray.__file__).read_text(encoding="utf-8")
        for forbidden in ("Popen", "os.kill", "proc.terminate", "process.stop",
                          "signal.raise_signal", "psutil"):
            assert forbidden not in source, forbidden

    def test_shutdown_is_idempotent(self, tmp_path):
        instance = _tray()
        instance._icon = MagicMock()
        instance._shutdown()
        instance._shutdown()
        instance._icon.stop.assert_called_once_with()

    def test_a_failing_poll_does_not_kill_the_thread(self, monkeypatch):
        """If the poll thread dies the menu silently freezes at its last contents."""
        monkeypatch.setattr(windows_tray.windows_support,
                            "refresh_user_environment", lambda: None)
        monkeypatch.setattr(
            host, "build_model",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        instance = _tray()
        instance._stop_event = threading.Event()

        # One iteration, then stop.
        waits = [False, True]
        monkeypatch.setattr(instance._stop_event, "wait", lambda _t: waits.pop(0))
        instance._poll()  # must not raise
        assert waits == []

    def test_an_unchanged_model_does_not_refresh(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "Approve"),))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)
        monkeypatch.setattr(windows_tray.windows_support,
                            "refresh_user_environment", lambda: None)
        instance = _tray()
        instance._build_menu()
        refreshes = []
        instance._refresh = lambda *_a: refreshes.append(1)
        waits = [False, True]
        monkeypatch.setattr(instance._stop_event, "wait", lambda _t: waits.pop(0))
        instance._poll()
        assert refreshes == []


# ── There is no launcher left ────────────────────────────────────────────────

class TestNoLauncherRemains:
    def test_the_module_imports_no_launcher_module(self):
        """None of Appistry's app-launching machinery came along.

        Matched on whole module names, not substrings: `roost.launcher` asks a
        supervisor to start a *raven* by identifier, which is a different thing
        from Appistry's `launch` module that ran arbitrary apps -- and a
        substring check called the legitimate one a violation.
        """
        import ast

        tree = ast.parse(Path(windows_tray.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("process", "registry", "launch", "cleanup", "menubar"):
            assert forbidden not in imported, forbidden

    def test_no_raven_id_is_special_cased(self):
        source = Path(windows_tray.__file__).read_text(encoding="utf-8")
        for name in ("huginn", "muninn", "Huginn", "Muninn"):
            assert name not in source, name


# ── It draws exactly the shared rows ─────────────────────────────────────────

class TestItDrawsTheSharedRows:
    """The rendered menu must be the shared rows and nothing else.

    The previous design let each tray assemble its own structure from raw state,
    and they drifted until each had separately hardcoded a special case for one
    participant's id. Pinning the rendered labels against ``tray.build_rows`` is
    what makes that drift impossible: the tray cannot add, drop, or reorder a row
    without failing here. ``tests/unit/test_tray_parity.py`` then checks the macOS
    tray against the same rows.
    """

    def test_the_rendered_labels_are_exactly_the_row_labels(self, monkeypatch):
        model = host.MenuModel((
            _live_menu("huginn", "Approve", badge=2),
            menu_spec.RavenMenu(name="muninn", display="Muninn", reason="Gone."),
        ))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)

        rows = tray.build_rows(model)
        expected = [row.label for row in rows if row.kind is not RowKind.SEPARATOR]

        assert _texts(_tray()._build_menu()) == expected

    def test_the_separators_land_in_the_same_places(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "A"), _live_menu("muninn", "B")))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)

        rows = tray.build_rows(model)
        expected = [row.kind is RowKind.SEPARATOR for row in rows]
        actual = [
            item is _FakeMenu.SEPARATOR for item in _tray()._build_menu().items
        ]

        assert actual == expected
