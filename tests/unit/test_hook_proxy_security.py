"""Security regression tests for the stable hook proxy and help server.

The hook proxy listens on a fixed, well-known loopback port, so any web page the
user has open can reach it without discovering anything. These tests pin the
properties that keep it from becoming a confused deputy: it must not accept
requests that a page's script could make, it must not forward a victim's ambient
credentials into a local app, and it must not relay anything back that could
inject headers or set cookies for other loopback apps.
"""

import http.client
import http.server
import socketserver
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

fake_rumps = types.ModuleType("rumps")


class _FakeApp:
    pass


fake_rumps.App = _FakeApp
fake_rumps.timer = lambda _seconds: (lambda fn: fn)
fake_rumps.MenuItem = object
sys.modules.setdefault("rumps", fake_rumps)

import menubar
from registry import AppEntry


def _entry(**kwargs):
    defaults = {
        "id": "demo-app",
        "name": "Demo",
        "cwd": "/tmp/demo",
        "command": ".venv/bin/python server.py",
        "port": 8009,
        "github_url": "https://github.com/example/demo",
    }
    defaults.update(kwargs)
    return AppEntry(**defaults)


class _ThreadedTestServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Upstream:
    """A stand-in app server that records exactly what the proxy sent it."""

    def __init__(self, respond=None):
        self.requests = []
        respond = respond or (lambda handler: None)
        recorder = self.requests

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _record(self):
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                recorder.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": body,
                })
                if respond(self) is None:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")

            # Every method is accepted so a proxy that forwards too many verbs
            # is caught here rather than masked by an upstream 501.
            do_GET = _record
            do_POST = _record
            do_PUT = _record
            do_PATCH = _record
            do_DELETE = _record

        self._server = _ThreadedTestServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def upstream():
    server = _Upstream()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    """Start the real hook proxy on an ephemeral port, wired to one app."""
    def _start(port: int):
        hook_port = menubar._free_port()
        monkeypatch.setattr(menubar.registry, "APPISTRY_DIR", tmp_path)
        monkeypatch.setattr(menubar.hooks, "hook_port", lambda: hook_port)
        monkeypatch.setattr(
            menubar.registry, "get",
            lambda app_id: _entry(id=app_id, port=port),
        )
        monkeypatch.setattr(menubar.process, "is_running", lambda _app_id: True)
        assert menubar._hook_server_start() == hook_port
        return hook_port

    try:
        yield _start
    finally:
        menubar._hook_server_shutdown()


def _raw_request(port, method, path, headers, body=b"", host=None):
    """Issue a request without urllib rewriting Host, and return (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for key, value in headers.items():
            conn.putheader(key, value)
        if body:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body or None)
        response = conn.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


# ── F-1: request-side confused deputy ────────────────────────────────────────

def test_proxy_rejects_foreign_host(proxy, upstream):
    """A Host that is not loopback is the signature of DNS rebinding."""
    hook_port = proxy(upstream.port)

    status, _headers, _body = _raw_request(
        hook_port, "GET", "/hooks/demo-app/callback", {},
        host="attacker.example.com",
    )

    assert status == 400
    assert upstream.requests == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.1:47658"])
def test_proxy_accepts_loopback_hosts(proxy, upstream, host):
    hook_port = proxy(upstream.port)

    status, _headers, _body = _raw_request(
        hook_port, "GET", "/hooks/demo-app/callback", {}, host=host,
    )

    assert status == 200
    assert len(upstream.requests) == 1


def test_proxy_rejects_any_origin_header(proxy, upstream):
    """The OAuth browser-return this proxy exists for is a top-level navigation,
    which sends no Origin. An Origin therefore means a page's script called us."""
    hook_port = proxy(upstream.port)

    status, _headers, _body = _raw_request(
        hook_port, "POST", "/hooks/demo-app/callback",
        {"Origin": "http://attacker.example.com", "Content-Type": "text/plain"},
        body=b"payload",
    )

    assert status == 403
    assert upstream.requests == []


def test_proxy_rejects_same_origin_style_origin_too(proxy, upstream):
    """Even a loopback Origin is refused — the proxy serves no same-origin UI."""
    hook_port = proxy(upstream.port)

    status, _headers, _body = _raw_request(
        hook_port, "GET", "/hooks/demo-app/callback",
        {"Origin": f"http://127.0.0.1:{upstream.port}"},
    )

    assert status == 403
    assert upstream.requests == []


def test_proxy_does_not_forward_authorization_or_cookie(proxy, upstream):
    """Ambient credentials must never be replayed into a local app server."""
    hook_port = proxy(upstream.port)

    status, _headers, _body = _raw_request(
        hook_port, "GET", "/hooks/demo-app/callback",
        {
            "Authorization": "Bearer VICTIM_TOKEN",
            "Cookie": "session=VICTIM_SESSION",
            "Referer": "http://attacker.example.com/page",
            "X-Forwarded-For": "1.2.3.4",
            "X-Huginn-Token": "VICTIM_TOKEN",
        },
    )

    assert status == 200
    forwarded = {k.lower() for k in upstream.requests[0]["headers"]}
    assert "authorization" not in forwarded
    assert "cookie" not in forwarded
    assert "referer" not in forwarded
    assert not any(name.startswith("x-") for name in forwarded)

    raw = repr(upstream.requests[0]["headers"])
    assert "VICTIM_TOKEN" not in raw
    assert "VICTIM_SESSION" not in raw


def test_proxy_forwards_only_allowlisted_headers(proxy, upstream):
    hook_port = proxy(upstream.port)

    status, _headers, _body = _raw_request(
        hook_port, "POST", "/hooks/demo-app/callback",
        {
            "Accept": "application/json",
            "Accept-Language": "en-GB",
            "Content-Type": "text/plain",
            "User-Agent": "TestAgent/1.0",
            "If-None-Match": "etag",
        },
        body=b"payload",
    )

    assert status == 200
    headers = upstream.requests[0]["headers"]
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["accept"] == "application/json"
    assert lowered["accept-language"] == "en-GB"
    assert lowered["content-type"] == "text/plain"
    assert lowered["user-agent"] == "TestAgent/1.0"
    assert "if-none-match" not in lowered
    assert upstream.requests[0]["body"] == b"payload"


def test_hook_headers_drops_credentials_and_x_headers():
    headers = {
        "Accept": "*/*",
        "Authorization": "Bearer secret",
        "Cookie": "a=b",
        "Origin": "http://evil.example",
        "Referer": "http://evil.example/",
        "X-Custom": "1",
        "Content-Type": "application/json",
        "User-Agent": "agent",
    }

    forwarded = menubar._hook_headers(headers)

    assert forwarded == {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": "agent",
    }


def test_hook_headers_drops_folded_values():
    assert menubar._hook_headers({"Accept": "a\r\nX-Injected: 1"}) == {}
    assert menubar._hook_headers({"Accept": " folded"}) == {}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_proxy_rejects_state_changing_methods(proxy, upstream, method):
    """The upstream fixture answers all five verbs, so a forwarded PUT would
    reach it and return 200. Only an unimplemented method yields 501."""
    hook_port = proxy(upstream.port)

    status, _headers, _body = _raw_request(
        hook_port, method, "/hooks/demo-app/resource", {},
    )

    assert status == 501
    assert upstream.requests == []


def test_proxy_registers_only_get_and_post():
    """do_PUT/do_PATCH/do_DELETE must not exist at all, not merely be guarded."""
    import inspect

    source = inspect.getsource(menubar._hook_server_start)
    registered = {
        verb for verb in ("GET", "POST", "PUT", "PATCH", "DELETE")
        if f"def do_{verb}(" in source
    }

    assert registered == {"GET", "POST"}


# ── F-3: response-side header injection ──────────────────────────────────────

def test_set_cookie_is_never_relayed():
    """Cookies ignore port, so relaying one would apply it to every loopback app."""
    class _Response:
        status = 200
        headers = {
            "Content-Type": "text/plain",
            "Set-Cookie": "session=attacker; Path=/",
        }

    sent = []

    class _Handler:
        def send_response(self, status):
            sent.append(("status", status))

        def send_header(self, key, value):
            sent.append((key.lower(), value))

        def end_headers(self):
            pass

        class wfile:
            @staticmethod
            def write(_data):
                pass

    menubar._relay_upstream_response(_Handler(), _Response(), b"body")

    assert not any(key == "set-cookie" for key, _ in sent)
    assert ("content-type", "text/plain") in sent


@pytest.mark.parametrize("evil", [
    "text/plain\r\nX-Injected: yes",
    "text/plain\nX-Injected: yes",
    "text/plain\rX-Injected: yes",
    " text/plain",
    "\ttext/plain",
])
def test_crlf_and_obs_fold_header_values_are_rejected(evil):
    assert menubar._header_value_is_unsafe(evil) is True


def test_ordinary_header_values_are_allowed():
    assert menubar._header_value_is_unsafe("text/html; charset=utf-8") is False
    assert menubar._header_value_is_unsafe("http://127.0.0.1:8009/next") is False


def test_relay_drops_non_allowlisted_and_unsafe_headers():
    class _Response:
        status = 302
        headers = {
            "Content-Type": "text/html",
            "Location": "http://127.0.0.1:8009/next",
            "Content-Language": "en",
            "Set-Cookie": "a=b",
            "X-Upstream-Secret": "leak",
            "Server": "upstream/1.0",
        }

    sent = []

    class _Handler:
        def send_response(self, status):
            pass

        def send_header(self, key, value):
            sent.append((key.lower(), value))

        def end_headers(self):
            pass

        class wfile:
            @staticmethod
            def write(_data):
                pass

    menubar._relay_upstream_response(_Handler(), _Response(), b"")

    names = {key for key, _ in sent}
    assert names <= (menubar._HOOK_RESPONSE_HEADER_ALLOWLIST | {"content-length"})
    assert "set-cookie" not in names
    assert "x-upstream-secret" not in names
    assert "server" not in names


def test_upstream_reflected_header_cannot_inject_through_proxy(proxy):
    """End-to-end: an app echoing input into a header must not split our response."""
    def respond(handler):
        # http.client preserves an obs-fold continuation inside a value, so this
        # is how an upstream app "smuggles" extra headers.
        handler.wfile.write(
            b"HTTP/1.0 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"X-Reflected: safe\r\n"
            b"\tSet-Cookie: injected=1\r\n"
            b"Content-Length: 2\r\n"
            b"\r\n"
            b"ok"
        )
        return True

    upstream = _Upstream(respond=respond)
    try:
        hook_port = proxy(upstream.port)
        status, headers, _body = _raw_request(
            hook_port, "GET", "/hooks/demo-app/callback", {},
        )
    finally:
        upstream.close()

    assert status == 200
    names = {key.lower() for key, _ in headers}
    assert "set-cookie" not in names
    assert "x-reflected" not in names
    joined = "".join(value for _key, value in headers)
    assert "injected" not in joined


# ── F-4: body cap ────────────────────────────────────────────────────────────

def test_negative_content_length_yields_413(proxy, upstream):
    """read(-1) means "read until EOF" — an unbounded buffer, not a small one."""
    hook_port = proxy(upstream.port)
    conn = http.client.HTTPConnection("127.0.0.1", hook_port, timeout=5)
    try:
        conn.putrequest("POST", "/hooks/demo-app/callback",
                        skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{hook_port}")
        conn.putheader("Content-Type", "text/plain")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        response = conn.getresponse()
        status = response.status
        response.read()
    finally:
        conn.close()

    assert status == 413
    assert upstream.requests == []


def test_oversized_declared_content_length_yields_413(proxy, upstream):
    hook_port = proxy(upstream.port)
    conn = http.client.HTTPConnection("127.0.0.1", hook_port, timeout=5)
    try:
        conn.putrequest("POST", "/hooks/demo-app/callback",
                        skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{hook_port}")
        conn.putheader("Content-Length", str(menubar._HOOK_MAX_BODY + 1))
        conn.endheaders()
        response = conn.getresponse()
        status = response.status
        response.read()
    finally:
        conn.close()

    assert status == 413
    assert upstream.requests == []


def test_read_capped_body_never_reads_past_the_cap():
    import io

    payload = b"x" * (menubar._HOOK_MAX_BODY + 4096)
    assert menubar._read_capped_body(io.BytesIO(payload), len(payload)) is None


def test_read_capped_body_ignores_negative_declared_length():
    import io

    stream = io.BytesIO(b"unbounded" * 1000)
    assert menubar._read_capped_body(stream, -1) == b""
    assert stream.tell() == 0, "a negative length must not read anything"


def test_read_capped_body_returns_short_bodies_intact():
    import io

    assert menubar._read_capped_body(io.BytesIO(b"hello"), 5) == b"hello"


# ── F-18: socket cleanup ─────────────────────────────────────────────────────

def test_hook_server_shutdown_closes_its_listening_socket(monkeypatch, tmp_path):
    """shutdown() only stops serve_forever; server_close() releases the socket.

    Leaving it bound makes an in-process restart hit the "port unavailable"
    branch, which silently disables every hook URL.
    """
    hook_port = menubar._free_port()
    monkeypatch.setattr(menubar.registry, "APPISTRY_DIR", tmp_path)
    monkeypatch.setattr(menubar.hooks, "hook_port", lambda: hook_port)

    try:
        assert menubar._hook_server_start() == hook_port
        srv = menubar._hook_server
        assert srv is not None
        menubar._hook_server_shutdown()

        assert srv.socket.fileno() == -1, "listening socket was leaked on shutdown"
        assert menubar._hook_server_start() == hook_port, "hook URLs were disabled"
    finally:
        menubar._hook_server_shutdown()


def test_reset_idle_timer_holds_the_help_server_lock():
    """The idle timer global is mutated from request threads."""
    assert menubar._help_server_lock.acquire(blocking=False)
    try:
        holder = []

        def watcher():
            # Should block until the main thread releases the lock.
            menubar._reset_idle_timer()
            holder.append("ran")

        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        thread.join(timeout=0.3)
        assert holder == [], "_reset_idle_timer ran without holding the lock"
    finally:
        menubar._help_server_lock.release()
        thread.join(timeout=2)
        if menubar._help_idle_timer is not None:
            menubar._help_idle_timer.cancel()
            menubar._help_idle_timer = None
