"""Tests for launch.py — launch-mode resolution and the dedicated-window path.

These tests never open a real GUI window: the ygg_shell subprocess spawn is
always stubbed so the suite stays headless.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launch
from registry import AppEntry


def _entry(**overrides):
    base = dict(
        id="notekeeper",
        name="Notekeeper",
        cwd="/tmp/notekeeper",
        command="scripts/start.sh",
        port=8000,
        github_url="https://github.com/example/notekeeper",
    )
    base.update(overrides)
    return AppEntry(**base)


class TestResolveLaunchMode:
    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YGG_LAUNCH_MODE", "shell")
        # Even with a file present, the env var wins.
        monkeypatch.setattr(launch.Path, "home", staticmethod(lambda: tmp_path))
        (tmp_path / ".yggdrasil").mkdir()
        (tmp_path / ".yggdrasil" / "launch-mode").write_text("browser\n")
        assert launch.resolve_launch_mode() == "shell"

    def test_falls_back_to_mode_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YGG_LAUNCH_MODE", raising=False)
        monkeypatch.setattr(launch.Path, "home", staticmethod(lambda: tmp_path))
        (tmp_path / ".yggdrasil").mkdir()
        (tmp_path / ".yggdrasil" / "launch-mode").write_text("shell\nignored second line\n")
        assert launch.resolve_launch_mode() == "shell"

    def test_defaults_to_shell(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YGG_LAUNCH_MODE", raising=False)
        monkeypatch.setattr(launch.Path, "home", staticmethod(lambda: tmp_path))
        assert launch.resolve_launch_mode() == "shell"

    def test_env_var_is_normalised(self, monkeypatch):
        monkeypatch.setenv("YGG_LAUNCH_MODE", "  SHELL  ")
        assert launch.resolve_launch_mode() == "shell"

    def test_is_shell_mode_reflects_resolution(self, monkeypatch):
        monkeypatch.setenv("YGG_LAUNCH_MODE", "shell")
        assert launch.is_shell_mode() is True
        monkeypatch.setenv("YGG_LAUNCH_MODE", "browser")
        assert launch.is_shell_mode() is False


class TestOpenDedicatedWindow:
    def test_spawns_shell_with_secret_and_title(self, monkeypatch):
        popen_calls = {}

        class FakeProc:
            def wait(self):
                return 0

        def fake_popen(cmd, env=None):
            popen_calls["cmd"] = cmd
            popen_calls["env"] = env
            return FakeProc()

        monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(launch.process, "read_launch_secret", lambda _id: "the-secret")
        # Pretend ygg_shell.py exists so we exercise the spawn path.
        monkeypatch.setattr(launch.Path, "is_file", lambda self: True)

        code = launch.open_dedicated_window(_entry(), block=True)

        assert code == 0
        cmd = popen_calls["cmd"]
        assert "--url" in cmd and "http://127.0.0.1:8000" in cmd
        assert "--title" in cmd and "Notekeeper" in cmd
        assert "--no-activate" in cmd
        assert "--secret-in-query" not in cmd  # fragment mode is the default
        assert popen_calls["env"]["YGG_LAUNCH_SECRET"] == "the-secret"

    def test_query_secret_mode_adds_flag(self, monkeypatch):
        popen_calls = {}

        def fake_popen(cmd, env=None):
            popen_calls["cmd"] = cmd
            return _Done()

        monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(launch.process, "read_launch_secret", lambda _id: "s")
        monkeypatch.setattr(launch.Path, "is_file", lambda self: True)

        launch.open_dedicated_window(_entry(), secret_mode="query", block=True)
        assert "--secret-in-query" in popen_calls["cmd"]

    def test_no_backend_exit_falls_back_to_browser(self, monkeypatch):
        class FakeProc:
            def wait(self):
                return 3  # ygg_shell: no webview backend

        monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, env=None: FakeProc())
        monkeypatch.setattr(launch.process, "read_launch_secret", lambda _id: "s")
        monkeypatch.setattr(launch.Path, "is_file", lambda self: True)
        opened = []
        monkeypatch.setattr(launch.webbrowser, "open", opened.append)

        code = launch.open_dedicated_window(_entry(), block=True)

        assert code == 3
        assert opened == ["http://localhost:8000"]

    def test_missing_shell_script_falls_back_to_browser(self, monkeypatch):
        monkeypatch.setattr(launch.Path, "is_file", lambda self: False)
        opened = []
        monkeypatch.setattr(launch.webbrowser, "open", opened.append)
        # Popen must never be called when the script is absent.
        monkeypatch.setattr(
            launch.subprocess, "Popen",
            MagicMock(side_effect=AssertionError("should not spawn")),
        )

        code = launch.open_dedicated_window(_entry(), block=True)

        assert code == 0
        assert opened == ["http://localhost:8000"]

    def test_absent_secret_omits_env_var(self, monkeypatch):
        popen_calls = {}

        class FakeProc:
            def wait(self):
                return 0

        def fake_popen(cmd, env=None):
            popen_calls["env"] = env
            return FakeProc()

        monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(launch.process, "read_launch_secret", lambda _id: None)
        monkeypatch.setattr(launch.Path, "is_file", lambda self: True)
        monkeypatch.setenv("YGG_LAUNCH_SECRET", "stale-parent-value")

        launch.open_dedicated_window(_entry(), block=True)

        # A missing per-app secret must not leak a stale parent-env value.
        assert "YGG_LAUNCH_SECRET" not in popen_calls["env"]


class _Done:
    def wait(self):
        return 0
