"""Tests for the macOS menu bar's rendering of the shared rows.

The tray is deliberately thin, so what is worth pinning is that it stays thin:
it renders whatever :mod:`tray` produced, it makes only host-row and enabled-item
rows clickable, it never reaches for a launcher, and a failed refresh does not
kill the poll timer (which would freeze the menu with no indication).
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# rumps is macOS-only and needs a real AppKit run loop, so the module is imported
# against a stand-in. The fakes below record what the tray asked for, which is
# exactly what these tests assert on.
fake_rumps = types.ModuleType("rumps")


class _FakeMenuItem:
    def __init__(self, title):
        self.title = title
        self.callback = None
        self.state = 0
        self.children = []

    def set_callback(self, callback):
        self.callback = callback

    def add(self, item):
        self.children.append(item)


class _FakeMenu:
    def __init__(self):
        self.items = []

    def clear(self):
        self.items = []

    def update(self, items):
        self.items.extend(items)


class _FakeApp:
    def __init__(self, *_args, **kwargs):
        self.kwargs = kwargs
        self.menu = _FakeMenu()
        self.icon = kwargs.get("icon")
        self.template = kwargs.get("template")
        self.title = kwargs.get("title", "")


fake_rumps.App = _FakeApp
fake_rumps.MenuItem = _FakeMenuItem
fake_rumps.timer = lambda _seconds: (lambda function: function)
fake_rumps.quit_application = lambda: None
sys.modules.setdefault("rumps", fake_rumps)

from roost import host
from roost import icons
from roost import menu_spec
from roost import menubar
from roost import ravens
from roost import tray
from roost.tray import RowKind


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(icons.paths, "STATE_DIR", tmp_path)
    monkeypatch.setattr(menubar.paths, "STATE_DIR", tmp_path)
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


def _app(model=None):
    """Build the tray without running an AppKit loop."""
    app = menubar.RoostApp.__new__(menubar.RoostApp)
    app.menu = _FakeMenu()
    app.icon = None
    app.template = None
    app.title = ""
    app._signature = None
    app._model = model if model is not None else host.MenuModel()
    return app


# ── Rendering ─────────────────────────────────────────────────────────────────

class TestRendering:
    def test_a_separator_row_renders_as_none(self):
        """rumps takes None as the separator, so the row must become None."""
        assert _app()._render(tray.Row(RowKind.SEPARATOR)) is None

    def test_an_enabled_item_is_clickable(self):
        row = tray.Row(RowKind.ITEM, label="Approve", raven="huginn",
                       item=menu_spec.MenuItem(label="Approve", action_id="a"),
                       enabled=True)
        assert _app()._render(row).callback is not None

    def test_a_disabled_item_is_inert(self):
        """A row that looks clickable and does nothing is worse than an inert one."""
        row = tray.Row(RowKind.ITEM, label="Approve", enabled=False,
                       item=menu_spec.MenuItem(label="Approve"))
        assert _app()._render(row).callback is None

    @pytest.mark.parametrize("kind", [RowKind.RAVEN, RowKind.REASON, RowKind.SECTION])
    def test_structural_rows_are_inert(self, kind):
        assert _app()._render(tray.Row(kind, label="Text")).callback is None

    def test_a_reason_row_is_still_rendered(self):
        """It must be visible: an omitted raven looks like an absent one."""
        item = _app()._render(tray.Row(RowKind.REASON, label="Is not answering."))
        assert item.title == "Is not answering."

    def test_a_host_row_is_clickable(self):
        row = tray.Row(RowKind.HOST, label="Help", action="help", enabled=True)
        assert _app()._render(row).callback is not None

    def test_a_submenu_row_renders_its_children(self):
        row = tray.Row(RowKind.HOST, label="Tray icon", action="icon", enabled=True,
                       children=(
                           tray.Row(RowKind.HOST, label="Raven", action="icon:raven",
                                    enabled=True, checked=True),
                           tray.Row(RowKind.HOST, label="Roost",
                                    action="icon:roost", enabled=True),
                       ))
        item = _app()._render(row)
        assert [child.title for child in item.children] == ["Raven", "Roost"]
        assert [child.state for child in item.children] == [1, 0]

    def test_the_whole_menu_is_built_from_the_shared_rows(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "Approve"),))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)
        app = _app()
        app._build_menu()
        titles = [item.title for item in app.menu.items if item is not None]
        assert "Huginn" in titles
        assert "Approve" in titles
        assert tray.HELP_LABEL in titles
        assert tray.QUIT_LABEL in titles

    def test_rebuilding_replaces_rather_than_appends(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "Approve"),))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)
        app = _app()
        app._build_menu()
        first = len(app.menu.items)
        app._build_menu()
        assert len(app.menu.items) == first


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
        app = _app(model)
        row = tray.Row(RowKind.ITEM, label="Approve", raven="huginn", enabled=True,
                       item=model.menus[0].spec.sections[0].items[0])
        app._make_activate(row)(None)
        assert seen == [("huginn", "act:Approve")]

    def test_a_url_result_is_opened(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "Console"),))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)
        monkeypatch.setattr(host, "activate", lambda *_a: "http://127.0.0.1:47100/")
        opened = []
        monkeypatch.setattr(menubar.webbrowser, "open", opened.append)
        app = _app(model)
        row = tray.Row(RowKind.ITEM, label="Console", raven="huginn", enabled=True,
                       item=model.menus[0].spec.sections[0].items[0])
        app._make_activate(row)(None)
        assert opened == ["http://127.0.0.1:47100/"]

    def test_a_click_on_a_raven_that_has_since_vanished_does_nothing(self, monkeypatch):
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: host.MenuModel())
        monkeypatch.setattr(
            host, "activate",
            lambda *_a: pytest.fail("activate must not run for a missing raven"),
        )
        app = _app(host.MenuModel())
        row = tray.Row(RowKind.ITEM, label="Gone", raven="huginn", enabled=True,
                       item=menu_spec.MenuItem(label="Gone", action_id="a"))
        app._make_activate(row)(None)

    def test_help_opens_the_local_help_page(self, monkeypatch):
        opened = []
        monkeypatch.setattr(menubar.help_server, "url", lambda: "http://127.0.0.1:1/")
        monkeypatch.setattr(menubar.webbrowser, "open", opened.append)
        _app()._host_action("help")
        assert opened == ["http://127.0.0.1:1/"]

    def test_choosing_an_icon_persists_it(self, monkeypatch):
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: host.MenuModel())
        app = _app()
        app._host_action(f"icon:{icons.DEFAULT_ICON}")
        assert icons.configured_icon() == icons.DEFAULT_ICON

    def test_quit_releases_the_host_lock_and_stops_the_help_server(self, monkeypatch):
        events = []
        monkeypatch.setattr(menubar.help_server, "shutdown",
                            lambda: events.append("help"))
        monkeypatch.setattr(menubar._HOST_LOCK, "release",
                            lambda: events.append("lock"))
        monkeypatch.setattr(menubar.rumps, "quit_application",
                            lambda: events.append("quit"))
        _app()._quit()
        assert events == ["help", "lock", "quit"]

    def test_quit_stops_no_raven(self):
        """The ravens are daemons the tray does not own, so it never stops them."""
        source = Path(menubar.__file__).read_text(encoding="utf-8")
        for forbidden in ("Popen", "os.kill", "SIGTERM", "SIGKILL", "execve"):
            assert forbidden not in source, forbidden


# ── There is no launcher left ────────────────────────────────────────────────

class TestNoLauncherRemains:
    def test_the_module_imports_no_launcher_module(self):
        source = Path(menubar.__file__).read_text(encoding="utf-8")
        for forbidden in ("import process", "import registry", "import launch",
                          "import cleanup", "import hooks"):
            assert forbidden not in source, forbidden

    def test_no_raven_id_is_special_cased(self):
        source = Path(menubar.__file__).read_text(encoding="utf-8")
        for name in ("huginn", "muninn", "Huginn", "Muninn"):
            assert name not in source, name


# ── Poll ──────────────────────────────────────────────────────────────────────

class TestPoll:
    def test_an_unchanged_model_does_not_rebuild(self, monkeypatch):
        model = host.MenuModel((_live_menu("huginn", "Approve"),))
        monkeypatch.setattr(host, "build_model", lambda *_a, **_k: model)
        monkeypatch.setattr(menubar, "refresh_zshenv", lambda: None)
        app = _app()
        app._build_menu()
        builds = []
        monkeypatch.setattr(app, "_build_menu", lambda *_a: builds.append(1))
        app._poll(None)
        assert builds == []

    def test_a_changed_model_rebuilds(self, monkeypatch):
        monkeypatch.setattr(menubar, "refresh_zshenv", lambda: None)
        monkeypatch.setattr(
            host, "build_model",
            lambda *_a, **_k: host.MenuModel((_live_menu("huginn", "Approve"),)),
        )
        app = _app()
        app._build_menu()
        monkeypatch.setattr(
            host, "build_model",
            lambda *_a, **_k: host.MenuModel((_live_menu("huginn", "Deny"),)),
        )
        app._poll(None)
        titles = [item.title for item in app.menu.items if item is not None]
        assert "Deny" in titles

    def test_a_failing_refresh_does_not_escape_the_timer(self, monkeypatch):
        """An exception out of the timer stops it, freezing the menu silently."""
        monkeypatch.setattr(menubar, "refresh_zshenv", lambda: None)
        monkeypatch.setattr(
            host, "build_model",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        _app()._poll(None)  # must not raise


# ── Notifications ─────────────────────────────────────────────────────────────

class TestNotify:
    def test_a_quote_cannot_terminate_the_applescript_literal(self, monkeypatch):
        """A raw quote would end the string and let the rest become script."""
        captured = {}
        monkeypatch.setattr(
            menubar.subprocess, "run",
            lambda argv, **_k: captured.setdefault("argv", argv),
        )
        menubar.notify("Roost", 'evil" & (do shell script "id") & "')
        script = captured["argv"][-1]
        # Every quote from the message survives only in escaped form, so the
        # payload stays inside the one string literal notify opened.
        assert '"' not in script.replace('\\"', "").split(
            "display notification ", 1
        )[1].split(" with title ", 1)[0].strip('"')

    def test_a_backslash_cannot_escape_the_escaping(self, monkeypatch):
        """A trailing backslash would otherwise turn our own \\" into a literal."""
        captured = {}
        monkeypatch.setattr(
            menubar.subprocess, "run",
            lambda argv, **_k: captured.setdefault("argv", argv),
        )
        menubar.notify("Roost", 'ends with a backslash\\')
        script = captured["argv"][-1]
        assert script.endswith('"')
        assert "\\\\" in script

    def test_a_control_character_never_reaches_osascript(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            menubar.subprocess, "run",
            lambda argv, **_k: captured.setdefault("argv", argv),
        )
        menubar.notify("Roost", "line\x1b[31mone\nline two")
        script = captured["argv"][-1]
        assert "\x1b" not in script
        assert "\n" not in script


# ── Icon ──────────────────────────────────────────────────────────────────────

class TestIconKwargs:
    def test_the_default_icon_is_passed_as_a_template_on_macos(self, monkeypatch):
        monkeypatch.setattr(menubar.icons, "resolve", lambda: icons.IconChoice(
            "raven", Path("/tmp/raven-template.png"), template=True, builtin=True
        ))
        kwargs = menubar._icon_kwargs()
        assert kwargs["template"] is True
        assert kwargs["icon"] == "/tmp/raven-template.png"

    def test_a_user_icon_is_not_a_template(self, monkeypatch):
        monkeypatch.setattr(menubar.icons, "resolve", lambda: icons.IconChoice(
            "mine.png", Path("/tmp/mine.png"), template=False, builtin=False
        ))
        assert menubar._icon_kwargs()["template"] is False

    def test_no_resolvable_icon_falls_back_to_a_title(self, monkeypatch):
        """A tray with no icon at all is unclickable; a text title is not."""
        monkeypatch.setattr(menubar.icons, "resolve", lambda: None)
        assert menubar._icon_kwargs() == {"title": "Ravens"}
