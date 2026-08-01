"""Hermetic tests for Windows path, shortcut, and control-server behavior."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import registry
import windows_support
from registry import AppEntry


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


def test_registered_shortcut_path_stays_inside_appistry_start_menu(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    entry = _entry(name="../../Startup/evil")

    result = windows_support.registered_shortcut_path(entry)

    assert result.parent == (windows_support.appistry_shortcuts_dir().resolve())
    assert result.suffix == ".lnk"
    assert ".." not in result.name


def test_build_registered_shortcut_targets_pythonw_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(windows_support, "_prepare_shortcut_icon", lambda _entry: None)
    captured = {}

    def create(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return path

    monkeypatch.setattr(windows_support, "_create_shortcut", create)
    appistry_dir = tmp_path / "Appistry Home"

    result = windows_support.build_registered_shortcut(_entry(), appistry_dir)

    assert result == captured["path"]
    assert captured["target"] == appistry_dir / ".venv" / "Scripts" / "pythonw.exe"
    assert str(appistry_dir / "appistry.py") in captured["arguments"]
    assert captured["arguments"].endswith("launch widget")
    assert captured["working_directory"] == appistry_dir


def test_safe_icon_source_rejects_relative_path_traversal(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not-an-image")

    result = windows_support._safe_icon_source(
        _entry(cwd=str(project), icon="../outside.png")
    )

    assert result is None


def test_safe_icon_source_rejects_oversized_file(tmp_path):
    icon = tmp_path / "large.png"
    icon.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

    result = windows_support._safe_icon_source(
        _entry(cwd=str(tmp_path), icon="large.png")
    )

    assert result is None


def test_prepare_shortcut_icon_rejects_excessive_pixel_dimensions(
    tmp_path, monkeypatch
):
    Image = pytest.importorskip("PIL.Image")

    source = tmp_path / "large-dimensions.png"
    source.write_bytes(b"small placeholder")
    monkeypatch.setattr(registry, "APPISTRY_DIR", tmp_path / ".appistry")

    class OversizedImage:
        width = 5000
        height = 5000

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def convert(self, _mode):
            raise AssertionError("oversized icon must not be decoded")

    monkeypatch.setattr(Image, "open", lambda _path: OversizedImage())

    result = windows_support._prepare_shortcut_icon(
        _entry(cwd=str(tmp_path), icon=source.name)
    )

    assert result is None


def test_control_server_running_rejects_invalid_port_file(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "APPISTRY_DIR", tmp_path)
    (tmp_path / "menubar-http-port").write_text("70000", encoding="utf-8")

    assert windows_support.control_server_running() is False


def test_control_server_running_requires_appistry_status_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "APPISTRY_DIR", tmp_path)
    (tmp_path / "menubar-http-port").write_text("54321", encoding="utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"service":"other","ok":true}'

    monkeypatch.setattr(windows_support.urllib.request, "urlopen", lambda *_a, **_k: Response())

    assert windows_support.control_server_running() is False


def test_path_contains_matches_normalized_entry(tmp_path):
    bin_dir = tmp_path / "Appistry" / "Scripts"
    path_value = str(tmp_path / "other") + windows_support.os.pathsep + str(bin_dir)

    assert windows_support._path_contains(path_value, bin_dir) is True


def test_remove_registered_shortcut_preserves_neighbor(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(registry, "APPISTRY_DIR", tmp_path / ".appistry")
    target = windows_support.registered_shortcut_path(_entry())
    target.parent.mkdir(parents=True)
    target.write_text("shortcut", encoding="utf-8")
    neighbor = target.parent / "Other.lnk"
    neighbor.write_text("keep", encoding="utf-8")

    windows_support.remove_registered_shortcut(_entry())

    assert target.exists() is False
    assert neighbor.read_text(encoding="utf-8") == "keep"


def test_refresh_environment_tracks_inherited_user_value(monkeypatch):
    monkeypatch.setattr(windows_support, "is_windows", lambda: True)
    monkeypatch.setattr(
        windows_support,
        "_read_registry_environment",
        lambda: {"APPISTRY_TEST_SETTING": "first"},
    )
    monkeypatch.setattr(windows_support, "_read_registry_path", lambda: "")
    monkeypatch.setenv("APPISTRY_TEST_SETTING", "first")
    windows_support._managed_environment.clear()

    windows_support.refresh_user_environment()
    monkeypatch.setattr(
        windows_support,
        "_read_registry_environment",
        lambda: {"APPISTRY_TEST_SETTING": "updated"},
    )
    windows_support.refresh_user_environment()

    assert windows_support.os.environ["APPISTRY_TEST_SETTING"] == "updated"


def test_refresh_environment_replaces_path_with_registry_path(monkeypatch):
    monkeypatch.setattr(windows_support, "is_windows", lambda: True)
    monkeypatch.setattr(windows_support, "_read_registry_environment", lambda: {})
    monkeypatch.setattr(
        windows_support,
        "_read_registry_path",
        lambda: r"C:\Windows\System32;C:\Users\alice\bin",
    )
    monkeypatch.setenv("PATH", "stale")
    windows_support._managed_environment.clear()

    windows_support.refresh_user_environment()

    assert windows_support.os.environ["PATH"] == (
        r"C:\Windows\System32;C:\Users\alice\bin"
    )


def test_start_tray_returns_true_when_control_server_is_already_running(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_support, "is_windows", lambda: True)
    monkeypatch.setattr(windows_support, "control_server_running", lambda: True)
    popen = MagicMock()
    monkeypatch.setattr(windows_support.subprocess, "Popen", popen)

    assert windows_support.start_tray(tmp_path) is True
    popen.assert_not_called()


def test_start_tray_reports_early_process_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_support, "is_windows", lambda: True)
    monkeypatch.setattr(windows_support, "control_server_running", lambda: False)
    proc = MagicMock()
    proc.poll.return_value = 1
    monkeypatch.setattr(windows_support.subprocess, "Popen", MagicMock(return_value=proc))

    assert windows_support.start_tray(tmp_path) is False


def test_start_tray_terminates_child_when_control_server_times_out(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(windows_support, "is_windows", lambda: True)
    monkeypatch.setattr(windows_support, "control_server_running", lambda: False)
    monkeypatch.setattr(windows_support.time, "monotonic", MagicMock(side_effect=[0, 9]))
    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(
        windows_support.subprocess,
        "Popen",
        MagicMock(return_value=proc),
    )

    assert windows_support.start_tray(tmp_path) is False
    proc.terminate.assert_called_once_with()
    proc.wait.assert_called_once_with(timeout=3)


def test_named_mutex_uses_windows_error_constant_module(monkeypatch):
    closed_handles = []
    monkeypatch.setattr(windows_support, "is_windows", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        SimpleNamespace(
            GetLastError=lambda: 183,
            CloseHandle=closed_handles.append,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32event",
        SimpleNamespace(CreateMutex=lambda *_args: "mutex-handle"),
    )
    monkeypatch.setitem(
        sys.modules,
        "winerror",
        SimpleNamespace(ERROR_ALREADY_EXISTS=183),
    )

    mutex = windows_support.NamedMutex("Local\\AppistryTest")

    assert mutex.acquire() is False
    assert mutex.handle is None
    assert closed_handles == ["mutex-handle"]
