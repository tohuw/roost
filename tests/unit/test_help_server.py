"""Tests for the loopback help server.

Two properties matter here, and they are both about the fact that a loopback
port is reachable by any web page the user happens to have open.

First, this server must never become a proxy again. The design it replaced
forwarded requests to app servers and rewrote ``Host`` to a clean loopback value,
which laundered a drive-by request past the upstream app's own origin check. The
structural fix is that there is no upstream — so the tests below pin that the
module reaches nothing and relays nothing.

Second, the server still has to defend itself: a foreign ``Host``, any ``Origin``
at all, a request body, and a non-GET method are all refused, and every response
carries ``nosniff`` and a per-response CSP nonce.
"""

import http.client
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import help_server
from roost import paths


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
    try:
        yield help_server.start()
    finally:
        help_server.shutdown()


def _request(port, path="/", *, method="GET", host=None, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body is not None:
            conn.send(body)
        response = conn.getresponse()
        return (
            response.status,
            {k.lower(): v for k, v in response.getheaders()},
            response.read(),
        )
    finally:
        conn.close()


# ── It is not a proxy ─────────────────────────────────────────────────────────

class TestItIsNotAProxy:
    """The open-relay shape must not come back.

    The old hook proxy was an unauthenticated relay onto arbitrary local ports
    that rewrote ``Host``, so a request from any web page arrived upstream
    looking locally originated. These tests pin the absence of that machinery,
    not just its current configuration.
    """

    def test_the_module_opens_no_outbound_connections(self):
        source = Path(help_server.__file__).read_text(encoding="utf-8")
        for forbidden in ("urlopen", "build_opener", "HTTPConnection", "Request("):
            assert forbidden not in source, forbidden

    def test_only_get_is_routed(self, server):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, _headers, _body = _request(server, method=method)
            assert status in (400, 501), method

    def test_an_unknown_path_is_not_forwarded_anywhere(self, server):
        status, _headers, _body = _request(server, "/hooks/anything/api/callback")
        assert status == 404

    def test_no_set_cookie_is_ever_emitted(self, server):
        _status, headers, _body = _request(server, "/")
        assert "set-cookie" not in headers


# ── Host, Origin, and body validation ────────────────────────────────────────

class TestRequestValidation:
    def test_a_foreign_host_is_refused(self, server):
        status, _headers, _body = _request(server, "/", host="attacker.example.com")
        assert status == 400

    def test_a_foreign_host_is_refused_on_the_status_route(self, server):
        status, _h, _b = _request(server, "/api/status", host="attacker.example.com")
        assert status == 400

    def test_localhost_is_accepted(self, server):
        status, _headers, _body = _request(server, "/api/status", host="localhost")
        assert status == 200

    def test_any_origin_is_refused(self, server):
        status, _headers, _body = _request(
            server, "/", headers={"Origin": "https://evil.example"}
        )
        assert status == 403

    def test_even_a_loopback_origin_is_refused(self, server):
        """This server has no browser API to script, so no Origin is legitimate."""
        status, _headers, _body = _request(
            server, "/", headers={"Origin": f"http://127.0.0.1:{server}"}
        )
        assert status == 403

    def test_a_request_body_is_refused(self, server):
        status, _headers, _body = _request(server, "/", body=b"payload")
        assert status == 413

    @pytest.mark.parametrize("host", [
        "127.0.0.1", "127.0.0.1:8080", "localhost", "localhost:47658",
        "LOCALHOST", "[::1]", "[::1]:47658",
    ])
    def test_loopback_hosts_accepted(self, host):
        assert help_server.request_host_is_local({"Host": host}) is True

    @pytest.mark.parametrize("host", [
        "attacker.example.com", "attacker.example.com:47658",
        "127.0.0.1.evil.com", "localhost.evil.com", "192.168.1.5",
        "0.0.0.0", "",
    ])
    def test_non_loopback_hosts_rejected(self, host):
        assert help_server.request_host_is_local({"Host": host}) is False

    def test_a_missing_host_is_rejected(self):
        assert help_server.request_host_is_local({}) is False

    @pytest.mark.parametrize("declared", ["-1", "1", "99999999", "not-a-number"])
    def test_unacceptable_content_lengths_are_refused(self, declared):
        """A negative length passed to read() means 'until EOF' — no bound at all."""
        assert help_server.request_body_length({"Content-Length": declared}) is None

    def test_an_absent_content_length_is_zero(self):
        assert help_server.request_body_length({}) == 0

    def test_a_zero_content_length_is_accepted(self):
        assert help_server.request_body_length({"Content-Length": "0"}) == 0


# ── Response headers ─────────────────────────────────────────────────────────

class TestResponseHeaders:
    def test_the_help_page_serves(self, server):
        status, headers, body = _request(server, "/")
        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert b"<html" in body

    def test_the_help_page_sets_a_csp_with_a_nonce(self, server):
        status, headers, body = _request(server, "/")
        assert status == 200
        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "nonce-" in csp
        assert headers["x-content-type-options"] == "nosniff"
        nonce = csp.split("style-src 'nonce-", 1)[1].split("'", 1)[0]
        assert f'nonce="{nonce}"'.encode() in body, "inline <style> carries no nonce"

    def test_the_nonce_differs_per_response(self, server):
        _s1, h1, _b1 = _request(server, "/")
        _s2, h2, _b2 = _request(server, "/")
        assert h1["content-security-policy"] != h2["content-security-policy"]

    def test_the_status_route_identifies_roost(self, server):
        import json

        status, headers, body = _request(server, "/api/status")
        assert status == 200
        assert headers["x-content-type-options"] == "nosniff"
        assert json.loads(body) == {"service": "roost", "ok": True}

    def test_responses_are_not_cached(self, server):
        _status, headers, _body = _request(server, "/")
        assert headers["cache-control"] == "no-store"


# ── Rendering ────────────────────────────────────────────────────────────────

class TestRendering:
    def test_help_md_is_read_as_utf8(self, monkeypatch):
        """help.md holds non-ASCII bytes.

        Omitting ``encoding="utf-8"`` means the platform default applies, which
        raises UnicodeDecodeError under a legacy Windows code page — and took the
        Help page down when neither the renderer nor the handler caught it.
        """
        real_read_text = Path.read_text
        seen = {}

        def spy(self, *args, **kwargs):
            if self.name == "help.md":
                seen["encoding"] = kwargs.get("encoding")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy)
        help_server.render_help_page()
        assert seen.get("encoding") == "utf-8"

    def test_help_md_actually_contains_non_ascii(self):
        raw = help_server.HELP_SOURCE.read_bytes()
        assert any(byte > 0x7F for byte in raw), "the precondition no longer holds"

    def test_the_unavailability_table_actually_renders(self):
        """help.md explains the "why is my bird greyed out?" reasons in a table.

        Markdown needs the ``tables`` extension for that, and nh3 needs the table
        tags in its allowlist. Miss either and the table silently degrades to a
        wall of pipe characters, which is the part of the page a confused user is
        most likely to be reading.
        """
        page = help_server.render_help_page()
        assert "<table>" in page
        assert "<th>" in page
        assert "<td>" in page
        assert "|---|" not in page, "the table fell through as literal Markdown"

    def test_the_shipped_help_page_covers_the_unavailable_case(self):
        """A greyed-out bird is the state users need explained, so it must be
        documented in the copy that is actually served."""
        page = help_server.render_help_page()
        assert "Not running" in page
        assert "roost birds" in page

    def test_the_page_renders_under_a_legacy_default_encoding(self, monkeypatch):
        real_read_text = Path.read_text

        def strict_ascii(self, *args, **kwargs):
            if kwargs.get("encoding") is None:
                return self.read_bytes().decode("ascii")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", strict_ascii)
        assert "<html" in help_server.render_help_page()

    def test_the_rendered_markdown_is_sanitized(self, monkeypatch, tmp_path):
        """The renderer sanitises whatever the source, not only untrusted input."""
        src = tmp_path / "help.md"
        src.write_text(
            "# Help\n\n<script>alert(1)</script>\n\n"
            "[click](javascript:alert(2))\n\n"
            '<img src=x onerror="alert(3)">\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(help_server, "HERE", tmp_path)

        page = help_server.render_help_page()

        assert "<script>alert(1)</script>" not in page
        assert "javascript:" not in page
        assert "onerror" not in page

    def test_a_render_failure_returns_500_rather_than_dropping_the_connection(
        self, server, monkeypatch
    ):
        monkeypatch.setattr(
            help_server, "render_help_page",
            lambda *_a, **_k: (_ for _ in ()).throw(UnicodeDecodeError(
                "ascii", b"\xc2\xa0", 0, 1, "simulated legacy code page"
            )),
        )
        status, _headers, _body = _request(server, "/")
        assert status == 500


# ── Port file ────────────────────────────────────────────────────────────────

class TestPortFile:
    def test_the_port_file_records_the_live_port(self, server, tmp_path):
        assert help_server.port_file_path().read_text(encoding="utf-8") == str(server)

    def test_the_port_file_is_owner_only(self, server):
        import stat

        if sys.platform == "win32":
            pytest.skip("POSIX mode bits are not meaningful on Windows")
        mode = stat.S_IMODE(help_server.port_file_path().stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_shutdown_removes_the_port_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
        help_server.start()
        help_server.shutdown()
        assert help_server.port_file_path().exists() is False

    def test_a_foreign_port_file_is_left_alone(self, monkeypatch, tmp_path):
        """Another instance's port file must not be deleted by our shutdown."""
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
        help_server.start()
        help_server.port_file_path().write_text("65000", encoding="utf-8")
        help_server.shutdown()
        assert help_server.port_file_path().read_text(encoding="utf-8") == "65000"

    def test_active_port_reads_the_recorded_port(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
        help_server.port_file_path().write_text("54321", encoding="utf-8")
        assert help_server.active_port() == 54321

    @pytest.mark.parametrize("content", ["", "0", "-1", "70000", "not-a-port"])
    def test_active_port_rejects_junk(self, monkeypatch, tmp_path, content):
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
        help_server.port_file_path().write_text(content, encoding="utf-8")
        assert help_server.active_port() is None

    def test_active_port_when_no_file_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
        assert help_server.active_port() is None

    def test_start_is_idempotent(self, server, monkeypatch, tmp_path):
        assert help_server.start() == server

    def test_start_creates_a_missing_state_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "fresh" / "roost")
        try:
            port = help_server.start()
            assert help_server.port_file_path().read_text(encoding="utf-8") == str(port)
        finally:
            help_server.shutdown()
