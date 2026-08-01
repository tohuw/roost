#!/usr/bin/env python3
"""Reference raven: the companion, with a lower priority and no token.

The second worked implementation of ``SPEC.md``, standing in for Muninn — the
agent-history companion. It is deliberately the *plainer* of the two examples,
because that is the more useful demonstration: the same contract, a different
raven, and no coordination between them beyond the shared directory and the
declared priority.

Read it against ``huginn_raven.py``. The differences are all things the contract
permits a raven to decide for itself:

- **A lower ``host_priority``**, so Muninn sorts after Huginn when both are
  present and sorts first — alone — when Huginn is not running. Neither raven
  knows the other exists; the ordering is entirely these two numbers, and
  Appistry knows neither name.
- **No ``token_path``.** Appistry never mints a credential on a raven's behalf,
  so a raven with no token file gets unauthenticated requests. Whether that is
  acceptable is the raven's decision, and for a read-only history view on
  loopback it reasonably can be. The ``Host``/``Origin`` checks are *not*
  optional either way — they are what stop a web page reaching this port at all.
- **Link rows rather than actions.** Every row here opens a page against this
  raven's own port. A raven that has nothing to be clicked does not need an
  action endpoint, and Appistry renders it identically.
- **A section that is sometimes empty.** When there is no history the menu has no
  sections, and Appistry draws the raven with "Nothing to report." — which is
  visibly different from a raven it could not reach.

Documentation, not a library: Appistry does not import this, and neither does the
real Muninn.

Run it:

    python3 examples/muninn_raven.py
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import socketserver
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

NAME = "muninn"
DISPLAY = "Muninn"

#: A range, not a version. Equality is the bug behind huginn issue #38: one
#: routine bump silently disabled every participant with nothing on screen to
#: explain it. Widening the window this raven accepts is a one-line change here.
MIN_API = 1
MAX_API = 1

#: Lower than Huginn's, so Huginn leads when both are present. This number is the
#: *only* thing that decides the order; the host has no opinion and no list of
#: known ravens.
HOST_PRIORITY = 50

MAX_REQUEST_BODY = 64 * 1024

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def state_dir() -> Path:
    """Return the shared descriptor directory.

    Byte-for-byte the same rule as ``huginn_raven.state_dir``, and that identity
    is the point: it is the contract, not either raven's preference. A raven that
    resolves this differently publishes where the host is not looking, and the
    failure is silent — an empty menu with nothing to explain it.
    """
    override = os.environ.get("RAVENS_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "Ravens"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "ravens"


# ── This raven's state ────────────────────────────────────────────────────────
#
# Hardcoded on purpose. A real raven reads its own store; Appistry cannot tell.

HISTORY = [
    {"id": "h-1", "title": "Deployed staging", "when": "12m ago"},
    {"id": "h-2", "title": "Reverted parser change", "when": "2h ago"},
    {"id": "h-3", "title": "Merged #412", "when": "yesterday"},
]


def build_menu() -> dict:
    """Return this raven's menu contribution.

    Every row is a link, so this raven needs no action endpoint at all. It still
    declares ``menu`` in its descriptor's endpoints; omitting ``action`` is how a
    raven says it has nothing to be clicked, and Appistry renders the rows the
    same way either way.
    """
    if not HISTORY:
        # No sections is a legitimate answer. Appistry draws the raven with
        # "Nothing to report." — distinct from a raven it could not reach, which
        # gets its own reason instead.
        return {"api_version": MAX_API, "title": DISPLAY, "sections": []}

    items = [
        {
            "label": entry["title"],
            "detail": entry["when"],
            # Opened as http://127.0.0.1:{port}{url}. The host builds that from
            # the descriptor's own port, so a row cannot navigate the user
            # anywhere this raven does not itself serve.
            "url": f"/history/{entry['id']}",
            "style": "muted",
        }
        for entry in HISTORY
    ]
    items.append({"separator": True})
    items.append({"label": "Open History", "url": "/"})

    return {
        "api_version": MAX_API,
        "title": DISPLAY,
        # No badge: nothing here wants attention. Omitting it is the same as
        # zero, and the host sums badges across ravens either way.
        "sections": [{"id": "recent", "title": "Recent", "items": items}],
    }


# ── Descriptor ────────────────────────────────────────────────────────────────

def publish(directory: Path, port: int) -> Path:
    """Write this raven's descriptor atomically.

    Note what is *absent*: no ``token_path`` and no ``token_header``. Appistry
    sends an unauthenticated request in that case rather than inventing a
    credential, which is the whole of what "the host never mints a credential on
    a raven's behalf" means in practice.
    """
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_version": MAX_API,
        "min_api": MIN_API,
        "max_api": MAX_API,
        "name": NAME,
        "display": DISPLAY,
        "pid": os.getpid(),
        "port": port,
        # Cross-checked by the host against the OS's record of when this process
        # began, so a recycled PID cannot pass as a live raven.
        "started": time.time(),
        "host_priority": HOST_PRIORITY,
        "endpoints": {"menu": "/api/menu"},
    }
    target = directory / f"{NAME}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{NAME}.", dir=str(directory))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def withdraw(descriptor: Path) -> None:
    """Remove the descriptor so a stopped raven leaves nothing behind.

    Best-effort. A hard kill skips this, and the host copes: it checks the
    recorded PID before trusting the file, so a stale descriptor renders as "Not
    running" with a reason rather than as a phantom raven.
    """
    try:
        descriptor.unlink(missing_ok=True)
    except OSError:
        pass


# ── HTTP ──────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    """The raven's own API.

    No token, but the loopback checks still apply. Skipping them because there is
    no credential to steal would be backwards: with no token, ``Host`` and
    ``Origin`` are the *only* thing standing between this port and any web page
    the user has open.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _host_is_loopback(self) -> bool:
        raw = self.headers.get("Host")
        if not raw:
            return False
        host = raw.strip()
        if host.startswith("["):
            host = host[1:].partition("]")[0]
        else:
            host = host.partition(":")[0]
        return host.casefold() in _LOOPBACK_HOSTS

    def _guard(self) -> bool:
        if not self._host_is_loopback():
            self._json(400, {"error": "unexpected host"})
            return False
        if self.headers.get("Origin") is not None:
            self._json(403, {"error": "cross-origin request rejected"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        path = self.path.partition("?")[0]
        if path == "/api/menu":
            self._json(200, build_menu())
            return
        if path == "/":
            self._html("<!DOCTYPE html><title>Muninn</title><h1>Muninn</h1>")
            return
        if path.startswith("/history/"):
            entry_id = path[len("/history/"):]
            entry = next((item for item in HISTORY if item["id"] == entry_id), None)
            if entry is None:
                self._json(404, {"error": "not found"})
                return
            # The title is this raven's own data, but it is still escaped: a
            # renderer that only escapes untrusted input is one refactor away
            # from not escaping at all.
            import html

            self._html(
                "<!DOCTYPE html><title>Muninn</title>"
                f"<h1>{html.escape(entry['title'])}</h1>"
                f"<p>{html.escape(entry['when'])}</p>"
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        # This raven publishes no actions, so it accepts none. Answering 405
        # rather than silently succeeding keeps a mistaken caller honest.
        if not self._guard():
            return
        self._json(405, {"error": "this raven publishes no actions"})

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, page: str):
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    directory = state_dir()
    port = _free_port()
    server = _Server(("127.0.0.1", port), _Handler)

    # After the bind, never before: a descriptor naming a port that is not yet
    # listening makes the host report a healthy raven as unreachable.
    descriptor = publish(directory, port)

    atexit.register(withdraw, descriptor)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_a: sys.exit(0))

    print(f"{DISPLAY} listening on http://127.0.0.1:{port}")
    print(f"  descriptor {descriptor}")
    print("  no token: this raven accepts unauthenticated loopback requests.")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
