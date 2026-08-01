"""Hermetic tests for the Windows path, shortcut, and tray-process helpers.

Only two Appistry shortcuts exist now (login startup and Start Menu) because
there are no apps to launch — the ravens start themselves. What is left worth
pinning is that the PATH and environment plumbing normalises correctly, that the
tray's liveness check probes rather than trusts a port file, and that stopping the
tray never signals an unverified PID.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import help_server
import paths
import windows_support


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "APPISTRY_DIR", tmp_path / ".appistry")
    return tmp_path


# ── Shortcuts ─────────────────────────────────────────────────────────────────

class TestShortcuts:
    def test_the_startup_shortcut_launches_the_tray_windowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        monkeypatch.setattr(windows_support, "prepare_tray_icon", lambda: None)
        captured = []

        def create(path, **kwargs):
            captured.append({"path": path, **kwargs})
            return path

        monkeypatch.setattr(windows_support, "_create_shortcut", create)
        appistry_dir = tmp_path / "Appistry Home"

        startup, menu = windows_support.install_appistry_shortcuts(appistry_dir)

        assert startup == captured[0]["path"]
        assert menu == captured[1]["path"]
        for entry in captured:
            # pythonw.exe, not python.exe: a console window flashing at sign-in
            # is the difference between a tray app and a startup annoyance.
            assert entry["target"] == (
                appistry_dir / ".venv" / "Scripts" / "pythonw.exe"
            )
            assert str(appistry_dir / "windows_tray.py") in entry["arguments"]
            assert entry["working_directory"] == appistry_dir

    def test_the_startup_shortcut_lands_in_the_startup_folder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        assert windows_support.startup_dir().name == "Startup"
        assert windows_support.startup_dir().parent == (
            windows_support.start_menu_programs_dir()
        )

    def test_there_are_no_per_app_shortcuts(self):
        """A status tray launches nothing, so it installs no app launchers."""
        source = Path(windows_support.__file__).read_text(encoding="utf-8")
        for forbidden in ("registered_shortcut_path", "build_registered_shortcut",
                          "remove_registered_shortcut"):
            assert forbidden not in source, forbidden

    def test_uninstall_removes_both_shortcuts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        for path in (
            windows_support.startup_dir() / "Appistry.lnk",
            windows_support.appistry_shortcuts_dir() / "Appistry.lnk",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("shortcut", encoding="utf-8")

        windows_support.uninstall_shortcuts()

        assert not (windows_support.startup_dir() / "Appistry.lnk").exists()
        assert not (
            windows_support.appistry_shortcuts_dir() / "Appistry.lnk"
        ).exists()

    def test_an_ico_tray_icon_is_used_directly(self, monkeypatch, tmp_path):
        icon = tmp_path / "raven.ico"
        icon.write_bytes(b"ico")
        monkeypatch.setattr(
            windows_support, "prepare_tray_icon", windows_support.prepare_tray_icon
        )
        import icons

        monkeypatch.setattr(icons, "resolve", lambda: icons.IconChoice(
            "raven", icon, template=False, builtin=True
        ))
        assert windows_support.prepare_tray_icon() == icon

    def test_an_unconvertible_icon_does_not_block_installation(self, monkeypatch, tmp_path):
        """A default-looking shortcut icon beats a tray that never starts."""
        source = tmp_path / "raven.png"
        source.write_bytes(b"not really a png")
        import icons

        monkeypatch.setattr(icons, "resolve", lambda: icons.IconChoice(
            "raven", source, template=False, builtin=True
        ))
        assert windows_support.prepare_tray_icon() is None

    def test_no_resolvable_icon_yields_none(self, monkeypatch):
        import icons

        monkeypatch.setattr(icons, "resolve", lambda: None)
        assert windows_support.prepare_tray_icon() is None


# ── PATH and environment ──────────────────────────────────────────────────────

class TestEnvironment:
    def test_path_contains_matches_a_normalised_entry(self, tmp_path):
        bin_dir = tmp_path / "Appistry" / "Scripts"
        value = str(tmp_path / "other") + windows_support.os.pathsep + str(bin_dir)
        assert windows_support._path_contains(value, bin_dir) is True

    def test_path_contains_ignores_an_unrelated_entry(self, tmp_path):
        bin_dir = tmp_path / "Appistry" / "Scripts"
        assert windows_support._path_contains(str(tmp_path / "other"), bin_dir) is False

    def test_path_contains_sees_through_quotes_and_whitespace(self, tmp_path):
        """A PATH entry written by hand often arrives quoted and padded."""
        bin_dir = tmp_path / "Appistry" / "Scripts"
        assert windows_support._path_contains(f'  "{bin_dir}"  ', bin_dir) is True

    def test_path_contains_normalises_a_trailing_separator(self, tmp_path):
        bin_dir = tmp_path / "Appistry" / "Scripts"
        assert windows_support._path_contains(f"{bin_dir}{os.sep}", bin_dir) is True

    def test_refresh_tracks_an_updated_user_value(self, monkeypatch):
        monkeypatch.setattr(windows_support, "is_windows", lambda: True)
        monkeypatch.setattr(windows_support, "_read_registry_environment",
                            lambda: {"APPISTRY_TEST_SETTING": "first"})
        monkeypatch.setattr(windows_support, "_read_registry_path", lambda: "")
        monkeypatch.setenv("APPISTRY_TEST_SETTING", "first")
        windows_support._managed_environment.clear()

        windows_support.refresh_user_environment()
        monkeypatch.setattr(windows_support, "_read_registry_environment",
                            lambda: {"APPISTRY_TEST_SETTING": "updated"})
        windows_support.refresh_user_environment()

        assert windows_support.os.environ["APPISTRY_TEST_SETTING"] == "updated"

    def test_refresh_picks_up_a_relocated_descriptor_directory(self, monkeypatch):
        """RAVENS_STATE_DIR set after startup must reach the running tray, or it
        watches a different directory than the raven publishes to."""
        monkeypatch.setattr(windows_support, "is_windows", lambda: True)
        monkeypatch.setattr(windows_support, "_read_registry_path", lambda: "")
        monkeypatch.delenv("RAVENS_STATE_DIR", raising=False)
        windows_support._managed_environment.clear()
        monkeypatch.setattr(windows_support, "_read_registry_environment",
                            lambda: {"RAVENS_STATE_DIR": r"D:\ravens"})

        windows_support.refresh_user_environment()

        assert windows_support.os.environ["RAVENS_STATE_DIR"] == r"D:\ravens"

    def test_refresh_replaces_path_with_the_registry_path(self, monkeypatch):
        monkeypatch.setattr(windows_support, "is_windows", lambda: True)
        monkeypatch.setattr(windows_support, "_read_registry_environment", lambda: {})
        monkeypatch.setattr(windows_support, "_read_registry_path",
                            lambda: r"C:\Windows\System32;C:\Users\alice\bin")
        monkeypatch.setenv("PATH", "stale")
        windows_support._managed_environment.clear()

        windows_support.refresh_user_environment()

        assert windows_support.os.environ["PATH"] == (
            r"C:\Windows\System32;C:\Users\alice\bin"
        )

    def test_refresh_is_a_no_op_off_windows(self, monkeypatch):
        monkeypatch.setattr(windows_support, "is_windows", lambda: False)
        monkeypatch.setattr(
            windows_support, "_read_registry_environment",
            lambda: pytest.fail("the registry must not be read off Windows"),
        )
        windows_support.refresh_user_environment()


# ── Tray liveness ─────────────────────────────────────────────────────────────

class TestTrayIsRunning:
    def test_an_out_of_range_port_file_is_not_probed(self, monkeypatch):
        help_server.port_file_path().parent.mkdir(parents=True, exist_ok=True)
        help_server.port_file_path().write_text("70000", encoding="utf-8")
        assert windows_support.tray_is_running() is False

    def test_a_missing_port_file_means_not_running(self):
        assert windows_support.tray_is_running() is False

    def test_an_unrelated_service_on_the_port_is_not_the_tray(self, monkeypatch):
        """A stale port can be inherited by anything; the reply must identify us."""
        help_server.port_file_path().parent.mkdir(parents=True, exist_ok=True)
        help_server.port_file_path().write_text("54321", encoding="utf-8")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"service":"other","ok":true}'

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: Response())
        assert windows_support.tray_is_running() is False

    def test_the_appistry_status_payload_means_running(self, monkeypatch):
        help_server.port_file_path().parent.mkdir(parents=True, exist_ok=True)
        help_server.port_file_path().write_text("54321", encoding="utf-8")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"service":"appistry","ok":true}'

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: Response())
        assert windows_support.tray_is_running() is True


# ── Tray process ──────────────────────────────────────────────────────────────

class TestStartTray:
    def test_a_running_tray_is_not_started_twice(self, tmp_path, monkeypatch):
        monkeypatch.setattr(windows_support, "is_windows", lambda: True)
        monkeypatch.setattr(windows_support, "tray_is_running", lambda: True)
        popen = MagicMock()
        monkeypatch.setattr(windows_support.subprocess, "Popen", popen)

        assert windows_support.start_tray(tmp_path) is True
        popen.assert_not_called()

    def test_an_early_exit_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(windows_support, "is_windows", lambda: True)
        monkeypatch.setattr(windows_support, "tray_is_running", lambda: False)
        proc = MagicMock()
        proc.poll.return_value = 1
        monkeypatch.setattr(windows_support.subprocess, "Popen",
                            MagicMock(return_value=proc))

        assert windows_support.start_tray(tmp_path) is False

    def test_a_child_that_never_answers_is_terminated(self, tmp_path, monkeypatch):
        """Otherwise a broken tray lingers, invisible, holding the host lock."""
        monkeypatch.setattr(windows_support, "is_windows", lambda: True)
        monkeypatch.setattr(windows_support, "tray_is_running", lambda: False)
        monkeypatch.setattr(windows_support.time, "monotonic",
                            MagicMock(side_effect=[0, 9]))
        proc = MagicMock()
        proc.poll.return_value = None
        monkeypatch.setattr(windows_support.subprocess, "Popen",
                            MagicMock(return_value=proc))

        assert windows_support.start_tray(tmp_path) is False
        proc.terminate.assert_called_once_with()
        proc.wait.assert_called_once_with(timeout=3)


class TestStopTray:
    def _psutil(self, monkeypatch, *, cmdline, proc=None):
        process = proc or MagicMock()
        process.cmdline.return_value = cmdline

        class TimeoutExpired(Exception):
            pass

        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        fake = SimpleNamespace(
            Process=lambda _pid: process,
            TimeoutExpired=TimeoutExpired,
            NoSuchProcess=NoSuchProcess,
            AccessDenied=AccessDenied,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake)
        return process

    def test_a_missing_pid_file_stops_nothing(self):
        assert windows_support.stop_tray() is False

    @pytest.mark.parametrize("content", ["0", "-1", "-99999"])
    def test_a_non_positive_pid_is_refused(self, monkeypatch, content):
        """os.kill(-1) signals every process this user can signal."""
        windows_support.tray_pid_path().parent.mkdir(parents=True, exist_ok=True)
        windows_support.tray_pid_path().write_text(content, encoding="utf-8")
        self._psutil(
            monkeypatch,
            cmdline=lambda: pytest.fail("a non-positive PID reached psutil"),
        )

        assert windows_support.stop_tray() is False
        assert windows_support.tray_pid_path().exists() is False

    @pytest.mark.parametrize("content", ["", "not-a-pid"])
    def test_an_unparseable_pid_file_stops_nothing(self, content):
        windows_support.tray_pid_path().parent.mkdir(parents=True, exist_ok=True)
        windows_support.tray_pid_path().write_text(content, encoding="utf-8")
        assert windows_support.stop_tray() is False

    def test_a_pid_that_is_not_the_tray_is_not_signalled(self, monkeypatch):
        """Any same-user process can write this file, so verify before signalling."""
        windows_support.tray_pid_path().parent.mkdir(parents=True, exist_ok=True)
        windows_support.tray_pid_path().write_text("4321", encoding="utf-8")
        process = self._psutil(
            monkeypatch, cmdline=[r"C:\Python\python.exe", "something_else.py"]
        )

        assert windows_support.stop_tray() is False
        process.terminate.assert_not_called()
        assert windows_support.tray_pid_path().exists() is False

    def test_the_verified_tray_is_terminated(self, monkeypatch):
        windows_support.tray_pid_path().parent.mkdir(parents=True, exist_ok=True)
        windows_support.tray_pid_path().write_text("4321", encoding="utf-8")
        process = self._psutil(
            monkeypatch,
            cmdline=[r"C:\Appistry\.venv\Scripts\pythonw.exe",
                     r"C:\Appistry\windows_tray.py"],
        )

        assert windows_support.stop_tray() is True
        process.terminate.assert_called_once_with()
        assert windows_support.tray_pid_path().exists() is False

    def test_without_psutil_nothing_is_signalled(self, monkeypatch):
        """No way to verify the PID means no signal, not a hopeful one."""
        windows_support.tray_pid_path().parent.mkdir(parents=True, exist_ok=True)
        windows_support.tray_pid_path().write_text("4321", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "psutil", None)

        assert windows_support.stop_tray() is False


class TestTrayPid:
    def test_the_pid_file_records_this_process(self):
        windows_support.write_tray_pid()
        assert windows_support.tray_pid_path().read_text(encoding="utf-8") == str(
            windows_support.os.getpid()
        )

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="POSIX mode bits are not meaningful on Windows")
    def test_the_pid_file_is_owner_only(self):
        import stat

        windows_support.write_tray_pid()
        mode = stat.S_IMODE(windows_support.tray_pid_path().stat().st_mode)
        assert mode == 0o600, oct(mode)
