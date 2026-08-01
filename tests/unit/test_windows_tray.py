"""Hermetic behavior tests for the Windows tray menu and actions."""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import menubar
import process
import registry
import windows_support
import windows_tray
from registry import AppEntry


class _FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = list(items)


class _FakeMenuItem:
    def __init__(self, text, action=None, *, enabled=True):
        self.text = text
        self.action = action
        self.enabled = enabled


class _FakePystray:
    Menu = _FakeMenu
    MenuItem = _FakeMenuItem


def _entry(**overrides):
    values = {
        "id": "widget",
        "name": "Widget",
        "cwd": r"C:\Users\alice\widget",
        "command": r".venv\Scripts\python.exe ui\server.py",
        "port": 8009,
        "github_url": "https://github.com/example/widget",
    }
    values.update(overrides)
    return AppEntry(**values)


def _tray_without_native_dependencies():
    tray = windows_tray.AppistryWindowsTray.__new__(
        windows_tray.AppistryWindowsTray
    )
    tray._pystray = _FakePystray
    tray._menu_signature = None
    return tray


def test_windows_menu_exposes_full_running_app_and_global_actions(monkeypatch):
    app = _entry()
    yggdrasil = _entry(
        id="yggdrasil",
        name="Yggdrasil",
        github_url="https://github.com/example/yggdrasil",
    )
    about_path = Path(app.cwd) / ".yggdrasil" / "about.md"
    signature = ((app.id, True), (yggdrasil.id, False))
    monkeypatch.setattr(
        menubar,
        "_menu_state",
        lambda: (
            [(app, True, about_path), (yggdrasil, False, None)],
            signature,
        ),
    )
    tray = _tray_without_native_dependencies()

    menu = tray._build_menu()

    labels = [
        item.text
        for item in menu.items
        if item is not _FakeMenu.SEPARATOR
    ]
    assert labels == [
        "Running apps",
        "Search running apps...",
        "Widget",
        "Browse Apps",
        "Help",
        "Quit All",
        "Quit Appistry",
    ]
    app_item = next(item for item in menu.items if getattr(item, "text", "") == "Widget")
    assert [item.text for item in app_item.action.items if item is not _FakeMenu.SEPARATOR] == [
        "Open",
        "Stop",
        "Restart",
        "About",
        "GitHub",
    ]
    assert tray._menu_signature == signature


def test_windows_menu_shows_empty_running_state_without_search(monkeypatch):
    monkeypatch.setattr(menubar, "_menu_state", lambda: ([], ()))
    tray = _tray_without_native_dependencies()

    menu = tray._build_menu()

    labels = [
        item.text
        for item in menu.items
        if item is not _FakeMenu.SEPARATOR
    ]
    assert labels == [
        "No apps are running",
        "Help",
        "Quit All",
        "Quit Appistry",
    ]


def test_windows_restart_opens_readiness_page_and_restarts_app(monkeypatch):
    monkeypatch.setenv("YGG_LAUNCH_MODE", "browser")
    app = _entry()
    events = []
    monkeypatch.setattr(
        menubar,
        "_open_launch_page",
        lambda app_id: events.append(("open", app_id)),
    )
    monkeypatch.setattr(
        process,
        "stop",
        lambda app_id: events.append(("stop", app_id)) or True,
    )
    monkeypatch.setattr(
        process,
        "start",
        lambda entry: events.append(("start", entry.id)) or True,
    )
    tray = _tray_without_native_dependencies()
    tray._refresh_menu = lambda: events.append(("refresh", app.id))

    tray._restart_app(app)

    assert events == [
        ("open", "widget"),
        ("stop", "widget"),
        ("start", "widget"),
        ("refresh", "widget"),
    ]


def test_windows_browse_starts_stopped_yggdrasil(monkeypatch):
    yggdrasil = _entry(id="yggdrasil", name="Yggdrasil")
    events = []
    monkeypatch.setattr(
        menubar,
        "_open_launch_page",
        lambda app_id: events.append(("open", app_id)),
    )
    monkeypatch.setattr(process, "is_running", lambda _app_id: False)
    monkeypatch.setattr(
        process,
        "start",
        lambda entry: events.append(("start", entry.id)) or True,
    )
    tray = _tray_without_native_dependencies()
    tray._refresh_menu = lambda: events.append(("refresh", yggdrasil.id))

    tray._browse_apps(yggdrasil)

    assert events == [
        ("open", "yggdrasil"),
        ("start", "yggdrasil"),
        ("refresh", "yggdrasil"),
    ]


def test_windows_tray_waits_for_grace_period_before_removed_shortcut_cleanup(
    tmp_path, monkeypatch
):
    app = _entry()
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(registry, "load", lambda: [app])
    monkeypatch.setattr(
        windows_tray.time,
        "monotonic",
        MagicMock(side_effect=[100, 116]),
    )
    tray = _tray_without_native_dependencies()
    tray._known_shortcuts = {app.id}
    tray._shortcut_missing_since = {}
    handled = []
    tray._handle_removed = handled.append

    tray._check_removed_shortcuts()
    assert handled == []

    tray._check_removed_shortcuts()
    assert handled == [app]


def test_windows_removed_shortcut_stops_cleans_and_unregisters_app(
    tmp_path, monkeypatch
):
    app = _entry(cwd=str(tmp_path))
    events = []
    monkeypatch.setattr(process, "is_running", lambda _app_id: True)
    monkeypatch.setattr(
        process,
        "stop",
        lambda app_id: events.append(("stop", app_id)) or True,
    )
    monkeypatch.setattr(
        windows_tray.cleanup,
        "git_clean_project",
        lambda cwd: events.append(("clean", cwd)) or True,
    )
    monkeypatch.setattr(
        windows_support,
        "remove_registered_shortcut",
        lambda entry: events.append(("shortcut", entry.id)),
    )
    monkeypatch.setattr(
        registry,
        "remove",
        lambda app_id: events.append(("registry", app_id)),
    )
    tray = _tray_without_native_dependencies()
    tray._known_shortcuts = {app.id}
    tray._shortcut_missing_since = {app.id: 1.0}
    tray._icon = MagicMock()

    tray._handle_removed(app)

    assert events == [
        ("stop", "widget"),
        ("clean", tmp_path),
        ("shortcut", "widget"),
        ("registry", "widget"),
    ]
    tray._icon.notify.assert_called_once()


def test_windows_quit_all_stops_apps_and_local_services(tmp_path, monkeypatch):
    running = _entry(id="running", name="Running")
    stopped = _entry(id="stopped", name="Stopped")
    monkeypatch.setattr(registry, "APPISTRY_DIR", tmp_path)
    monkeypatch.setattr(registry, "load", lambda: [running, stopped])
    monkeypatch.setattr(process, "is_running", lambda app_id: app_id == "running")
    stopped_ids = []
    monkeypatch.setattr(
        process,
        "stop",
        lambda app_id: stopped_ids.append(app_id) or True,
    )
    hook_shutdown = MagicMock()
    help_shutdown = MagicMock()
    monkeypatch.setattr(menubar, "_hook_server_shutdown", hook_shutdown)
    monkeypatch.setattr(menubar, "_help_server_shutdown", help_shutdown)
    windows_support.tray_pid_path().write_text("1234", encoding="utf-8")
    tray = _tray_without_native_dependencies()
    tray._stop_event = threading.Event()
    tray._icon = MagicMock()

    tray._shutdown(stop_apps=True)

    assert stopped_ids == ["running"]
    assert windows_support.tray_pid_path().exists() is False
    assert tray._stop_event.is_set() is True
    hook_shutdown.assert_called_once_with()
    help_shutdown.assert_called_once_with()
    tray._icon.stop.assert_called_once_with()
