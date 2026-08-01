"""Tests for appistry.py — pure/mockable logic."""

import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import appistry
from appistry import _get_github_url


def _make_run(stdout="", returncode=0):
    """Return a mock subprocess.CompletedProcess."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


class TestGetGithubUrl:
    def test_https_url_returned_as_is(self):
        with patch("subprocess.run", return_value=_make_run("https://github.com/org/repo\n")):
            assert _get_github_url("/proj") == "https://github.com/org/repo"

    def test_https_url_strips_dot_git(self):
        with patch("subprocess.run", return_value=_make_run("https://github.com/org/repo.git\n")):
            assert _get_github_url("/proj") == "https://github.com/org/repo"

    def test_ssh_url_normalised_to_https(self):
        with patch("subprocess.run", return_value=_make_run("git@github.com:org/repo.git\n")):
            result = _get_github_url("/proj")
            assert result == "https://github.com/org/repo"

    def test_non_github_remote_returns_none(self):
        with patch("subprocess.run", return_value=_make_run("https://gitlab.com/org/repo\n")):
            assert _get_github_url("/proj") is None

    def test_git_failure_returns_none(self):
        with patch("subprocess.run", return_value=_make_run("", returncode=1)):
            assert _get_github_url("/proj") is None

    def test_git_exception_returns_none(self):
        with patch("subprocess.run", side_effect=Exception("git not found")):
            assert _get_github_url("/proj") is None

    def test_empty_output_returns_none(self):
        with patch("subprocess.run", return_value=_make_run("")):
            assert _get_github_url("/proj") is None

    def test_trailing_newline_handled(self):
        with patch("subprocess.run", return_value=_make_run("https://github.com/a/b\n\n")):
            assert _get_github_url("/proj") == "https://github.com/a/b"


class TestMenubarLaunchUrl:
    def test_returns_launch_url_when_menubar_server_responds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(appistry.registry, "APPISTRY_DIR", tmp_path)
        (tmp_path / "menubar-http-port").write_text("54321")
        with patch("urllib.request.urlopen") as urlopen:
            assert appistry._menubar_launch_url("my app") == "http://127.0.0.1:54321/launch/my%20app"
        urlopen.assert_called_once()
        assert "/api/launch/my%20app/ready" in urlopen.call_args.args[0]

    def test_returns_none_when_port_file_is_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(appistry.registry, "APPISTRY_DIR", tmp_path)
        (tmp_path / "menubar-http-port").write_text("54321")
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            assert appistry._menubar_launch_url("my-app") is None


class TestCmdLaunch:
    def _entry(self):
        return appistry.AppEntry(
            id="widget",
            name="Widget",
            cwd="/tmp/widget",
            command="scripts/start.sh",
            port=8009,
            github_url="https://github.com/example/widget",
        )

    def test_stopped_app_opens_readiness_page_then_starts(self, monkeypatch, capsys):
        monkeypatch.setattr(appistry.registry, "get", lambda _app_id: self._entry())
        monkeypatch.setattr(appistry.process, "is_running", lambda _app_id: False)
        monkeypatch.setattr(appistry.process, "start", lambda _entry: True)
        monkeypatch.setattr(
            appistry,
            "_menubar_launch_url",
            lambda _app_id: "http://127.0.0.1:54321/launch/widget",
        )
        opened = []
        monkeypatch.setattr(appistry.webbrowser, "open", opened.append)

        result = appistry.cmd_launch(type("Args", (), {"id": "widget"})())

        assert result == 0
        assert opened == ["http://127.0.0.1:54321/launch/widget"]
        assert "Started and opened" in capsys.readouterr().out

    def test_stopped_app_without_tray_starts_before_opening_raw_url(self, monkeypatch):
        monkeypatch.setattr(appistry.registry, "get", lambda _app_id: self._entry())
        monkeypatch.setattr(appistry.process, "is_running", lambda _app_id: False)
        events = []
        monkeypatch.setattr(appistry.process, "start", lambda _entry: events.append("start") or True)
        monkeypatch.setattr(appistry, "_menubar_launch_url", lambda _app_id: None)
        monkeypatch.setattr(appistry.webbrowser, "open", lambda url: events.append(url))

        result = appistry.cmd_launch(type("Args", (), {"id": "widget"})())

        assert result == 0
        assert events == ["start", "http://localhost:8009"]


class TestCmdWindow:
    def _entry(self):
        return appistry.AppEntry(
            id="notekeeper",
            name="Notekeeper",
            cwd="/tmp/notekeeper",
            command="scripts/start.sh",
            port=8000,
            github_url="https://github.com/example/notekeeper",
        )

    def test_running_app_opens_window_without_starting(self, monkeypatch, capsys):
        monkeypatch.setattr(appistry.registry, "get", lambda _app_id: self._entry())
        monkeypatch.setattr(appistry.process, "is_running", lambda _app_id: True)
        started = []
        monkeypatch.setattr(appistry.process, "start", lambda _e: started.append("start") or True)
        calls = {}
        monkeypatch.setattr(
            appistry.launch, "open_dedicated_window",
            lambda entry, secret_mode="fragment", block=False: calls.update(
                entry=entry, secret_mode=secret_mode, block=block) or 0,
        )

        args = type("Args", (), {"id": "notekeeper", "secret_mode": "fragment"})()
        result = appistry.cmd_window(args)

        assert result == 0
        assert started == []  # already running — must not restart
        assert calls["entry"].id == "notekeeper"
        assert calls["secret_mode"] == "fragment"
        assert calls["block"] is True

    def test_stopped_app_is_started_then_windowed(self, monkeypatch):
        monkeypatch.setattr(appistry.registry, "get", lambda _app_id: self._entry())
        monkeypatch.setattr(appistry.process, "is_running", lambda _app_id: False)
        events = []
        monkeypatch.setattr(appistry.process, "start", lambda _e: events.append("start") or True)
        monkeypatch.setattr(
            appistry.launch, "open_dedicated_window",
            lambda entry, secret_mode="fragment", block=False: events.append("window") or 0,
        )

        args = type("Args", (), {"id": "notekeeper", "secret_mode": "query"})()
        result = appistry.cmd_window(args)

        assert result == 0
        assert events == ["start", "window"]

    def test_failed_start_reports_error(self, monkeypatch, capsys):
        monkeypatch.setattr(appistry.registry, "get", lambda _app_id: self._entry())
        monkeypatch.setattr(appistry.process, "is_running", lambda _app_id: False)
        monkeypatch.setattr(appistry.process, "start", lambda _e: False)
        monkeypatch.setattr(
            appistry.launch, "open_dedicated_window",
            MagicMock(side_effect=AssertionError("should not open window on failed start")),
        )

        args = type("Args", (), {"id": "notekeeper", "secret_mode": "fragment"})()
        result = appistry.cmd_window(args)

        assert result == 1
        assert "failed to start" in capsys.readouterr().err


class TestWindowsInstall:
    def test_fresh_install_builds_venv_cli_shortcuts_and_tray(self, tmp_path, monkeypatch):
        monkeypatch.setattr(appistry, "__file__", str(tmp_path / "appistry.py"))
        monkeypatch.setattr(appistry.registry, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(appistry.registry, "load", lambda: [])
        run = MagicMock(return_value=_make_run(""))
        monkeypatch.setattr(appistry.subprocess, "run", run)
        monkeypatch.setattr(
            appistry.windows_support,
            "install_appistry_shortcuts",
            lambda _directory: (tmp_path / "startup.lnk", tmp_path / "menu.lnk"),
        )
        monkeypatch.setattr(
            appistry.windows_support,
            "add_cli_dir_to_user_path",
            lambda _directory: True,
        )
        started = []
        monkeypatch.setattr(
            appistry.windows_support,
            "start_tray",
            lambda directory: started.append(directory) or True,
        )

        result = appistry._cmd_install_windows(type("Args", (), {"force": False})())

        assert result == 0
        commands = [call.args[0] for call in run.call_args_list]
        assert commands[0][1:3] == ["-m", "venv"]
        assert commands[1][1:4] == ["-m", "pip", "install"]
        assert "requirements.txt" in commands[1][-1]
        assert commands[2][-2:] == ["--editable", str(tmp_path)]
        assert started == [tmp_path]

    def test_install_reports_tray_start_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(appistry, "__file__", str(tmp_path / "appistry.py"))
        monkeypatch.setattr(appistry.registry, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(appistry.registry, "load", lambda: [])
        monkeypatch.setattr(appistry.subprocess, "run", MagicMock(return_value=_make_run("")))
        monkeypatch.setattr(
            appistry.windows_support,
            "install_appistry_shortcuts",
            lambda _directory: (tmp_path / "startup.lnk", tmp_path / "menu.lnk"),
        )
        monkeypatch.setattr(
            appistry.windows_support,
            "add_cli_dir_to_user_path",
            lambda _directory: False,
        )
        monkeypatch.setattr(appistry.windows_support, "start_tray", lambda _directory: False)

        result = appistry._cmd_install_windows(type("Args", (), {"force": False})())

        assert result == 1


class TestHookUrl:
    def test_cmd_hook_url_prints_registered_app_url(self, monkeypatch, capsys):
        monkeypatch.setattr(appistry.registry, "get", lambda app_id: appistry.AppEntry(
            id=app_id,
            name="Demo App",
            cwd="/tmp/demo-app",
            command=".venv/bin/python ui/server.py",
            port=8766,
            github_url="https://github.com/example/demo-app",
        ))

        result = appistry.cmd_hook_url(type("Args", (), {
            "id": "demo-app",
            "path": "/api/oauth/callback",
        })())

        assert result == 0
        assert capsys.readouterr().out.strip() == (
            "http://127.0.0.1:47658/hooks/demo-app/api/oauth/callback"
        )

    def test_cmd_hook_url_rejects_unknown_app(self, monkeypatch, capsys):
        monkeypatch.setattr(appistry.registry, "get", lambda app_id: None)

        result = appistry.cmd_hook_url(type("Args", (), {
            "id": "missing",
            "path": "/callback",
        })())

        assert result == 1
        assert "not found" in capsys.readouterr().err


class TestCmdRegister:
    """A malicious --id must be rejected before it reaches the filesystem."""

    def _args(self, **overrides):
        defaults = dict(
            name="Evil App", cwd="/tmp/evil", command="node server.js",
            port=9999, icon=None, id=None,
        )
        defaults.update(overrides)
        return type("Args", (), defaults)()

    def _patch_common(self, monkeypatch):
        monkeypatch.setattr(appistry.registry, "get", lambda app_id: None)
        monkeypatch.setattr(appistry, "_get_github_url",
                             lambda cwd: "https://github.com/example/evil")
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    def test_path_traversal_id_rejected(self, monkeypatch, capsys):
        self._patch_common(monkeypatch)
        result = appistry.cmd_register(self._args(id="../../etc/evil"))
        assert result == 1
        assert "must be a lowercase slug" in capsys.readouterr().err

    def test_shell_metacharacter_id_rejected(self, monkeypatch, capsys):
        self._patch_common(monkeypatch)
        result = appistry.cmd_register(self._args(id='x";touch /tmp/pwn;"'))
        assert result == 1
        assert "must be a lowercase slug" in capsys.readouterr().err

    def test_valid_explicit_id_accepted(self, monkeypatch, capsys):
        self._patch_common(monkeypatch)
        monkeypatch.setattr(appistry.registry, "upsert", lambda entry: None)
        monkeypatch.setattr(appistry, "_build_registered_app", lambda entry: None)
        result = appistry.cmd_register(self._args(id="evil-app"))
        assert result == 0
        assert "Registered: evil-app" in capsys.readouterr().out


class TestCliIdentifierValidation:
    def test_start_rejects_unsafe_id_before_registry_lookup(self, monkeypatch, capsys):
        def fail_if_called(_app_id):
            raise AssertionError("invalid id reached registry lookup")

        monkeypatch.setattr(appistry.registry, "get", fail_if_called)

        result = appistry.cmd_start(type("Args", (), {"id": "../../other-app"})())

        assert result == 1
        assert "must be a lowercase slug" in capsys.readouterr().err


class TestAppistryLauncherScript:
    """The install directory (APPISTRY_HOME) is env/user-controlled and gets
    embedded in this launcher script — metacharacters must not inject commands."""

    def test_normal_path_produces_expected_pgrep(self):
        target = Path("/opt/appistry/menubar.py")
        script = appistry._appistry_launcher_script(target)
        assert self._pgrep_arg(script.splitlines()[1]) == str(target)

    def _pgrep_arg(self, quoted_line):
        # Tokenize the "if pgrep -qf <arg>; then" line and pull out <arg>.
        # shlex has no whitespace before ';', so it rides along on the last
        # token — strip it off.
        tokens = shlex.split(quoted_line)
        arg = tokens[tokens.index("-qf") + 1]
        return arg[:-1] if arg.endswith(";") else arg

    def test_spaces_are_quoted(self):
        malicious = Path("/opt/my appistry/menubar.py")
        script = appistry._appistry_launcher_script(malicious)
        assert self._pgrep_arg(script.splitlines()[1]) == str(malicious)

    def test_command_substitution_does_not_execute(self):
        malicious = Path("/tmp/$(touch /tmp/pwned)/menubar.py")
        script = appistry._appistry_launcher_script(malicious)
        # The payload must appear as a single quoted argv element to pgrep,
        # not as unquoted shell syntax that bash would expand.
        quoted_line = script.splitlines()[1]
        assert quoted_line.count("$(") == 0 or "'" in quoted_line
        assert self._pgrep_arg(quoted_line) == str(malicious)

    def test_single_quote_is_safely_escaped(self):
        malicious = Path("/tmp/it's-evil/menubar.py")
        script = appistry._appistry_launcher_script(malicious)
        quoted_line = script.splitlines()[1]
        assert self._pgrep_arg(quoted_line) == str(malicious)


class TestAppBundlePath:
    """entry.name is display-only and must never escape /Applications."""

    def test_normal_name_produces_expected_bundle(self):
        entry = appistry.AppEntry(
            id="widget", name="Widget", cwd="/tmp/widget",
            command="node server.js", port=8009,
            github_url="https://github.com/example/widget",
        )
        bundle = appistry._app_bundle_path(entry)
        assert bundle == (appistry._APPLICATIONS_DIR / "Widget.app").resolve()

    def test_path_traversal_name_stays_under_applications(self):
        entry = appistry.AppEntry(
            id="widget", name="../../etc/evil", cwd="/tmp/widget",
            command="node server.js", port=8009,
            github_url="https://github.com/example/widget",
        )
        bundle = appistry._app_bundle_path(entry)
        assert bundle.parent == appistry._APPLICATIONS_DIR.resolve()
        assert ".." not in str(bundle)
