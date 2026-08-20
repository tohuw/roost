"""Asking the OS to start a stopped bird.

The menu used to show a stopped bird greyed out with "its recorded process is
gone" and nothing to click. The reason given for that was that a stopped daemon
withdraws its descriptor so the host cannot see it — true only of a *clean*
shutdown. A kill, a crash or a power cut leaves the descriptor exactly where it
was, which is the case that produced the useless menu.

What survives is narrower and is the invariant these tests defend: Roost must
never execute a command named in a descriptor.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import host, launcher, menu_spec, birds, tray
from roost.tray import RowKind


class TestParsing:
    def test_absent_is_not_an_error(self):
        """launch is optional; a bird predating it must keep working."""
        assert launcher.parse(None) is None

    @pytest.mark.parametrize("kind", launcher.KINDS)
    def test_each_known_supervisor_is_accepted(self, kind):
        assert launcher.parse({"kind": kind, "id": "svc.name-1"}).kind == kind

    @pytest.mark.parametrize("raw", [
        "not a dict",
        123,
        {"kind": "bash", "id": "x"},
        {"kind": "launchd"},
        {"kind": "launchd", "id": ""},
        {"kind": "launchd", "id": 7},
    ])
    def test_a_malformed_block_is_refused(self, raw):
        with pytest.raises(launcher.LaunchError):
            launcher.parse(raw)

    @pytest.mark.parametrize("identifier", [
        "rm -rf /",              # an argument
        "a;whoami",              # a separator
        "../../etc/passwd",      # a path
        "/usr/bin/python",       # a path
        r"C:\Windows\system32",  # a path
        "svc$(id)",              # a substitution
        "svc`id`",
        "svc\nid",
        "a" * 129,               # over length
    ])
    def test_an_identifier_that_is_not_an_identifier_is_refused(self, identifier):
        """The whole safety property: an id, never a command or a path."""
        with pytest.raises(launcher.LaunchError):
            launcher.parse({"kind": "launchd", "id": identifier})


class TestDispatch:
    def test_launchd_is_asked_by_label(self):
        spec = launcher.LaunchSpec("launchd", "is.tohuw.huginn")
        with patch.object(launcher, "_run", return_value=(True, "")) as run, \
             patch.object(launcher.os, "getuid", create=True, return_value=501):
            launcher.start(spec)
        argv = run.call_args.args[0]
        assert argv[0] == "launchctl"
        assert "gui/501/is.tohuw.huginn" in argv

    def test_systemd_is_asked_by_unit(self):
        spec = launcher.LaunchSpec("systemd", "muninn.service")
        with patch.object(launcher, "_run", return_value=(True, "")) as run:
            launcher.start(spec)
        assert run.call_args.args[0] == [
            "systemctl", "--user", "start", "muninn.service"]

    def test_the_command_is_never_taken_from_the_descriptor(self):
        """Windows reads the command from the Run key, not from the file.

        The descriptor names *which* autostart entry to trigger. Windows already
        runs that exact string at every sign-in, so triggering it grants nothing
        the user has not already installed.
        """
        source = Path(launcher.__file__).read_text(encoding="utf-8")
        assert "shell=True" not in source
        # The only command string that reaches Popen comes from winreg.
        assert "QueryValueEx" in source

    def test_a_supervisor_failure_is_a_reason_not_an_exception(self):
        spec = launcher.LaunchSpec("systemd", "muninn.service")
        failed = MagicMock(returncode=1, stderr="Unit muninn.service not found.", stdout="")
        with patch.object(launcher.subprocess, "run", return_value=failed):
            ok, reason = launcher.start(spec)
        assert ok is False
        assert "not found" in reason

    def test_a_missing_supervisor_is_a_reason_not_an_exception(self):
        spec = launcher.LaunchSpec("systemd", "muninn.service")
        with patch.object(launcher.subprocess, "run", side_effect=FileNotFoundError):
            ok, reason = launcher.start(spec)
        assert ok is False
        assert "systemctl" in reason


class TestPlatformFit:
    @pytest.mark.parametrize("kind,platform,nt,expected", [
        ("launchd", "darwin", False, True),
        ("launchd", "linux", False, False),
        ("systemd", "linux", False, True),
        ("systemd", "darwin", False, False),
        ("windows-run", "win32", True, True),
        ("windows-run", "linux", False, False),
    ])
    def test_only_this_machines_supervisor_counts(self, kind, platform, nt, expected, monkeypatch):
        """A descriptor copied between machines can name the wrong supervisor.

        Offering a row that cannot possibly work is worse than offering none.
        """
        monkeypatch.setattr(launcher.sys, "platform", platform)
        monkeypatch.setattr(launcher.os, "name", "nt" if nt else "posix")
        assert launcher.supported_here(launcher.LaunchSpec(kind, "x")) is expected


class TestTheRow:
    def _stopped(self, launch):
        return menu_spec.unavailable(birds.UnavailableBird(
            "muninn", "Muninn", "Not running (its recorded process is gone).",
            None, launch=launch,
        ))

    def test_a_stopped_bird_that_says_how_gets_a_start_row(self, monkeypatch):
        monkeypatch.setattr(launcher, "supported_here", lambda _spec: True)
        rows = tray.bird_rows(self._stopped(launcher.LaunchSpec("systemd", "muninn.service")))
        start = [r for r in rows if r.action == "start:muninn"]
        assert len(start) == 1
        assert start[0].enabled is True
        assert start[0].label == "Start Muninn"

    def test_the_reason_is_still_shown(self):
        """The row is additional to the reason, not a replacement for it."""
        rows = self._rows_with_start()
        assert any(r.kind is RowKind.REASON for r in rows)

    def _rows_with_start(self):
        with patch.object(launcher, "supported_here", lambda _spec: True):
            return tray.bird_rows(
                self._stopped(launcher.LaunchSpec("systemd", "muninn.service")))

    def test_a_bird_that_says_nothing_gets_no_row(self):
        """Absent launch is the old behaviour, unchanged."""
        rows = tray.bird_rows(self._stopped(None))
        assert not [r for r in rows if str(r.action).startswith("start:")]

    def test_no_row_when_this_machine_cannot_honour_it(self, monkeypatch):
        monkeypatch.setattr(launcher, "supported_here", lambda _spec: False)
        rows = tray.bird_rows(self._stopped(launcher.LaunchSpec("launchd", "is.tohuw.muninn")))
        assert not [r for r in rows if str(r.action).startswith("start:")]

    def test_becoming_startable_changes_the_signature(self):
        """Otherwise the row never reaches the screen until something else changes."""
        without = self._stopped(None).signature()
        with_launch = self._stopped(launcher.LaunchSpec("systemd", "m.service")).signature()
        assert without != with_launch


class TestModelLookup:
    def test_the_spec_comes_from_the_model_the_menu_was_drawn_from(self):
        """Re-reading disk between draw and click acts on what was never shown."""
        spec = launcher.LaunchSpec("systemd", "muninn.service")
        model = host.MenuModel((
            menu_spec.BirdMenu(name="muninn", display="Muninn",
                                reason="gone", launch=spec),
        ))
        assert model.launch_spec("muninn") is spec
        assert model.launch_spec("nobody") is None
