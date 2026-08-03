#!/usr/bin/env python3
"""Reference raven: the leading one, with a token and per-item actions.

A worked, runnable implementation of the raven side of ``SPEC.md``, standing in
for Huginn — an agent activity console whose menu is a live list of sessions,
some of which want the user's attention.

Documentation, not a library. Roost does not import this, and neither does
the real Huginn; each project implements the contract itself. This exists so the
contract has an executable form to read and to disagree with.

Run it:

    python3 examples/huginn_raven.py

It binds a free loopback port, mints a token, publishes its descriptor, serves
``/api/menu`` and ``/api/menu/action``, and removes the descriptor on exit.

Four things here are the contract, not this example's preference:

**Declare a range, not a version.** ``min_api``/``max_api`` describe the window
this raven speaks, and Roost renders it if that window overlaps its own.
Comparing for equality is the bug behind huginn issue #38 — a routine bump
silently disabled every participant with nothing on screen to explain it.

**Publish last, remove on exit.** The descriptor is written *after* the port is
listening, so the host never finds a descriptor pointing at a port that is not
yet accepting connections. It is removed on shutdown, so a stopped raven has no
descriptor rather than a stale one — though the host also survives the crash
case, because it checks the recorded PID before trusting the file.

**Own the token.** This raven mints its own, writes it 0600, and names the file
in its descriptor. Roost reads it fresh per request and sends it only here.
Roost never mints or shares a credential, so authentication is the raven's to
provide.

**Defend the port.** A loopback port is reachable by any web page the user has
open, so ``Host`` must be loopback and any ``Origin`` is refused. That check is
the raven's own — the host is not a security boundary on the raven's behalf.

It also shows the lifecycle rows of ``SPEC.md`` §10 — a **Quit** that stops *this*
process. There is nothing special about them: they are action ids like any other,
and the host cannot tell them apart from ``focus:s-1``. What they demonstrate is
the one ordering rule that is easy to get wrong — **answer, then exit**. A raven
that terminates inside its own request handler makes a successful quit look like a
failed action to the host, which is still waiting for a response.

There is deliberately **no Start row**, because there cannot be one: a stopped
raven has withdrawn its descriptor, so the host has nothing to draw. Starting is
the OS supervisor's job (§10).
"""

from __future__ import annotations

import atexit
import hmac
import json
import os
import secrets
import signal
import socket
import socketserver
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

NAME = "huginn"
DISPLAY = "Huginn"

#: The protocol range this raven speaks. A range, never an equality — see the
#: module docstring.
MIN_API = 1
MAX_API = 1

#: Higher sorts earlier in the shared menu. Huginn leads when both ravens are
#: present; when it is absent Muninn's section simply sorts first and the same
#: menu runs standalone. Roost does not know either name — the ordering is
#: entirely this number.
HOST_PRIORITY = 100

TOKEN_HEADER = "X-Huginn-Token"

#: The action id for the lifecycle row this raven publishes. A plain id in this
#: raven's own vocabulary — the protocol reserves no word for it, and the host
#: attaches no meaning to it (SPEC.md §10).
QUIT_ACTION = "quit"

#: A menu request is on the host's menu-build path, so keep the body small and
#: bounded. This is also the cap the host enforces on its side.
MAX_REQUEST_BODY = 64 * 1024

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


# ── Descriptor location ───────────────────────────────────────────────────────

def state_dir() -> Path:
    """Return the shared descriptor directory.

    This resolution order *is* the contract, and both ravens must implement it
    identically or they will publish where the host is not looking:

    1. ``$RAVENS_STATE_DIR`` when set and non-empty.
    2. Windows: ``%LOCALAPPDATA%\\Ravens``, falling back to
       ``~\\AppData\\Local\\Ravens``.
    3. POSIX: ``$XDG_STATE_HOME/ravens``, falling back to
       ``~/.local/state/ravens``.

    Honouring ``XDG_STATE_HOME`` is not optional even if a raven's own state
    directory ignores it: this directory is shared, and putting it somewhere the
    other participant does not expect breaks discovery for both.
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
# Hardcoded on purpose: a real raven has its own model and its own store, and the
# whole point of the contract is that Roost cannot tell the difference.

SESSIONS = [
    {"id": "s-1", "title": "Approve: deploy to staging", "agent": "claude",
     "attention": True},
    {"id": "s-2", "title": "Refactor the parser", "agent": "codex",
     "attention": False},
    {"id": "s-3", "title": "Waiting on review", "agent": "claude",
     "attention": False},
]

_acknowledged: set[str] = set()

#: Set by the ``quit`` action instead of exiting inside the request handler, so
#: the response reaches the host before this process goes away (SPEC.md §10).
#: A ``threading.Event`` because the server is threaded: the action runs on a
#: request thread and the shutdown has to happen on the main one.
_shutdown_requested = threading.Event()


def build_menu() -> dict:
    """Return this raven's whole menu contribution.

    The host renders these labels and hands the ``id`` values back untouched. It
    does not know what ``focus:s-1`` means, and it must not need to — which is
    what lets this function change freely without any change to Roost.

    Rows carry either an ``id`` (POSTed back to ``/api/menu/action``) or a
    ``url`` (opened as ``http://127.0.0.1:{port}{url}``). A row with neither
    renders disabled rather than as a live row that does nothing.
    """
    attention = [
        session for session in SESSIONS
        if session["attention"] and session["id"] not in _acknowledged
    ]
    others = [session for session in SESSIONS if session not in attention]

    sections = []
    if attention:
        sections.append({
            "id": "attention",
            "title": "Needs attention",
            "items": [
                {
                    "id": f"focus:{session['id']}",
                    "label": session["title"],
                    "detail": session["agent"],
                    # "attention" is an intent, not styling: the host maps it to
                    # its own presentation. A raven cannot supply a marker of its
                    # own, because that would be styling by another name.
                    "style": "attention",
                }
                for session in attention
            ],
        })

    items = [
        {
            "id": f"focus:{session['id']}",
            "label": session["title"],
            "detail": session["agent"],
        }
        for session in others
    ]
    items.append({"separator": True})
    # A link row: the host opens it against this raven's own port, so a raven
    # cannot navigate the user anywhere it does not itself serve.
    items.append({"id": "open-console", "label": "Open Console", "url": "/"})
    sections.append({"id": "sessions", "title": "Sessions", "items": items})

    # Lifecycle (SPEC.md §10). Ordinary action ids: the host draws the label and
    # POSTs the id back exactly as it does for "focus:s-1", with no idea that this
    # one ends the process it is talking to. There is no Start row and cannot be —
    # once this process exits, its descriptor is gone and the host has nothing to
    # hang a row on.
    sections.append({
        "id": "lifecycle",
        "items": [{"id": QUIT_ACTION, "label": f"Quit {DISPLAY}"}],
    })

    return {
        "api_version": MAX_API,
        # The host uses this in place of the descriptor's display name when
        # present, so a raven can retitle its own section as its state changes.
        "title": DISPLAY,
        # The host shows this beside the name and sums it across ravens. It is
        # this raven's own number; Roost does not compute it and does not know
        # what it counts.
        "badge": len(attention),
        "sections": sections,
    }


def perform_action(action_id: str) -> dict:
    """Act on an id this raven published.

    The id is this raven's own vocabulary, round-tripped through the host
    unchanged. It still arrives over HTTP from another process, so it is matched
    against what this raven actually issued rather than parsed for meaning.

    ``quit`` does **not** exit here, and that is the point worth copying: this
    function's return value still has to be serialised and written to the socket.
    It records the intent and lets the caller shut down once the response is out
    (SPEC.md §10).
    """
    if action_id == "open-console":
        return {"ok": True}
    if action_id == QUIT_ACTION:
        _shutdown_requested.set()
        return {"ok": True, "stopping": True}
    if action_id.startswith("focus:"):
        session_id = action_id[len("focus:"):]
        if any(session["id"] == session_id for session in SESSIONS):
            _acknowledged.add(session_id)
            return {"ok": True, "focused": session_id}
    return {"ok": False, "error": "unknown action"}


# ── Token ─────────────────────────────────────────────────────────────────────

def write_token(directory: Path) -> tuple[str, Path]:
    """Mint a fresh token and write it owner-only.

    A new token per start, on purpose: the host reads the file on every request
    and never caches, so rotation needs no coordination. The file is opened 0600
    from the outset rather than chmodded afterwards — creating it first would
    leave a window in which it is world-readable.
    """
    token = secrets.token_urlsafe(32)
    path = directory / f"{NAME}.token"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return token, path


# ── Descriptor ────────────────────────────────────────────────────────────────

def publish(directory: Path, port: int, token_path: Path) -> Path:
    """Write this raven's descriptor atomically.

    Atomic because the host may read at any moment and must never see a
    half-written file. The temp file is created in the same directory so the
    replace cannot cross a filesystem boundary.
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
        # The host cross-checks this against the OS's record of when the process
        # began, so a recycled PID cannot pass as a live raven. That is the only
        # reason the field exists.
        "started": time.time(),
        "host_priority": HOST_PRIORITY,
        "token_path": str(token_path),
        "token_header": TOKEN_HEADER,
        # Paths only. The host pins the origin to 127.0.0.1 and the port to the
        # one declared above, so an endpoint cannot redirect it elsewhere.
        "endpoints": {"menu": "/api/menu", "action": "/api/menu/action"},
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


def withdraw(descriptor: Path, token_path: Path) -> None:
    """Remove the descriptor and token so a stopped raven leaves nothing behind.

    Best-effort: if this never runs (a hard kill, a power loss) the host still
    copes, because it checks the recorded PID before trusting the descriptor. A
    stale file is a handled case, not a broken one.
    """
    for path in (descriptor, token_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


# ── HTTP ──────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    """The raven's own API. It defends itself; the host is not its guard."""

    token = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    # ── Request validation ───────────────────────────────────────────────────

    def _host_is_loopback(self) -> bool:
        """A page served from any other hostname carries that name in Host, even
        when it resolves to 127.0.0.1 — so this is what stops DNS rebinding."""
        raw = self.headers.get("Host")
        if not raw:
            return False
        host = raw.strip()
        if host.startswith("["):
            host = host[1:].partition("]")[0]
        else:
            host = host.partition(":")[0]
        return host.casefold() in _LOOPBACK_HOSTS

    def _authorised(self) -> bool:
        """Compare the token in constant time.

        ``==`` on a secret leaks its prefix through timing. ``compare_digest``
        does not, and the cost of using it is nothing.
        """
        supplied = self.headers.get(TOKEN_HEADER) or ""
        return hmac.compare_digest(supplied, self.token)

    def _guard(self) -> bool:
        if not self._host_is_loopback():
            self._json(400, {"error": "unexpected host"})
            return False
        if self.headers.get("Origin") is not None:
            # This API serves no page of its own, so no Origin is legitimate: it
            # means a script issued the request rather than the menu bar.
            self._json(403, {"error": "cross-origin request rejected"})
            return False
        if not self._authorised():
            self._json(401, {"error": "bad or missing token"})
            return False
        return True

    def _read_body(self) -> bytes | None:
        """Read a bounded body, or None if the declared length is unacceptable.

        A negative Content-Length passed to ``read()`` means "until EOF" — no
        bound at all — so it is rejected rather than clamped.
        """
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            length = int(raw)
        except ValueError:
            return None
        if length < 0 or length > MAX_REQUEST_BODY:
            return None
        return self.rfile.read(length)

    # ── Routes ──────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path.partition("?")[0] == "/":
            # The console shell itself: no secret, so it is safely fetchable.
            self._html("<!DOCTYPE html><title>Huginn</title><h1>Huginn</h1>")
            return
        if not self._guard():
            return
        if self.path.partition("?")[0] == "/api/menu":
            self._json(200, build_menu())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        if self.path.partition("?")[0] != "/api/menu/action":
            self._json(404, {"error": "not found"})
            return
        body = self._read_body()
        if body is None:
            self._json(413, {"error": "request body is too large"})
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            self._json(400, {"error": "body is not JSON"})
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            self._json(400, {"error": "expected an object with a string id"})
            return
        self._json(200, perform_action(payload["id"]))

    # ── Responses ───────────────────────────────────────────────────────────

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
    directory.mkdir(parents=True, exist_ok=True)
    token, token_path = write_token(directory)
    _Handler.token = token

    port = _free_port()
    server = _Server(("127.0.0.1", port), _Handler)

    # Publish only after the socket is bound. A descriptor naming a port that is
    # not yet listening would make the host report a healthy raven as
    # unreachable during startup.
    descriptor = publish(directory, port, token_path)

    atexit.register(withdraw, descriptor, token_path)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_a: sys.exit(0))

    print(f"{DISPLAY} listening on http://127.0.0.1:{port}")
    print(f"  descriptor {descriptor}")
    print(f"  token      {token_path}")
    print("Ctrl-C to stop, or use this raven's own Quit row in the menu.")

    # serve_forever() runs on its own thread so this one can wait on the quit
    # event. Calling shutdown() from inside a request handler would deadlock:
    # shutdown() waits for the serve loop to finish the request that is calling
    # it. Waiting here instead is what lets the response go out first.
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while not _shutdown_requested.wait(0.5):
            pass
        print(f"{DISPLAY} stopping: the menu asked it to.")
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
