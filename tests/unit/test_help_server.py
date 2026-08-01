"""Security and robustness regression tests for the local help/launch server."""

import http.client
import sys
import types
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
        "id": "widget",
        "name": "Widget",
        "cwd": "/tmp/widget",
        "command": ".venv/bin/python server.py",
        "port": 8009,
        "github_url": "https://github.com/example/widget",
    }
    defaults.update(kwargs)
    return AppEntry(**defaults)


@pytest.fixture
def help_server(monkeypatch, tmp_path):
    monkeypatch.setattr(menubar.registry, "APPISTRY_DIR", tmp_path)
    monkeypatch.setattr(menubar, "_HELP_PORT_PATH", tmp_path / "menubar-http-port")
    try:
        yield menubar._help_server_start()
    finally:
        menubar._help_server_shutdown()


def _get(port, path, host=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        return response.status, dict(
            (k.lower(), v) for k, v in response.getheaders()
        ), response.read()
    finally:
        conn.close()


# ── F-15: help page must survive a non-UTF-8 locale ──────────────────────────

def test_help_md_is_read_as_utf8(monkeypatch):
    """help.md holds non-ASCII bytes.

    Omitting `encoding="utf-8"` means the platform default applies, which raises
    UnicodeDecodeError under a legacy Windows code page — and neither the
    renderer nor the request handler caught it, so clicking Help dropped the
    connection.
    """
    real_read_text = Path.read_text
    seen = {}

    def spy(self, *args, **kwargs):
        if self.name == "help.md":
            seen["encoding"] = kwargs.get("encoding")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)

    menubar._render_help_page()

    assert seen.get("encoding") == "utf-8"


def test_help_md_actually_contains_non_ascii():
    raw = (menubar.HERE / "help.md").read_bytes()
    assert any(byte > 0x7F for byte in raw), "the F-15 precondition no longer holds"


def test_help_page_renders_under_a_legacy_default_encoding(monkeypatch):
    """Simulate a code page that cannot decode help.md's bytes."""
    real_read_text = Path.read_text

    def strict_ascii(self, *args, **kwargs):
        if kwargs.get("encoding") is None:
            return self.read_bytes().decode("ascii")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", strict_ascii)

    page = menubar._render_help_page()

    assert "<html" in page


def test_help_page_output_is_sanitized(monkeypatch, tmp_path):
    """Consistent with _make_about: the renderer sanitizes, whatever the source."""
    src = tmp_path / "help.md"
    src.write_text(
        "# Help\n\n<script>alert(1)</script>\n\n"
        "[click](javascript:alert(2))\n\n"
        '<img src=x onerror="alert(3)">\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(menubar, "HERE", tmp_path)

    page = menubar._render_help_page()

    assert "<script>alert(1)</script>" not in page
    assert "javascript:" not in page
    assert "onerror" not in page


def test_help_route_returns_500_instead_of_dropping_the_connection(help_server, monkeypatch):
    monkeypatch.setattr(
        menubar, "_render_help_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(UnicodeDecodeError(
            "ascii", b"\xc2\xa0", 0, 1, "simulated legacy code page"
        )),
    )

    status, _headers, _body = _get(help_server, "/")

    assert status == 500


def test_help_page_serves_successfully(help_server):
    status, headers, body = _get(help_server, "/")

    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert b"<html" in body


# ── F-19: Host validation on the help server ─────────────────────────────────

def test_help_server_rejects_foreign_host(help_server):
    status, _headers, _body = _get(help_server, "/", host="attacker.example.com")

    assert status == 400


def test_help_server_rejects_foreign_host_on_api_routes(help_server):
    status, _headers, _body = _get(
        help_server, "/api/status", host="attacker.example.com"
    )

    assert status == 400


def test_help_server_accepts_localhost(help_server):
    status, _headers, _body = _get(help_server, "/api/status", host="localhost")

    assert status == 200


# ── F-19: CSP and sniffing headers ───────────────────────────────────────────

def test_help_page_sets_a_csp_with_a_nonce(help_server):
    status, headers, body = _get(help_server, "/")

    assert status == 200
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "nonce-" in csp
    assert headers["x-content-type-options"] == "nosniff"

    nonce = csp.split("style-src 'nonce-", 1)[1].split("'", 1)[0]
    assert f'nonce="{nonce}"'.encode() in body, "inline <style> carries no nonce"


def test_launch_page_sets_a_csp_with_a_nonce(help_server, monkeypatch):
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: _entry(id=app_id))

    status, headers, body = _get(help_server, "/launch/widget")

    assert status == 200
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    nonce = csp.split("script-src 'nonce-", 1)[1].split("'", 1)[0]
    assert f'<script nonce="{nonce}">'.encode() in body
    assert f'<style nonce="{nonce}">'.encode() in body


def test_csp_nonce_differs_per_response(help_server):
    _s1, h1, _b1 = _get(help_server, "/")
    _s2, h2, _b2 = _get(help_server, "/")

    assert h1["content-security-policy"] != h2["content-security-policy"]


def test_launch_icon_response_pins_its_content_type(help_server, monkeypatch, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    monkeypatch.setattr(
        menubar.registry, "get",
        lambda app_id: _entry(id=app_id, cwd=str(tmp_path), icon="icon.png"),
    )

    status, headers, body = _get(help_server, "/launch-icon/widget")

    assert status == 200
    assert headers["content-type"] == "image/png"
    # The Content-Type comes from a registry-supplied filename extension, so the
    # browser must not be allowed to sniff its way to something else.
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-disposition"] == "inline"
    assert body.startswith(b"\x89PNG")


# ── F-14: icon size cap ──────────────────────────────────────────────────────

def test_oversized_icon_is_refused(tmp_path):
    icon = tmp_path / "huge.png"
    icon.write_bytes(b"\x00" * (menubar._MAX_ICON_BYTES + 1))

    assert menubar._read_icon_bytes(icon) is None


def test_icon_at_the_cap_is_served(tmp_path):
    icon = tmp_path / "big.png"
    icon.write_bytes(b"\x00" * menubar._MAX_ICON_BYTES)

    data = menubar._read_icon_bytes(icon)

    assert data is not None
    assert len(data) == menubar._MAX_ICON_BYTES


def test_icon_cap_matches_windows_support_limit():
    assert menubar._MAX_ICON_BYTES == 10 * 1024 * 1024


def test_oversized_icon_request_returns_404(help_server, monkeypatch, tmp_path):
    icon = tmp_path / "huge.png"
    icon.write_bytes(b"\x00" * (menubar._MAX_ICON_BYTES + 1))
    monkeypatch.setattr(
        menubar.registry, "get",
        lambda app_id: _entry(id=app_id, cwd=str(tmp_path), icon="huge.png"),
    )

    status, _headers, _body = _get(help_server, "/launch-icon/widget")

    assert status == 404


def test_unreadable_icon_returns_none(tmp_path):
    assert menubar._read_icon_bytes(tmp_path / "does-not-exist.png") is None


# ── Host parsing ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:8080", "localhost", "localhost:47658",
    "LOCALHOST", "[::1]", "[::1]:47658",
])
def test_loopback_hosts_accepted(host):
    assert menubar._request_host_is_local({"Host": host}) is True


@pytest.mark.parametrize("host", [
    "attacker.example.com", "attacker.example.com:47658",
    "127.0.0.1.evil.com", "localhost.evil.com", "192.168.1.5",
    "0.0.0.0", "", None,
])
def test_non_loopback_hosts_rejected(host):
    assert menubar._request_host_is_local({"Host": host} if host is not None else {}) is False
