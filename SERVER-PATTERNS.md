# Server Startup Patterns for Appistry-Integrated Apps

**Version:** 1.0
**Audience:** Developers (and AI assistants) building local web-server apps that integrate with Appistry

---

## Overview

Appistry does not manage port assignment or enforce single-instance behavior; those are
your app's responsibility. This document covers four startup concerns that every
Appistry-integrated server app should address, and how they fit together:

1. **Port hunting** — finding a free port rather than hardcoding one
2. **Single-instance locking** — preventing duplicate server processes
3. **Appistry registration** — publishing the real port and launch metadata
4. **Browser ownership** — letting Appistry own the launch wait page when it spawned the app

These patterns interlock. Implement them in this order at startup; the sections
below explain each one and why the ordering matters.

---

## Startup Order

```
acquire_instance_lock()    # must be first — exits if already running
actual_port = find_free_port(preferred)
write_lock_port(actual_port)   # record port so a future launch can open the browser
register(port=actual_port)     # tell Appistry the real port
open_browser_unless_appistry_launched(actual_port)
uvicorn.run(app, port=actual_port)
```

---

## 1. Single-Instance Locking

### Why not a PID file?

A plain PID file has a stale-lock problem: if the process is killed hard (SIGKILL, power
loss), the file remains. On next launch the app reads a dead PID, has to decide whether
it is stale, and may guess wrong. Using an OS-level file lock solves this cleanly —
`flock` is released automatically by the kernel when the process dies for any reason.

### The lock file

Keep the lock file inside the app's log directory (e.g., `logs/app.lock`). It is not a
log file and should not be rotated, but co-locating it with logs keeps it out of the
project root and in a directory that already exists.

The lock file doubles as a data store. It holds `{pid}:{port}` so a second launch can
read the port and open the browser without any IPC. The format is intentionally simple:
one line, two fields, colon-separated.

### Opening the file

**Do not use `open(path, "w")`**. The `"w"` mode truncates the file immediately, before
you have tried to acquire the lock. If another instance is running, you will destroy its
`pid:port` content before you can read it, and the browser-pop fails.

Use `os.open` with `O_CREAT | O_RDWR` instead. This creates the file if absent and opens
it for read/write without truncating:

```python
raw_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
lock_fd = os.fdopen(raw_fd, "r+")
```

### Acquiring the lock

```python
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    # Another instance holds the lock — handle below
    ...
```

`LOCK_EX` is an exclusive lock (only one holder at a time).
`LOCK_NB` makes the call non-blocking — it raises `BlockingIOError` immediately if the
lock is not available, rather than waiting. Never use a blocking flock in a startup path.

### On success

Truncate, write the PID, flush. The port is not known yet — it will be added in step 3.

```python
lock_fd.seek(0)
lock_fd.truncate()
lock_fd.write(str(os.getpid()))
lock_fd.flush()
```

Keep `lock_fd` open (store it in a module-level variable). Closing it releases the lock.

### On failure (another instance running)

Read the lock file content before closing the fd. The content is `{pid}:{port}` written
by the running instance. Parse the port, open the browser, and exit cleanly (exit 0 —
this is an expected, non-error condition from the user's perspective).

```python
try:
    content = lock_fd.read().strip()
    port = int(content.split(":")[1]) if ":" in content else None
except Exception:
    port = None
lock_fd.close()

if port:
    url = f"http://localhost:{port}"
    subprocess.Popen(["open", url])   # macOS; use xdg-open on Linux
else:
    print("App is already running.", file=sys.stderr)
sys.exit(0)
```

If the port cannot be parsed (e.g., the lock was acquired but the port has not been
written yet — a narrow race at startup), fall through to the no-port branch. The user
still sees a message; they just don't get the automatic browser pop.

---

## 2. Port Hunting

### Why hunt?

Hardcoding a port means the app fails silently or crashes if that port is already in use
— by another app, a previous crashed instance whose socket is in TIME_WAIT, or a
conflicting Appistry-registered app on the same machine. Hunting makes the app resilient
without any user configuration.

### The algorithm

Try to bind the preferred port on loopback. If it fails, increment and retry. Cap the
search at a reasonable range (100 ports is plenty).

```python
import socket

def find_free_port(start: int = 8000) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"No free port found in range {start}–{start + 99}")
```

Bind to `127.0.0.1`, not `0.0.0.0`. You are looking for a port available on loopback
specifically — that is the address your server will actually use.

### Race condition

There is a small window between `find_free_port()` returning and your server binding the
port where another process could claim it. This is acceptable for a local dev tool. If
you need a tighter guarantee, open the socket in `find_free_port`, leave it open, and
pass the bound socket to your server instead of a port number. That is more invasive and
usually unnecessary.

---

## 3. Writing the Port to the Lock File

After `find_free_port()` succeeds, update the lock file with `{pid}:{port}`. This is the
data that a future duplicate-launch will read to pop the browser.

```python
def write_lock_port(lock_fd, port: int) -> None:
    lock_fd.seek(0)
    lock_fd.truncate()
    lock_fd.write(f"{os.getpid()}:{port}")
    lock_fd.flush()
```

This must happen before the server starts accepting connections. A second launch that
arrives after `write_lock_port` will always find a valid port. A second launch that
arrives in the narrow gap between lock acquisition and `write_lock_port` will find only
a PID (no colon), and will fall back to the no-port message path rather than crashing.

---

## 4. Appistry Registration with the Actual Port

Appistry's `register` command is idempotent — call it on every startup. But you must
pass the actual port the server will bind to, not a hardcoded constant. If you hunt for
a port and then register the wrong port, Appistry's "Open" action sends the browser to
the wrong address.

```python
from appistry_integration import register
register(port=actual_port)
```

The `register()` function in the reference `appistry_integration.py` accepts an optional
`port` argument that overrides the module-level `APP_PORT` constant. Always pass it
explicitly when you use port hunting.

Call `register()` after `write_lock_port()` and before `uvicorn.run()` (or whatever
server you use). This ensures:

- The lock file is current before the server is live
- Appistry's registry reflects the real port before any client could open it

Registration is fast (milliseconds) and non-blocking; it will not delay startup
noticeably.

## 5. Browser Ownership and the Launch Page

Appistry opens a branded local launch page as soon as the user chooses Open. That page
polls Appistry until the app responds on the registered port, then redirects to the app.
An Appistry-spawned app should not open a second raw browser tab during startup.

Appistry sets `APPISTRY_LAUNCHED=1` and `APPISTRY_APP_ID=<id>` in the child process
environment. Use that marker to suppress app-owned browser auto-open:

```python
def open_browser_unless_appistry_launched(port: int) -> None:
    if os.environ.get("APPISTRY_LAUNCHED") == "1":
        return
    subprocess.run(["open", f"http://localhost:{port}"], check=False)
```

Keep duplicate-launch browser pop behavior in the instance-lock failure path. That path
is for a user launching the app directly while it is already running, and Appistry may
not be involved.

---

## Complete Startup Example

```python
import fcntl
import os
import socket
import subprocess
import sys
from pathlib import Path

LOG_DIR   = Path(__file__).resolve().parent.parent / "logs"
LOCK_FILE = LOG_DIR / "app.lock"

_lock_fd = None  # module-level; keeps the lock alive for the process lifetime


def acquire_instance_lock() -> None:
    global _lock_fd
    LOG_DIR.mkdir(exist_ok=True)
    raw_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    _lock_fd = os.fdopen(raw_fd, "r+")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            content = _lock_fd.read().strip()
            port = int(content.split(":")[1]) if ":" in content else None
        except Exception:
            port = None
        _lock_fd.close()
        if port:
            url = f"http://localhost:{port}"
            print(f"Already running at {url}", file=sys.stderr)
            subprocess.Popen(["open", url])
        else:
            print("Already running.", file=sys.stderr)
        sys.exit(0)
    _lock_fd.seek(0)
    _lock_fd.truncate()
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()


def write_lock_port(port: int) -> None:
    if _lock_fd is not None:
        _lock_fd.seek(0)
        _lock_fd.truncate()
        _lock_fd.write(f"{os.getpid()}:{port}")
        _lock_fd.flush()


def find_free_port(start: int = 8000) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"No free port found in range {start}–{start + 99}")


def run(preferred_port: int = 8000):
    acquire_instance_lock()

    actual_port = find_free_port(preferred_port)
    if actual_port != preferred_port:
        print(f"Port {preferred_port} in use, using {actual_port}", file=sys.stderr)
    write_lock_port(actual_port)

    try:
        from appistry_integration import register
        register(port=actual_port)
    except Exception:
        pass

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=actual_port)
```

---

## Platform Notes

`fcntl` is available on macOS and Linux. It is not available on Windows. If you need
Windows support, replace the flock logic with a Windows named mutex or a socket-based
lock (bind a well-known port solely as a lock, then hunt a different range for the actual
server). The rest of the patterns are platform-neutral.

`subprocess.Popen(["open", url])` is macOS-specific. Use `["xdg-open", url]` on Linux
or `["start", "", url]` (with `shell=True`) on Windows.

---

## What Appistry Guarantees vs. What It Does Not

| Concern | Who handles it |
|---|---|
| Menu bar UI for registered apps | Appistry |
| Starting / stopping the server process | Appistry (via `appistry start/stop`) |
| Opening the browser to the app | Appistry (via `appistry open`) |
| Port assignment | **Your app** |
| Single-instance enforcement | **Your app** |
| Browser pop on duplicate launch | **Your app** |

Appistry only knows the port you tell it via `register`. It has no way to discover the
actual bound port on its own. If you skip port hunting, or forget to pass the real port
to `register()`, Appistry's "Open" will send the browser to the wrong address.
