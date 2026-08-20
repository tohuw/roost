"""Tests for the CLI.

The CLI is small on purpose: the birds start themselves, so there is nothing to
register, start, stop, or open. What is worth pinning is that the launcher verbs
are really gone (a stale one would imply a launcher that no longer exists), that
``birds`` reports the unavailable ones with their reasons, and that descriptor
text — untrusted input — cannot write control characters into a terminal.
"""

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import cli
from roost import paths
from roost import birds


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "roost")
    monkeypatch.setenv("BIRDS_STATE_DIR", str(tmp_path / "birds"))
    return tmp_path


def _require_symlinks(tmp_path: Path) -> None:
    """Skip unless this machine will actually make a symlink.

    Windows refuses without Developer Mode or elevation, and that is a property
    of the machine rather than of the OS — a developer with it enabled should
    still get the coverage. So this asks by trying, instead of reading
    sys.platform and writing the whole platform off.
    """
    probe = tmp_path / "_symlink-probe"
    try:
        probe.symlink_to(tmp_path)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this machine cannot create symlinks ({exc.__class__.__name__})")
    probe.unlink()


def _write_descriptor(directory: Path, name: str, **overrides):
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_version": 1, "min_api": 1, "max_api": 1,
        "name": name, "display": name.title(),
        "pid": 1, "port": 47100, "host_priority": 0, "endpoints": {},
    }
    payload.update(overrides)
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


# ── The launcher verbs are gone ───────────────────────────────────────────────

class TestNoLauncherVerbs:
    """A verb for a launcher that no longer exists would be worse than none."""

    @pytest.mark.parametrize("verb", [
        "register", "unregister", "list", "start", "stop", "open", "launch",
        "run", "window", "hook-url", "migrate", "rebuild",
    ])
    def test_the_verb_is_not_offered(self, verb):
        assert verb not in cli.COMMANDS

    @pytest.mark.parametrize("verb", [
        "register", "start", "stop", "launch", "hook-url",
    ])
    def test_the_verb_is_rejected_by_the_parser(self, verb):
        with pytest.raises(SystemExit) as exit_info:
            cli.build_parser().parse_args([verb])
        assert exit_info.value.code != 0

    def test_the_module_imports_no_launcher_module(self):
        source = Path(cli.__file__).read_text(encoding="utf-8")
        for forbidden in ("import process", "import registry", "import launch",
                          "import cleanup", "import hooks"):
            assert forbidden not in source, forbidden

    def test_the_cli_spawns_no_bird(self):
        source = Path(cli.__file__).read_text(encoding="utf-8")
        for forbidden in ("os.execve", "os.kill", "SIGTERM"):
            assert forbidden not in source, forbidden

    def test_the_remaining_verbs_are_the_documented_ones(self):
        assert set(cli.COMMANDS) == {
            "install", "uninstall", "ui", "birds"
        }


# ── birds ────────────────────────────────────────────────────────────────────

class TestCmdBirds:
    def _run(self):
        return cli.cmd_birds(argparse.Namespace())

    def test_a_missing_directory_is_reported_not_an_error(self, capsys):
        assert self._run() == 0
        assert "does not exist yet" in capsys.readouterr().out

    def test_an_empty_directory_is_reported(self, tmp_path, capsys):
        (tmp_path / "birds").mkdir(parents=True)
        assert self._run() == 0
        assert "empty" in capsys.readouterr().out

    def test_a_live_bird_is_listed_with_its_details(self, tmp_path, monkeypatch, capsys):
        _write_descriptor(tmp_path / "birds", "huginn", port=47123, host_priority=100)
        monkeypatch.setattr(birds, "pid_is_alive", lambda *_a, **_k: True)

        assert self._run() == 0
        out = capsys.readouterr().out
        assert "Huginn" in out
        assert "47123" in out
        assert "100" in out

    def test_a_socket_transport_bird_is_listed_by_address_not_port(
        self, tmp_path, monkeypatch, capsys
    ):
        """A unix/pipe descriptor has no port; printing it as one would lie."""
        directory = tmp_path / "birds"
        directory.mkdir(parents=True)
        payload = {
            "api_version": 1, "min_api": 1, "max_api": 1,
            "name": "muninn", "display": "Muninn", "pid": 1, "host_priority": 0,
            "transport": "unix", "address": "/tmp/muninn.sock",
            "pages_dir": "/tmp/muninn/pages", "endpoints": {"menu": "menu"},
        }
        (directory / "muninn.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(birds, "pid_is_alive", lambda *_a, **_k: True)

        assert self._run() == 0
        out = capsys.readouterr().out
        assert "Muninn" in out
        assert "unix" in out
        assert "/tmp/muninn.sock" in out
        # "transport" legitimately contains "port" as a substring; assert on
        # the actual port-printing line rather than a bare substring check.
        assert "      port " not in out

    def test_an_unavailable_bird_is_listed_with_its_reason(
        self, tmp_path, monkeypatch, capsys
    ):
        """Omitting it would be indistinguishable from never having installed it."""
        _write_descriptor(tmp_path / "birds", "muninn")
        monkeypatch.setattr(birds, "pid_is_alive", lambda *_a, **_k: False)

        assert self._run() == 0
        out = capsys.readouterr().out
        assert "muninn" in out
        assert "Not running" in out

    def test_a_malformed_descriptor_is_reported_not_raised(self, tmp_path, capsys):
        directory = tmp_path / "birds"
        directory.mkdir(parents=True)
        (directory / "huginn.json").write_text("}{ not json", encoding="utf-8")

        assert self._run() == 0
        assert "JSON" in capsys.readouterr().out

    def test_a_token_is_reported_as_present_never_printed(
        self, tmp_path, monkeypatch, capsys
    ):
        secret = tmp_path / "token"
        secret.write_text("super-secret-value", encoding="utf-8")
        _write_descriptor(tmp_path / "birds", "huginn", token_path=str(secret))
        monkeypatch.setattr(birds, "pid_is_alive", lambda *_a, **_k: True)

        assert self._run() == 0
        out = capsys.readouterr().out
        assert "token    yes" in out
        assert "super-secret-value" not in out
        assert str(secret) not in out

    def test_a_control_character_in_a_descriptor_never_reaches_the_terminal(
        self, tmp_path, capsys
    ):
        """A descriptor is untrusted input; an ANSI escape rewrites the terminal."""
        directory = tmp_path / "birds"
        directory.mkdir(parents=True)
        (directory / "huginn.json").write_text(
            json.dumps({"api_version": 1, "name": "huginn",
                        "display": "Huginn\x1b[2KFAKE", "pid": 1, "port": 1}),
            encoding="utf-8",
        )

        assert self._run() == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x00" not in out

    def test_the_descriptor_directory_is_printed(self, tmp_path, capsys):
        self._run()
        assert str(tmp_path / "birds") in capsys.readouterr().out


# ── icon ──────────────────────────────────────────────────────────────────────

class TestParser:
    def test_no_verb_prints_help_and_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["roost"])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_help_exits_zero(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["roost", "--help"])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == 0

    def test_every_verb_has_a_handler(self):
        parser = cli.build_parser()
        actions = [
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        assert actions
        for verb in actions[0].choices:
            assert verb in cli.COMMANDS, verb


# ── ui ────────────────────────────────────────────────────────────────────────

class TestCmdUi:
    def test_a_running_menu_bar_is_not_started_twice(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.windows_support, "is_windows", lambda: False)
        monkeypatch.setattr(cli.help_server, "active_port", lambda: 54321)
        monkeypatch.setattr(cli, "_macos_tray_responding", lambda: True)
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *_a, **_k: pytest.fail("launchctl must not run"),
        )

        assert cli.cmd_ui(argparse.Namespace()) == 0
        assert "already running" in capsys.readouterr().out

    def test_a_stale_port_file_does_not_block_a_start(self, monkeypatch):
        """A crashed tray leaves its port file behind; probing is what decides."""
        monkeypatch.setattr(cli.windows_support, "is_windows", lambda: False)
        monkeypatch.setattr(cli.help_server, "active_port", lambda: 54321)
        monkeypatch.setattr(cli, "_macos_tray_responding", lambda: False)
        calls = []

        class Result:
            returncode = 0

        monkeypatch.setattr(cli.subprocess, "run",
                            lambda argv, **_k: calls.append(argv) or Result())

        assert cli.cmd_ui(argparse.Namespace()) == 0
        assert calls == [["launchctl", "start", cli.LABEL]]

    def test_a_launchd_failure_is_reported(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.windows_support, "is_windows", lambda: False)
        monkeypatch.setattr(cli.help_server, "active_port", lambda: None)

        class Result:
            returncode = 1

        monkeypatch.setattr(cli.subprocess, "run", lambda *_a, **_k: Result())

        assert cli.cmd_ui(argparse.Namespace()) == 1
        assert "roost install" in capsys.readouterr().err

    def test_a_running_windows_tray_is_not_started_twice(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.windows_support, "is_windows", lambda: True)
        monkeypatch.setattr(cli.windows_support, "tray_is_running", lambda: True)
        monkeypatch.setattr(
            cli.windows_support, "start_tray",
            lambda *_a, **_k: pytest.fail("the tray must not be started twice"),
        )

        assert cli.cmd_ui(argparse.Namespace()) == 0
        assert "already running" in capsys.readouterr().out

    def test_a_windows_start_failure_is_reported(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.windows_support, "is_windows", lambda: True)
        monkeypatch.setattr(cli.windows_support, "tray_is_running", lambda: False)
        monkeypatch.setattr(cli.windows_support, "start_tray",
                            lambda *_a, **_k: False)

        assert cli.cmd_ui(argparse.Namespace()) == 1
        assert paths.LOG_NAME in capsys.readouterr().err


# ── uninstall ─────────────────────────────────────────────────────────────────

class TestCmdUninstall:
    def test_uninstall_stops_no_bird(self, monkeypatch, capsys):
        """The birds outlive Roost; uninstalling the tray must not touch them."""
        monkeypatch.setattr(cli.windows_support, "is_windows", lambda: False)

        class Result:
            returncode = 0

        monkeypatch.setattr(cli.subprocess, "run", lambda *_a, **_k: Result())

        assert cli.cmd_uninstall(argparse.Namespace()) == 0
        assert "were not touched" in capsys.readouterr().out

    def test_uninstall_leaves_a_foreign_symlink_alone(
        self, monkeypatch, tmp_path, capsys
    ):
        _require_symlinks(tmp_path)
        monkeypatch.setattr(cli.windows_support, "is_windows", lambda: False)
        monkeypatch.setattr(cli.Path, "home", classmethod(lambda _cls: tmp_path))

        class Result:
            returncode = 0

        monkeypatch.setattr(cli.subprocess, "run", lambda *_a, **_k: Result())
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        other = tmp_path / "somewhere-else"
        other.write_text("#!/bin/sh\n", encoding="utf-8")
        (bin_dir / "roost").symlink_to(other)

        cli.cmd_uninstall(argparse.Namespace())

        assert (bin_dir / "roost").is_symlink()
        assert "points elsewhere" in capsys.readouterr().out
