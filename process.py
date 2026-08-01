"""
process.py

Process management for Appistry. Handles PID files at ~/.appistry/pids/{id}.pid,
running-state checks via os.kill, starting subprocesses with subprocess.Popen,
and graceful shutdown with SIGTERM/SIGKILL.
"""

from __future__ import annotations

import os
import ntpath
import posixpath
import logging
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


_IS_WINDOWS = sys.platform == "win32"
log = logging.getLogger(__name__)

def _developer_path_prefixes() -> list[str]:
    """Return platform-native developer tool paths missing from GUI sessions."""
    if _IS_WINDOWS:
        candidates = [
            os.environ.get("NVM_SYMLINK", ""),
            os.environ.get("NVM_HOME", ""),
            str(Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "npm"),
            str(Path.home() / ".local" / "bin"),
            str(Path.home() / "bin"),
        ]
        return [path for path in candidates if path]
    return [
        str(Path.home() / ".volta" / "bin"),
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / "bin"),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
    ]


def _nvm_default_bin() -> str | None:
    """Return the bin dir for nvm's default Node version, or None."""
    if _IS_WINDOWS:
        return None
    alias = Path.home() / ".nvm" / "alias" / "default"
    try:
        version = alias.read_text().strip()
        if version.startswith("v"):
            candidate = Path.home() / ".nvm" / "versions" / "node" / version / "bin"
            if candidate.is_dir():
                return str(candidate)
    except OSError:
        pass
    return None


def _build_launch_env() -> dict:
    """Return os.environ augmented with developer tool paths.

    Ensures bare executables like 'node' are findable when Appistry runs
    under launchd, which starts with a minimal PATH.
    """
    env = os.environ.copy()
    current = set(env.get("PATH", "").split(os.pathsep))
    additions = [p for p in _developer_path_prefixes() if p not in current]
    nvm_bin = _nvm_default_bin()
    if nvm_bin and nvm_bin not in current:
        additions.insert(0, nvm_bin)
    if additions:
        env["PATH"] = os.pathsep.join(additions + [env.get("PATH", "")])
    return env

from registry import APPISTRY_DIR, AppEntry, validate_app_id


PIDS_DIR = APPISTRY_DIR / "pids"
SECRETS_DIR = APPISTRY_DIR / "secrets"

# Shell interpreters that must not be invoked as bare commands from the registry.
_BLOCKED_BARE_EXECUTABLES = {
    "sh", "bash", "zsh", "fish", "dash", "python", "python3",
    "python.exe", "python3.exe", "cmd", "cmd.exe", "powershell",
    "powershell.exe", "pwsh", "pwsh.exe",
}

# Flags that pass arbitrary code to a shell or interpreter regardless of the executable.
# Blocking these prevents /usr/bin/bash -c "payload" even when bash itself is allowed.
_BLOCKED_SHELL_FLAGS = {
    "-c", "--command", "/c", "/k", "-command", "-encodedcommand",
}

def _split_command(command: str) -> list[str]:
    """Split a registered command according to the active platform's quoting.

    Windows' command-line rules preserve backslashes and use double quotes for
    executable paths. ``shlex`` in non-POSIX mode is a conservative parser for
    the command shapes Appistry accepts; matching outer quotes are removed only
    after tokenisation.
    """
    if not _IS_WINDOWS:
        return shlex.split(command)
    parts = shlex.split(command, posix=False)
    return [
        part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
        for part in parts
    ]


def _validate_command(command: str, cwd: str) -> list[str]:
    """Parse a registry command into argv, rejecting obviously wrong shapes.

    This is NOT a security boundary and must not be read as one. A registered
    `command` is trusted as the user: whoever can write the registry already
    runs code as that user, so there is nothing to escalate to. The checks here
    exist to catch mistakes and to make one specific class of accident loud —
    a registered entry that hands arbitrary text to a shell (`bash -c "..."`),
    which would silently defeat `shell=False` and the argv-list launch model.
    The blocklist is intentionally not exhaustive; do not treat it as complete.

    Raises ValueError if the command is empty, the executable looks unsafe, or
    any argument is a shell code-injection flag (-c / --command).
    Returns the argv list ready for Popen.
    """
    argv = _split_command(command)
    if not argv:
        raise ValueError("Empty command")

    exe = argv[0]
    path_module = ntpath if _IS_WINDOWS else posixpath
    exe_name = path_module.basename(exe).casefold()

    if _IS_WINDOWS and path_module.splitext(exe_name)[1] in {".bat", ".cmd"}:
        raise ValueError(
            f"Windows batch launchers are not allowed in registered commands: {exe!r}"
        )

    # Block bare interpreter invocations regardless of path form
    if exe_name in _BLOCKED_BARE_EXECUTABLES and len(argv) == 1:
        raise ValueError(f"Bare interpreter not allowed: {exe!r}")

    # Block shell flags that introduce arbitrary code execution
    for arg in argv[1:]:
        if arg.casefold() in _BLOCKED_SHELL_FLAGS:
            raise ValueError(
                f"Shell code-injection flag {arg!r} not permitted in registered commands"
            )

    if not path_module.isabs(exe):
        # Relative path — must not escape cwd via path traversal.
        # Use normpath (not resolve) so symlinks to system interpreters
        # (e.g. .venv/bin/python → /opt/homebrew/...) are not blocked.
        normalized = path_module.normpath(path_module.join(cwd, exe))
        cwd_normalized = path_module.normpath(cwd)
        try:
            contained = path_module.commonpath([normalized, cwd_normalized])
        except ValueError:
            contained = ""
        if path_module.normcase(contained) != path_module.normcase(cwd_normalized):
            raise ValueError(f"Executable escapes cwd: {exe!r}")

    return argv


def _resolve_executable(argv: list[str], cwd: str, env: dict) -> list[str]:
    """Resolve relative executables against cwd before falling back to PATH."""
    candidate = Path(cwd) / argv[0]
    if not (ntpath if _IS_WINDOWS else posixpath).isabs(argv[0]) and candidate.is_file():
        argv[0] = str(candidate)
        return argv
    resolved = shutil.which(argv[0], path=env.get("PATH"))
    if resolved:
        argv[0] = resolved
    return argv


# ── PID file helpers ──────────────────────────────────────────────────────────

def _pid_path(app_id: str) -> Path:
    safe_id = validate_app_id(app_id)
    base = PIDS_DIR.resolve()
    path = (base / f"{safe_id}.pid").resolve()
    path.relative_to(base)  # raises ValueError if escaped
    return path


def _log_path(app_id: str) -> Path:
    safe_id = validate_app_id(app_id)
    base = APPISTRY_DIR.resolve()
    path = (base / f"{safe_id}.log").resolve()
    path.relative_to(base)  # raises ValueError if escaped
    return path


def _read_pid(app_id: str) -> int | None:
    """Return the stored PID for app_id, or None if absent/unreadable."""
    pid, _created_at = _read_pid_record(app_id)
    return pid


def _read_pid_record(app_id: str) -> tuple[int | None, float | None]:
    """Return PID plus optional process creation time used to detect PID reuse.

    Only a real process id (> 0) is accepted. The non-positive values are not
    merely useless, they are destructive: `os.kill(-1, SIGTERM)` signals *every*
    process the user can signal and `os.kill(0, ...)` signals Appistry's own
    process group, so a PID file containing `-1` or `0` would turn "Stop" into a
    mass kill. Any same-user process can write into ~/.appistry/pids/, so this
    file is not trusted input.
    """
    path = _pid_path(app_id)
    if not path.exists():
        return None, None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid_text, separator, created_text = raw.partition(":")
        created_at = float(created_text) if separator else None
        pid = int(pid_text)
    except (ValueError, OSError):
        return None, None
    if pid <= 0:
        log.warning("Ignoring non-positive PID in PID file for %s", app_id)
        return None, None
    return pid, created_at


def _write_pid(app_id: str, pid: int) -> None:
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    value = str(pid)
    if _IS_WINDOWS:
        try:
            import psutil
        except ImportError:
            psutil = None
        if psutil is not None:
            try:
                value = f"{pid}:{psutil.Process(pid).create_time():.6f}"
            except (psutil.Error, OSError):
                log.debug("Could not record Windows process creation time", exc_info=True)
    _pid_path(app_id).write_text(value, encoding="utf-8")


def _remove_pid(app_id: str) -> None:
    path = _pid_path(app_id)
    if path.exists():
        path.unlink()


# ── Launch-secret helpers ───────────────────────────────────────────────────────
#
# When Appistry owns the dedicated native window (see `appistry window`), it mints
# a per-launch secret, injects it into the app server's environment as
# YGG_LAUNCH_SECRET, and persists it here so a *separate* `appistry` invocation —
# the window command — can read the same value back and hand it to the window.
# The file lives beside the PID file and is deleted on stop(), so it never
# outlives the running instance. It is written 0600 under a 0700 directory so no
# other local user can read a running app's launch secret.

def _secret_path(app_id: str) -> Path:
    safe_id = validate_app_id(app_id)
    base = SECRETS_DIR.resolve()
    path = (base / f"{safe_id}").resolve()
    path.relative_to(base)  # raises ValueError if escaped
    return path


def _restrict_to_owner(path: Path) -> None:
    """Best-effort: make path readable/writable only by the current user.

    POSIX honors chmod bits directly. Windows ignores them (NTFS uses ACLs, not
    mode bits), so this replaces the DACL with one entry granting the owner
    full control and nothing else.
    """
    if _IS_WINDOWS:
        try:
            import ntsecuritycon
            import win32security

            user, _, _ = win32security.LookupAccountName("", os.getlogin())
            dacl = win32security.ACL()
            dacl.AddAccessAllowedAce(
                win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user
            )
            sd = win32security.SECURITY_DESCRIPTOR()
            sd.SetSecurityDescriptorDacl(1, dacl, False)
            win32security.SetFileSecurity(
                str(path), win32security.DACL_SECURITY_INFORMATION, sd
            )
        except Exception:
            log.debug("Could not restrict ACL for %s", path, exc_info=True)
    else:
        mode = 0o700 if path.is_dir() else 0o600
        try:
            path.chmod(mode)
        except OSError:
            pass


def write_launch_secret(app_id: str, secret: str) -> None:
    """Persist a running app's launch secret at ~/.appistry/secrets/{id}.

    The secret and its containing directory are restricted to the current
    user only (0600/0700 on POSIX, an owner-only DACL on Windows). Existing
    files are truncated and re-restricted.
    """
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    _restrict_to_owner(SECRETS_DIR)
    path = _secret_path(app_id)
    # Open with restrictive mode from the outset so the secret is never briefly
    # world-readable between create and chmod (POSIX only — Windows applies
    # the ACL after creation below).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_to_owner(path)  # enforce restriction even if the file pre-existed


def read_launch_secret(app_id: str) -> str | None:
    """Return the persisted launch secret for app_id, or None if absent."""
    path = _secret_path(app_id)
    if not path.exists():
        return None
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return secret or None


def clear_launch_secret(app_id: str) -> None:
    """Delete the persisted launch secret for app_id, if present."""
    path = _secret_path(app_id)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


# ── Public API ────────────────────────────────────────────────────────────────

def is_running(app_id: str) -> bool:
    """Return True if the process recorded for app_id is still alive."""
    pid, expected_created_at = _read_pid_record(app_id)
    # Defense in depth: _read_pid_record already filters these out, but every
    # os.kill below is only safe for a positive pid, so re-assert it here.
    if pid is None or pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            import psutil
        except ImportError:
            _remove_pid(app_id)
            return False
        try:
            candidate = psutil.Process(pid)
            same_process = (
                expected_created_at is None
                or abs(candidate.create_time() - expected_created_at) < 0.01
            )
            running = psutil.pid_exists(pid) and same_process and candidate.is_running()
        except (psutil.NoSuchProcess, OSError):
            running = False
        except psutil.AccessDenied:
            running = True
        if not running:
            _remove_pid(app_id)
        return running
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # Process is gone; clean up the stale PID file
        _remove_pid(app_id)
        return False
    except PermissionError:
        # Process exists but we can't signal it (different user — treat as running)
        return True


def start(entry: AppEntry) -> bool:
    """
    Spawn the app's command as a background subprocess.

    Writes the PID to ~/.appistry/pids/{id}.pid and tails the log to
    ~/.appistry/{id}.log. Returns True if the process appears healthy after
    a brief check; False if it died immediately.
    """
    log_path = _log_path(entry.id)
    APPISTRY_DIR.mkdir(parents=True, exist_ok=True)

    log_fh = log_path.open("a")
    try:
        argv = _validate_command(entry.command, entry.cwd)
    except ValueError as exc:
        log_fh.write(f"Blocked unsafe command: {exc}\n")
        log_fh.close()
        return False

    env = _build_launch_env()
    env["APPISTRY_LAUNCHED"] = "1"
    env["APPISTRY_APP_ID"] = entry.id

    # Mint a per-launch secret and inject it so the app server can gate its
    # session bootstrap on the dedicated window (see `appistry window`). Persist
    # it so the separate window invocation can read the same value back. This is
    # harmless when unused: an app server only enforces the secret if it reads
    # YGG_LAUNCH_SECRET, and the plain-browser path leaves the server permissive.
    launch_secret = secrets.token_urlsafe(32)
    env["YGG_LAUNCH_SECRET"] = launch_secret
    try:
        write_launch_secret(entry.id, launch_secret)
    except (OSError, ValueError) as exc:
        # Never block a launch on secret persistence — the app still runs and
        # falls back to its own launcher/shell for the dedicated-window path.
        log.warning("Could not persist launch secret for %s: %s", entry.id, exc)

    # Resolve the executable against the launch env PATH so bare names like
    # 'node' or 'python3' are found even when the parent process has a minimal
    # PATH (e.g. when spawned from launchd).
    argv = _resolve_executable(argv, entry.cwd, env)

    popen_options = {
        "shell": False,
        "cwd": entry.cwd,
        "env": env,
        "stdout": log_fh,
        "stderr": log_fh,
    }
    if _IS_WINDOWS:
        popen_options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        popen_options["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            argv,
            **popen_options,
        )
    except OSError as exc:
        log_fh.write(f"Failed to start process: {exc}\n")
        log_fh.close()
        clear_launch_secret(entry.id)
        return False

    _write_pid(entry.id, proc.pid)

    # Brief health check — give the process a moment to fail fast
    time.sleep(0.5)
    if proc.poll() is not None:
        # Process already exited
        _remove_pid(entry.id)
        clear_launch_secret(entry.id)
        log_fh.close()
        return False

    log_fh.close()
    return True


def run_foreground(entry: AppEntry) -> int:
    """Run an app in the foreground, using exec on POSIX and a child on Windows.

    Unlike start(), which Popens a detached child, this records the current
    PID as the app's PID and then execs the server in-place, so the running
    server *is* this process. That preserves the launching process's identity
    on macOS: when invoked from an .app bundle's MacOS executable, the server
    inherits the bundle's LaunchServices identity, so file-access (TCC) prompts
    are attributed to the app (e.g. "My App") instead of the raw interpreter
    ("Python"). exec keeps the same PID, so the recorded PID file, is_running(),
    and stop() all continue to work unchanged.

    On POSIX this never returns on success because the process image is replaced.
    On Windows it waits for the child and returns its exit code. Both platforms
    return non-zero if the command is invalid or the executable cannot be found.
    """
    try:
        argv = _validate_command(entry.command, entry.cwd)
    except ValueError as exc:
        print(f"Blocked unsafe command: {exc}", file=sys.stderr)
        return 1

    env = _build_launch_env()
    env["APPISTRY_LAUNCHED"] = "1"
    env["APPISTRY_APP_ID"] = entry.id

    argv = _resolve_executable(argv, entry.cwd, env)

    if _IS_WINDOWS:
        APPISTRY_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with _log_path(entry.id).open("a") as log_fh:
                proc = subprocess.Popen(
                    argv,
                    shell=False,
                    cwd=entry.cwd,
                    env=env,
                    stdout=log_fh,
                    stderr=log_fh,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                _write_pid(entry.id, proc.pid)
                return proc.wait()
        except OSError as exc:
            print(f"Failed to run {argv[0]!r}: {exc}", file=sys.stderr)
            return 1
        finally:
            _remove_pid(entry.id)

    os.chdir(entry.cwd)
    _write_pid(entry.id, os.getpid())

    # Redirect stdout/stderr to the same log start() uses, so the bundle-launch
    # path (where the launcher's fds point at the system console) still captures
    # server output. Done before execve so the replacement image inherits the fds.
    APPISTRY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        log_fd = os.open(str(_log_path(entry.id)),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
    except OSError:
        pass  # logging is best-effort; never block the launch on it

    try:
        os.execve(argv[0], argv, env)
    except OSError as exc:
        _remove_pid(entry.id)
        print(f"Failed to exec {argv[0]!r}: {exc}", file=sys.stderr)
        return 1
    return 0  # unreachable when execve succeeds


def stop(app_id: str) -> bool:
    """
    Stop the process recorded for app_id.

    On POSIX sends SIGTERM, waits up to 5 seconds, then SIGKILL. On Windows,
    terminates the recorded process tree and kills survivors. Removes the PID
    file. Returns True on success, False if no matching process was found.
    """
    pid, expected_created_at = _read_pid_record(app_id)
    # A non-positive pid must never reach os.kill: -1 broadcasts the signal to
    # every process this user can signal, 0 hits our own process group.
    if pid is None or pid <= 0:
        return False

    if _IS_WINDOWS:
        try:
            import psutil

            parent = psutil.Process(pid)
            if (
                expected_created_at is not None
                and abs(parent.create_time() - expected_created_at) >= 0.01
            ):
                _remove_pid(app_id)
                return False
            processes = parent.children(recursive=True)
            processes.append(parent)
            for child in processes:
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _, alive = psutil.wait_procs(processes, timeout=5)
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            psutil.wait_procs(alive, timeout=2)
        except ImportError:
            return False
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return False
        _remove_pid(app_id)
        clear_launch_secret(app_id)
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid(app_id)
        clear_launch_secret(app_id)
        return False
    except PermissionError:
        return False

    # Wait up to 5 seconds for graceful exit
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        # Still alive — escalate to SIGKILL
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    _remove_pid(app_id)
    clear_launch_secret(app_id)
    return True
