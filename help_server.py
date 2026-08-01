"""The loopback help server, shared by both trays.

The tray needs somewhere to put a Help page, and a menu item cannot render
Markdown. So one short-lived loopback server renders ``help.md`` and shuts itself
down when nobody has asked for it in a while. It also answers ``/api/status``,
which is how ``appistry ui`` and the Windows installer tell a live tray from a
stale port file.

**This is not a proxy and must never become one.** It forwards nothing, reaches
nothing, and holds no credential. That is deliberate: the previous design in this
repository proxied requests to app servers on a fixed loopback port, and a fixed
loopback port is reachable by any web page the user has open. Because the proxy
rewrote ``Host`` to a clean loopback value, a drive-by request arrived at the
upstream app looking locally originated — laundering an attack straight past the
consumer's own ``require_local_origin`` check (see huginn's
``huginn/server/app.py``, which rejects a foreign ``Host`` and a cross-origin
``Origin`` precisely so it can trust what reaches it). The fix is structural:
there is no upstream, so there is nothing to launder.

What is left still has to defend itself, because the same "any page can reach
loopback" property applies to this server:

- ``Host`` must name a loopback address, or the request is refused. A page served
  from any other hostname carries that hostname here even when it resolves to
  127.0.0.1, so this is what stops DNS rebinding.
- **Any** ``Origin`` is refused. This server has no browser-facing API a page
  should be scripting; the only legitimate caller is the user's own navigation
  from the tray, which sends no ``Origin``.
- Only ``GET`` is routed. A request carrying a body is refused outright rather
  than having its body ignored.
- Responses are built from a fixed set of headers, never copied from anywhere,
  and carry ``nosniff`` plus a nonce-based CSP that forbids every external
  source.
"""

from __future__ import annotations

import html
import http.server
import json
import logging
import secrets
import socket
import socketserver
import threading
from pathlib import Path

import paths

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
HELP_SOURCE = HERE / "help.md"

PORT_FILE_NAME = "menubar-http-port"

#: Shut the server down after this long without a request. The Help page is
#: opened once in a while, not held open, so there is no reason for a listener to
#: sit on a loopback port for the whole session.
IDLE_SECONDS = 600

#: Hosts accepted in the ``Host`` header.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: A request body is not merely unused here, it is a sign the caller has
#: mistaken this for an API. Cap what will even be acknowledged.
MAX_REQUEST_BODY = 0

# The help page carries its own inline <style> and loads nothing else, so a
# per-response nonce lets the policy forbid every external source outright.
# default-src 'none' means an injected <script src>, <img>, or form post has
# nowhere to go even if the Markdown sanitiser were ever bypassed.
_CSP_TEMPLATE = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "style-src 'nonce-{nonce}'; "
    "script-src 'nonce-{nonce}'; "
    "connect-src 'self'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

_server: "socketserver.TCPServer | None" = None
_port: int = 0
_idle_timer: "threading.Timer | None" = None
# Reentrant: _reset_idle_timer mutates the timer from request threads and is also
# called from inside start(), which already holds this lock.
_lock = threading.RLock()


def port_file_path() -> Path:
    return paths.APPISTRY_DIR / PORT_FILE_NAME


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ── Request validation ────────────────────────────────────────────────────────

def request_host_is_local(headers) -> bool:
    """Return True if the ``Host`` header names a loopback address.

    Mirrors the check the ravens make themselves. A page served from any other
    hostname carries that hostname in ``Host`` even when it resolves to
    127.0.0.1, so this is what stops DNS rebinding and drive-by requests.
    """
    raw = headers.get("Host")
    if not raw:
        return False
    host = raw.strip()
    if host.startswith("["):  # bracketed IPv6 literal
        host = host[1:].partition("]")[0]
    else:
        host = host.partition(":")[0]
    return host.casefold() in _LOCAL_HOSTS


def request_has_origin(headers) -> bool:
    """Return True if the request carries an ``Origin`` header.

    This server exposes no same-origin UI for a page to script, and the only
    legitimate caller — the user's own navigation from the tray menu — is a
    top-level navigation, which sends no ``Origin``. So *any* ``Origin`` means a
    page's script issued the request, and it is refused.
    """
    return headers.get("Origin") is not None


def request_body_length(headers) -> int | None:
    """Return the declared body length, or None if it is unacceptable.

    A negative ``Content-Length`` is not merely invalid but dangerous: passed to
    ``read()`` it means "until EOF", i.e. no bound at all. Anything over the cap
    (which is zero here — this server takes no bodies) is refused before a byte
    is read.
    """
    raw = headers.get("Content-Length")
    if raw is None:
        return 0
    try:
        length = int(raw)
    except ValueError:
        return None
    if length < 0 or length > MAX_REQUEST_BODY:
        return None
    return length


# ── Rendering ─────────────────────────────────────────────────────────────────

def _html_nonce() -> str:
    """Return a fresh CSP nonce for one inline-asset HTML response."""
    return secrets.token_urlsafe(16)


def _nonce_attr(nonce: str) -> str:
    return f' nonce="{html.escape(nonce, quote=True)}"' if nonce else ""


def csp_for(nonce: str) -> str:
    return _CSP_TEMPLATE.format(nonce=nonce)


def render_help_page(nonce: str = "") -> str:
    """Render ``help.md`` into a standalone HTML page.

    The Markdown is sanitised even though it ships with the repository. "The
    renderer sanitises" should not depend on where the Markdown came from, and an
    allowlist that is only correct for trusted input is one refactor away from
    being wrong.
    """
    import markdown as _md
    import nh3

    # help.md holds non-ASCII characters, so the encoding must be explicit:
    # relying on the platform default raises UnicodeDecodeError under a legacy
    # code page and takes the Help page down with it.
    raw_body = _md.markdown(
        (HERE / "help.md").read_text(encoding="utf-8"), extensions=["fenced_code"]
    )
    body = nh3.clean(
        raw_body,
        tags={"h1", "h2", "h3", "p", "em", "strong", "code", "pre", "a",
              "ul", "ol", "li", "blockquote", "br", "hr", "table", "thead",
              "tbody", "tr", "th", "td"},
        attributes={"a": {"href", "title"}},
        url_schemes={"https"},
        link_rel="noopener noreferrer",
    )
    nonce_attr = _nonce_attr(nonce)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Appistry Help</title>
  <style{nonce_attr}>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           max-width: 560px; margin: 60px auto; padding: 0 24px;
           color: #1d1d1f; line-height: 1.6; }}
    h1   {{ font-size: 1.6rem; font-weight: 700; }}
    h2   {{ font-size: 1rem; font-weight: 600; margin-top: 2em; }}
    code, pre {{ font-family: "SF Mono", Menlo, monospace; font-size: 0.88rem; }}
    pre  {{ background: #f5f5f7; border-radius: 8px; padding: 12px 16px; }}
    code {{ background: #e5e5ea; border-radius: 4px; padding: 1px 5px; }}
    pre code {{ background: none; padding: 0; }}
    blockquote {{ background: #fff8e1; border-left: 3px solid #f5a623;
                  border-radius: 0 6px 6px 0; margin: 1em 0; padding: 10px 14px; }}
    blockquote p {{ margin: 0; }}
    hr {{ border: none; border-top: 1px solid #d1d1d6; margin: 1.5em 0; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #1c1c1e; color: #f2f2f7; }}
      pre  {{ background: #2c2c2e; }}
      code {{ background: #3a3a3c; }}
      blockquote {{ background: #2c2c1e; }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


# ── Server ────────────────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # silence the access log

    def do_GET(self):
        if not request_host_is_local(self.headers):
            self.send_error(400, "Unexpected Host header")
            return
        if request_has_origin(self.headers):
            self._json(403, {"error": "Cross-origin requests are not accepted."})
            return
        if request_body_length(self.headers) is None:
            self._json(413, {"error": "This endpoint accepts no request body."})
            return
        _reset_idle_timer()
        try:
            self._route()
        except Exception:
            # A rendering failure must produce a response, not a dropped
            # connection: the browser tab the user just opened would otherwise
            # show a protocol error with nothing to act on.
            log.warning("Help server request failed", exc_info=True)
            try:
                self.send_error(500)
            except OSError:
                pass

    def _route(self):
        path = self.path.partition("?")[0]
        if path in ("/", ""):
            nonce = _html_nonce()
            self._html(render_help_page(nonce), nonce)
        elif path == "/api/status":
            self._json(200, {"service": "appistry", "ok": True})
        else:
            self.send_error(404)

    def _html(self, page: str, nonce: str):
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", csp_for(nonce))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start() -> int:
    """Ensure the help server is running and return its port."""
    global _server, _port
    with _lock:
        if _server is not None:
            _reset_idle_timer()
            return _port
        port = _free_port()
        server = _ThreadedServer(("127.0.0.1", port), _Handler)
        try:
            # Owner-only and atomic: another local user has no business reading
            # which port this user's tray is on, and a reader must never see a
            # half-written port.
            paths.atomic_write_text(port_file_path(), str(port))
            threading.Thread(
                target=server.serve_forever, name="appistry-help", daemon=True
            ).start()
        except Exception:
            server.server_close()
            port_file_path().unlink(missing_ok=True)
            raise
        _server = server
        _port = port
        _reset_idle_timer()
        return port


def _reset_idle_timer() -> None:
    """(Re)arm the idle-shutdown timer.

    Called from request threads, so the module global is mutated under the same
    lock ``start``/``shutdown`` use — otherwise two concurrent requests can leak
    a timer or cancel the live one.
    """
    global _idle_timer
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(IDLE_SECONDS, shutdown)
        _idle_timer.daemon = True
        _idle_timer.start()


def shutdown() -> None:
    """Stop the server and remove the port file if it is still ours."""
    global _server, _port, _idle_timer
    with _lock:
        port = _port
        if _server is not None:
            _server.shutdown()
            # Without server_close() the listening socket stays bound, so an
            # in-process restart hits "port unavailable" and silently disables
            # the Help item.
            _server.server_close()
            _server = None
            _port = 0
        try:
            if port_file_path().read_text(encoding="utf-8").strip() == str(port):
                port_file_path().unlink()
        except OSError:
            pass
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = None


def url() -> str:
    """Return the help page URL, starting the server if it is not up."""
    return f"http://127.0.0.1:{start()}/"


def active_port() -> int | None:
    """Return the port recorded by a running tray, or None.

    Used by ``appistry ui`` and the Windows installer to tell a live tray from a
    stale port file. The value is range-checked because a truncated or
    hand-edited file must not become a port number.
    """
    try:
        port = int(port_file_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None
