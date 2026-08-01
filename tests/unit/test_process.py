"""Tests for process._validate_command — pure validation logic."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import process
from process import _validate_command, _pid_path, _log_path
from registry import AppEntry


@pytest.fixture(autouse=True)
def default_to_posix_process_mode(monkeypatch):
    """Keep legacy process tests platform-independent; Windows cases opt in."""
    monkeypatch.setattr(process, "_IS_WINDOWS", False)


class TestValidateCommand:
    CWD = "/projects/myapp"

    # ── Valid commands ────────────────────────────────────────────────────────

    def test_simple_relative(self):
        argv = _validate_command(".venv/bin/python server.py", self.CWD)
        assert argv == [".venv/bin/python", "server.py"]

    def test_absolute_path(self):
        argv = _validate_command("/usr/bin/node app.js", self.CWD)
        assert argv[0] == "/usr/bin/node"

    def test_args_preserved(self):
        argv = _validate_command(".venv/bin/uvicorn app:app --port 8000", self.CWD)
        assert argv == [".venv/bin/uvicorn", "app:app", "--port", "8000"]

    def test_interpreter_with_script_is_allowed(self):
        # python with a script argument is fine; only bare python is blocked
        argv = _validate_command(".venv/bin/python3 manage.py runserver", self.CWD)
        assert argv[0] == ".venv/bin/python3"

    def test_bash_with_script_file_is_allowed(self):
        # bash with a script path is fine; only bare bash is blocked
        argv = _validate_command("/bin/bash start.sh", self.CWD)
        assert argv[0] == "/bin/bash"

    # ── Empty command ─────────────────────────────────────────────────────────

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Empty command"):
            _validate_command("", self.CWD)

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="Empty command"):
            _validate_command("   ", self.CWD)

    # ── Bare interpreter block ────────────────────────────────────────────────

    def test_bare_python_blocked(self):
        with pytest.raises(ValueError, match="Bare interpreter"):
            _validate_command("python", self.CWD)

    def test_bare_python3_blocked(self):
        with pytest.raises(ValueError, match="Bare interpreter"):
            _validate_command("python3", self.CWD)

    def test_bare_bash_blocked(self):
        with pytest.raises(ValueError, match="Bare interpreter"):
            _validate_command("bash", self.CWD)

    def test_bare_zsh_blocked(self):
        with pytest.raises(ValueError, match="Bare interpreter"):
            _validate_command("zsh", self.CWD)

    def test_absolute_bare_python_blocked(self):
        with pytest.raises(ValueError, match="Bare interpreter"):
            _validate_command("/usr/bin/python3", self.CWD)

    def test_absolute_bare_bash_blocked(self):
        with pytest.raises(ValueError, match="Bare interpreter"):
            _validate_command("/bin/bash", self.CWD)

    # ── Shell injection flags ─────────────────────────────────────────────────

    def test_dash_c_blocked(self):
        with pytest.raises(ValueError, match="code-injection flag"):
            _validate_command("/bin/bash -c 'rm -rf /'", self.CWD)

    def test_double_dash_command_blocked(self):
        with pytest.raises(ValueError, match="code-injection flag"):
            _validate_command("/bin/sh --command 'payload'", self.CWD)

    def test_dash_c_with_python_blocked(self):
        with pytest.raises(ValueError, match="code-injection flag"):
            _validate_command("/usr/bin/python3 -c 'import os'", self.CWD)

    # ── CWD escape ────────────────────────────────────────────────────────────

    def test_cwd_escape_blocked(self):
        with pytest.raises(ValueError, match="Executable escapes cwd"):
            _validate_command("../../etc/evil", self.CWD)

    def test_same_dir_relative_allowed(self):
        argv = _validate_command("./start.sh", self.CWD)
        assert argv == ["./start.sh"]

    def test_subdirectory_relative_allowed(self):
        argv = _validate_command("scripts/start.sh arg", self.CWD)
        assert argv == ["scripts/start.sh", "arg"]


class TestValidateWindowsCommand:
    CWD = r"C:\Projects\myapp"

    def test_quoted_venv_python_path_is_preserved(self, monkeypatch):
        monkeypatch.setattr(process, "_IS_WINDOWS", True)

        argv = _validate_command(
            r'".venv\Scripts\python.exe" "ui\server.py" --port 8000',
            self.CWD,
        )

        assert argv == [r".venv\Scripts\python.exe", r"ui\server.py", "--port", "8000"]

    def test_drive_relative_escape_is_rejected(self, monkeypatch):
        monkeypatch.setattr(process, "_IS_WINDOWS", True)

        with pytest.raises(ValueError, match="escapes cwd"):
            _validate_command(r"..\..\Windows\System32\evil.exe", self.CWD)

    def test_batch_launcher_is_rejected(self, monkeypatch):
        monkeypatch.setattr(process, "_IS_WINDOWS", True)

        with pytest.raises(ValueError, match="batch launchers"):
            _validate_command(r"scripts\start.cmd", self.CWD)

    def test_cmd_code_flag_is_rejected(self, monkeypatch):
        monkeypatch.setattr(process, "_IS_WINDOWS", True)

        with pytest.raises(ValueError, match="code-injection flag"):
            _validate_command(r"C:\Windows\System32\cmd.exe /C calc.exe", self.CWD)


class TestPathContainment:
    """A malicious --id must never escape ~/.appistry/pids or ~/.appistry."""

    def test_pid_path_traversal_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "PIDS_DIR", tmp_path / "pids")
        with pytest.raises(ValueError):
            _pid_path("../../etc/evil")

    def test_pid_path_shell_metacharacters_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "PIDS_DIR", tmp_path / "pids")
        with pytest.raises(ValueError):
            _pid_path('x";touch /tmp/pwn;"')

    def test_pid_path_valid_id_stays_under_pids_dir(self, tmp_path, monkeypatch):
        pids_dir = tmp_path / "pids"
        monkeypatch.setattr(process, "PIDS_DIR", pids_dir)
        path = _pid_path("widget")
        assert path.parent.resolve() == pids_dir.resolve()
        assert path.name == "widget.pid"

    def test_log_path_traversal_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path)
        with pytest.raises(ValueError):
            _log_path("../../etc/evil")

    def test_log_path_valid_id_stays_under_appistry_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path)
        path = _log_path("widget")
        assert path.parent.resolve() == tmp_path.resolve()
        assert path.name == "widget.log"

    def test_pid_record_reads_windows_creation_time(self, tmp_path, monkeypatch):
        pids_dir = tmp_path / "pids"
        pids_dir.mkdir()
        monkeypatch.setattr(process, "PIDS_DIR", pids_dir)
        (pids_dir / "widget.pid").write_text("4242:1234.500000", encoding="utf-8")

        assert process._read_pid_record("widget") == (4242, 1234.5)
        assert process._read_pid("widget") == 4242


class TestLaunchSecret:
    """The Appistry-owned launch secret must round-trip and stay private."""

    def test_secret_path_traversal_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "SECRETS_DIR", tmp_path / "secrets")
        with pytest.raises(ValueError):
            process._secret_path("../../etc/evil")

    def test_secret_path_valid_id_stays_under_secrets_dir(self, tmp_path, monkeypatch):
        secrets_dir = tmp_path / "secrets"
        monkeypatch.setattr(process, "SECRETS_DIR", secrets_dir)
        path = process._secret_path("widget")
        assert path.parent.resolve() == secrets_dir.resolve()
        assert path.name == "widget"

    def test_write_read_clear_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "SECRETS_DIR", tmp_path / "secrets")
        assert process.read_launch_secret("widget") is None
        process.write_launch_secret("widget", "s3cr3t-value")
        assert process.read_launch_secret("widget") == "s3cr3t-value"
        process.clear_launch_secret("widget")
        assert process.read_launch_secret("widget") is None

    def test_clear_is_idempotent_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "SECRETS_DIR", tmp_path / "secrets")
        # Must not raise even when nothing was ever written.
        process.clear_launch_secret("widget")

    def test_secret_file_and_dir_permissions_are_restricted(self, tmp_path, monkeypatch):
        """Real POSIX chmod bits, or a real owner-only Windows ACL — not a
        monkeypatched code path, since Windows os.chmod can't enforce
        restriction and this needs to prove the actual host OS restricts
        access."""
        monkeypatch.setattr(process, "_IS_WINDOWS", sys.platform == "win32")
        secrets_dir = tmp_path / "secrets"
        monkeypatch.setattr(process, "SECRETS_DIR", secrets_dir)
        process.write_launch_secret("widget", "s3cr3t")

        secret_path = secrets_dir / "widget"
        if sys.platform == "win32":
            import os

            import ntsecuritycon
            import win32security

            user, _, _ = win32security.LookupAccountName("", os.getlogin())
            for target in (secret_path, secrets_dir):
                sd = win32security.GetFileSecurity(
                    str(target), win32security.DACL_SECURITY_INFORMATION
                )
                dacl = sd.GetSecurityDescriptorDacl()
                assert dacl.GetAceCount() == 1
                ace = dacl.GetAce(0)
                assert ace[1] == ntsecuritycon.FILE_ALL_ACCESS
                assert ace[2] == user
        else:
            import stat

            file_mode = stat.S_IMODE(secret_path.stat().st_mode)
            dir_mode = stat.S_IMODE(secrets_dir.stat().st_mode)
            assert file_mode == 0o600
            assert dir_mode == 0o700

    def test_rewrite_truncates_previous_secret(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "SECRETS_DIR", tmp_path / "secrets")
        process.write_launch_secret("widget", "a-long-original-secret")
        process.write_launch_secret("widget", "short")
        assert process.read_launch_secret("widget") == "short"


class TestStartProcess:
    def test_windows_developer_paths_do_not_include_macos_locations(self, monkeypatch):
        monkeypatch.setattr(process, "_IS_WINDOWS", True)
        monkeypatch.delenv("NVM_SYMLINK", raising=False)
        monkeypatch.delenv("NVM_HOME", raising=False)

        paths = process._developer_path_prefixes()

        assert "/opt/homebrew/bin" not in paths
        assert "/usr/local/bin" not in paths

    def test_resolve_relative_executable_against_cwd(self, tmp_path):
        executable = tmp_path / "scripts" / "start.exe"
        executable.parent.mkdir()
        executable.write_text("placeholder", encoding="utf-8")

        result = process._resolve_executable(
            ["scripts/start.exe", "--serve"],
            str(tmp_path),
            {"PATH": ""},
        )

        assert result == [str(executable), "--serve"]

    def test_marks_process_as_appistry_launched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(process, "SECRETS_DIR", tmp_path / ".appistry" / "secrets")
        monkeypatch.setattr(process.time, "sleep", lambda _seconds: None)
        proc = MagicMock()
        proc.pid = 1234
        proc.poll.return_value = None
        popen = MagicMock(return_value=proc)
        monkeypatch.setattr(process.subprocess, "Popen", popen)
        entry = AppEntry(
            id="widget",
            name="Widget",
            cwd=str(tmp_path),
            command="scripts/start.sh",
            port=8009,
            github_url="https://github.com/example/widget",
        )

        assert process.start(entry) is True

        kwargs = popen.call_args.kwargs
        assert kwargs["env"]["APPISTRY_LAUNCHED"] == "1"
        assert kwargs["env"]["APPISTRY_APP_ID"] == "widget"

    def test_start_mints_and_persists_launch_secret(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(process, "SECRETS_DIR", tmp_path / ".appistry" / "secrets")
        monkeypatch.setattr(process, "PIDS_DIR", tmp_path / ".appistry" / "pids")
        monkeypatch.setattr(process.time, "sleep", lambda _seconds: None)
        proc = MagicMock()
        proc.pid = 1234
        proc.poll.return_value = None
        popen = MagicMock(return_value=proc)
        monkeypatch.setattr(process.subprocess, "Popen", popen)
        entry = AppEntry(
            id="widget",
            name="Widget",
            cwd=str(tmp_path),
            command="scripts/start.sh",
            port=8009,
            github_url="https://github.com/example/widget",
        )

        assert process.start(entry) is True

        injected = popen.call_args.kwargs["env"]["YGG_LAUNCH_SECRET"]
        assert injected
        # The same secret the child received must be readable by the separate
        # window invocation for this running instance.
        assert process.read_launch_secret("widget") == injected

    def test_failed_start_clears_persisted_secret(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(process, "SECRETS_DIR", tmp_path / ".appistry" / "secrets")
        monkeypatch.setattr(process, "PIDS_DIR", tmp_path / ".appistry" / "pids")
        monkeypatch.setattr(process.time, "sleep", lambda _seconds: None)
        proc = MagicMock()
        proc.pid = 1234
        proc.poll.return_value = 1  # exited immediately
        monkeypatch.setattr(process.subprocess, "Popen", MagicMock(return_value=proc))
        entry = AppEntry(
            id="widget",
            name="Widget",
            cwd=str(tmp_path),
            command="scripts/start.sh",
            port=8009,
            github_url="https://github.com/example/widget",
        )

        assert process.start(entry) is False
        # A dead launch must not leave a secret claiming a live window can attach.
        assert process.read_launch_secret("widget") is None

    def test_windows_start_uses_creation_flags_without_new_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(process, "PIDS_DIR", tmp_path / ".appistry" / "pids")
        monkeypatch.setattr(process, "SECRETS_DIR", tmp_path / ".appistry" / "secrets")
        monkeypatch.setattr(process, "_IS_WINDOWS", True)
        monkeypatch.setattr(process.time, "sleep", lambda _seconds: None)
        proc = MagicMock()
        proc.pid = 1234
        proc.poll.return_value = None
        popen = MagicMock(return_value=proc)
        monkeypatch.setattr(process.subprocess, "Popen", popen)
        entry = AppEntry(
            id="widget",
            name="Widget",
            cwd=str(tmp_path),
            command="scripts/start.exe",
            port=8009,
            github_url="https://github.com/example/widget",
        )

        assert process.start(entry) is True

        kwargs = popen.call_args.kwargs
        assert "creationflags" in kwargs
        assert "start_new_session" not in kwargs


class TestRunForeground:
    def _entry(self, tmp_path):
        (tmp_path / "scripts").mkdir(exist_ok=True)
        return AppEntry(
            id="widget",
            name="Widget",
            cwd=str(tmp_path),
            command="scripts/start.sh",
            port=8009,
            github_url="https://github.com/example/widget",
        )

    def test_records_pid_and_execs_in_place(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(process, "PIDS_DIR", tmp_path / ".appistry" / "pids")
        monkeypatch.setattr(process.os, "getpid", lambda: 4242)
        monkeypatch.setattr(process.os, "chdir", lambda _p: None)
        # Don't let the real log-redirect touch the test runner's fds.
        monkeypatch.setattr(process.os, "dup2", lambda _a, _b: None)
        execve = MagicMock(side_effect=SystemExit(0))  # stand in for image replacement
        monkeypatch.setattr(process.os, "execve", execve)

        entry = self._entry(tmp_path)
        with pytest.raises(SystemExit):
            process.run_foreground(entry)

        # PID recorded before exec, so stop()/is_running() keep working post-exec.
        assert process._read_pid("widget") == 4242
        argv, passed_argv, env = execve.call_args.args
        assert Path(argv).name == "start.sh"
        assert env["APPISTRY_LAUNCHED"] == "1"
        assert env["APPISTRY_APP_ID"] == "widget"

    def test_exec_failure_clears_pid(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(process, "PIDS_DIR", tmp_path / ".appistry" / "pids")
        monkeypatch.setattr(process.os, "getpid", lambda: 4242)
        monkeypatch.setattr(process.os, "chdir", lambda _p: None)
        monkeypatch.setattr(process.os, "dup2", lambda _a, _b: None)
        monkeypatch.setattr(process.os, "execve",
                            MagicMock(side_effect=OSError("no such file")))

        entry = self._entry(tmp_path)
        assert process.run_foreground(entry) == 1
        # A failed exec must not leave a stale PID claiming the app is running.
        assert process._read_pid("widget") is None

    def test_invalid_command_returns_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process, "APPISTRY_DIR", tmp_path / ".appistry")
        monkeypatch.setattr(process, "PIDS_DIR", tmp_path / ".appistry" / "pids")
        entry = AppEntry(
            id="bad", name="Bad", cwd=str(tmp_path),
            command="", port=8009,
            github_url="https://github.com/example/bad",
        )
        assert process.run_foreground(entry) == 1
