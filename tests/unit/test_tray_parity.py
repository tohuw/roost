"""Both trays must show the same menu.

The two trays are separate files with separate widget APIs, so nothing in the
type system stops them from drifting. They did drift in a previous design of this
repository: each tray assembled its own structure from raw state, and each ended
up separately hardcoding a special case for one particular participant's id.

This module renders one model through *both* trays and compares the results. It
is the test that would have caught that drift, and it is the reason the decision
about what the menu contains lives in :mod:`tray` rather than in either tray.
"""

import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ── rumps stand-in (macOS-only, needs a real AppKit run loop) ─────────────────

fake_rumps = types.ModuleType("rumps")


class _RumpsMenuItem:
    def __init__(self, title):
        self.title = title
        self.callback = None
        self.state = 0
        self.children = []

    def set_callback(self, callback):
        self.callback = callback

    def add(self, item):
        self.children.append(item)


class _RumpsMenu:
    def __init__(self):
        self.items = []

    def clear(self):
        self.items = []

    def update(self, items):
        self.items.extend(items)


class _RumpsApp:
    def __init__(self, *_args, **kwargs):
        self.menu = _RumpsMenu()
        self.icon = kwargs.get("icon")
        self.template = kwargs.get("template")
        self.title = kwargs.get("title", "")


fake_rumps.App = _RumpsApp
fake_rumps.MenuItem = _RumpsMenuItem
fake_rumps.timer = lambda _seconds: (lambda function: function)
fake_rumps.quit_application = lambda: None
sys.modules.setdefault("rumps", fake_rumps)


# ── pystray stand-in ─────────────────────────────────────────────────────────

class _PystrayMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = list(items)


class _PystrayMenuItem:
    def __init__(self, text, action=None, *, enabled=True, checked=None, radio=False):
        self.text = text
        self.action = action
        self.enabled = enabled
        self.checked = checked


class _FakePystray:
    Menu = _PystrayMenu
    MenuItem = _PystrayMenuItem


from roost import host
from roost import icons
from roost import menu_spec
from roost import menubar
from roost import birds
from roost import tray
from roost import windows_tray
from roost.tray import RowKind


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    return tmp_path


def _descriptor(name):
    return birds.BirdDescriptor(
        name=name, display=name.title(), api_version=1, min_api=1, max_api=1,
        pid=1, port=47100, token_path=None, token_header="", endpoints={},
        host_priority=0, started=None, path=Path(f"/tmp/{name}.json"),
    )


def _live(name, *labels, badge=0, style="normal", detail=""):
    items = tuple(
        menu_spec.MenuItem(label=label, action_id=f"act:{label}", style=style,
                           detail=detail)
        for label in labels
    )
    return menu_spec.BirdMenu(
        name=name, display=name.title(),
        spec=menu_spec.MenuSpec(badge=badge, sections=(
            menu_spec.MenuSection(id="s", title="Sessions", items=items),
        )),
        descriptor=_descriptor(name),
    )


def _macos_menu(model):
    app = menubar.RoostApp.__new__(menubar.RoostApp)
    app.menu = _RumpsMenu()
    app.icon = None
    app.template = None
    app.title = ""
    app._signature = None
    app._model = model
    app._build_menu(model)
    return app


def _windows_menu(model):
    instance = windows_tray.RoostWindowsTray.__new__(
        windows_tray.RoostWindowsTray
    )
    instance._pystray = _FakePystray
    instance._signature = None
    instance._model = model
    instance._state_lock = threading.RLock()
    instance._stop_event = threading.Event()
    return instance._build_menu(model)


def _macos_labels(app):
    return [item.title for item in app.menu.items if item is not None]


def _windows_labels(menu):
    return [
        item.text for item in menu.items if item is not _PystrayMenu.SEPARATOR
    ]


def _macos_separators(app):
    return [item is None for item in app.menu.items]


def _windows_separators(menu):
    return [item is _PystrayMenu.SEPARATOR for item in menu.items]


MODELS = {
    "no birds": host.MenuModel(),
    "one live bird": host.MenuModel((_live("huginn", "Approve"),)),
    "two live birds": host.MenuModel((_live("huginn", "A"), _live("muninn", "B"))),
    "one unavailable": host.MenuModel((
        menu_spec.BirdMenu(name="muninn", display="Muninn", reason="Not running."),
    )),
    "mixed": host.MenuModel((
        _live("huginn", "Approve", badge=3),
        menu_spec.BirdMenu(name="muninn", display="Muninn", reason="Gone."),
    )),
    "styled and detailed": host.MenuModel((
        _live("huginn", "Approve", style="attention", detail="claude"),
    )),
    "up but silent": host.MenuModel((
        menu_spec.BirdMenu(name="huginn", display="Huginn",
                            descriptor=_descriptor("huginn")),
    )),
    "an unknown bird": host.MenuModel((_live("corvid-nine", "Row"),)),
}


@pytest.mark.parametrize("name", sorted(MODELS))
def test_both_trays_show_the_same_labels(name):
    model = MODELS[name]
    assert _macos_labels(_macos_menu(model)) == _windows_labels(_windows_menu(model))


@pytest.mark.parametrize("name", sorted(MODELS))
def test_both_trays_put_the_separators_in_the_same_places(name):
    model = MODELS[name]
    assert _macos_separators(_macos_menu(model)) == _windows_separators(
        _windows_menu(model)
    )


@pytest.mark.parametrize("name", sorted(MODELS))
def test_both_trays_agree_on_which_rows_are_interactive(name):
    """A row is interactive if it runs a callback or opens a submenu.

    The two toolkits express a submenu differently — rumps nests items under a
    parent with no callback, pystray sets the parent's action to a ``Menu`` — so
    the comparison is on the behaviour, not on the field.
    """
    model = MODELS[name]

    macos = [
        item.callback is not None or bool(item.children)
        for item in _macos_menu(model).menu.items
        if item is not None
    ]
    windows = [
        item.action is not None
        for item in _windows_menu(model).items
        if item is not _PystrayMenu.SEPARATOR
    ]

    assert macos == windows
    # An interactive row must exist in every case: Help and Quit are always
    # present, so a menu with nothing clickable would mean the tray is unusable.
    assert any(macos)


@pytest.mark.parametrize("name", sorted(MODELS))
def test_both_trays_render_exactly_the_shared_rows(name):
    """Neither tray may add, drop, or reorder a row on its own."""
    model = MODELS[name]
    expected = [
        row.label for row in tray.build_rows(model)
        if row.kind is not RowKind.SEPARATOR
    ]
    assert _macos_labels(_macos_menu(model)) == expected
    assert _windows_labels(_windows_menu(model)) == expected


