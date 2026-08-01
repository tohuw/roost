"""Tests for Appistry launch-page helpers."""

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


def _timer(_seconds):
    return lambda fn: fn


fake_rumps.App = _FakeApp
fake_rumps.timer = _timer
fake_rumps.MenuItem = object
sys.modules.setdefault("rumps", fake_rumps)

import menubar
from registry import AppEntry


def _entry(**kwargs):
    defaults = {
        "id": "widget",
        "name": "Widget",
        "cwd": "/tmp/widget",
        "command": ".venv/bin/python ui/server.py",
        "port": 8009,
        "github_url": "https://github.com/example/widget",
    }
    defaults.update(kwargs)
    return AppEntry(**defaults)


def test_launch_status_ready_when_process_and_http_are_ready(monkeypatch):
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: _entry(id=app_id))
    monkeypatch.setattr(menubar.process, "is_running", lambda app_id: True)
    monkeypatch.setattr(menubar, "_probe_app_port", lambda port: True)

    status = menubar._launch_status("widget")

    assert status["ready"] is True
    assert status["state"] == "ready"
    assert status["url"] == "http://127.0.0.1:8009"


def test_launch_status_waits_without_probing_when_process_is_not_running(monkeypatch):
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: _entry(id=app_id))
    monkeypatch.setattr(menubar.process, "is_running", lambda app_id: False)
    monkeypatch.setattr(
        menubar,
        "_probe_app_port",
        lambda port: (_ for _ in ()).throw(AssertionError("should not probe")),
    )

    status = menubar._launch_status("widget")

    assert status["ready"] is False
    assert status["state"] == "not_running"
    assert "start" in status["message"]


def test_app_search_matches_display_name_case_insensitively():
    assert menubar._app_matches_search(_entry(name="Screen Capture"), "screen") is True


def test_app_search_matches_app_id():
    assert menubar._app_matches_search(_entry(id="screenshot-sorter"), "sorter") is True


def test_app_search_rejects_unrelated_query():
    assert menubar._app_matches_search(_entry(name="Sanity", id="sanity"), "notekeeper") is False


class _FakeSearchMenuItem:
    def __init__(self):
        self.hidden = False


def test_search_filter_hides_nonmatching_running_apps():
    app = menubar.AppistryApp.__new__(menubar.AppistryApp)
    sanity_item = _FakeSearchMenuItem()
    notekeeper_item = _FakeSearchMenuItem()
    empty_item = _FakeSearchMenuItem()
    app._search_query = "san"
    app._running_menu_items = [
        (_entry(name="Sanity", id="sanity"), sanity_item),
        (_entry(name="Notekeeper", id="notekeeper"), notekeeper_item),
    ]
    app._no_search_results = empty_item

    app._apply_search_filter()

    assert sanity_item.hidden is False
    assert notekeeper_item.hidden is True
    assert empty_item.hidden is True


def test_search_filter_shows_empty_state_when_nothing_matches():
    app = menubar.AppistryApp.__new__(menubar.AppistryApp)
    sanity_item = _FakeSearchMenuItem()
    empty_item = _FakeSearchMenuItem()
    app._search_query = "notekeeper"
    app._running_menu_items = [
        (_entry(name="Sanity", id="sanity"), sanity_item),
    ]
    app._no_search_results = empty_item

    app._apply_search_filter()

    assert sanity_item.hidden is True
    assert empty_item.hidden is False


def test_search_enter_opens_first_matching_running_app(monkeypatch):
    app = menubar.AppistryApp.__new__(menubar.AppistryApp)
    app._search_query = "san"
    app._running_menu_items = [
        (_entry(name="Notekeeper", id="notekeeper"), _FakeSearchMenuItem()),
        (_entry(name="Sanity", id="sanity"), _FakeSearchMenuItem()),
    ]
    opened = []
    monkeypatch.setattr(menubar, "_open_launch_page", opened.append)

    result = app._open_first_search_match()

    assert result is True
    assert opened == ["sanity"]


def test_launch_icon_path_accepts_relative_browser_image(monkeypatch, tmp_path):
    icon = tmp_path / "ui" / "static" / "icon.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"fake")
    entry = _entry(cwd=str(tmp_path), icon="ui/static/icon.png")
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: entry)

    assert menubar._launch_icon_path("widget") == icon


def test_launch_icon_path_rejects_path_traversal(monkeypatch, tmp_path):
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"fake")
    entry = _entry(cwd=str(tmp_path), icon="../outside.png")
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: entry)

    assert menubar._launch_icon_path("widget") is None


def test_render_launch_page_escapes_app_name(monkeypatch, tmp_path):
    entry = _entry(cwd=str(tmp_path), name='Bad <Name> "quote"', icon=None)
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: entry)

    page = menubar._render_launch_page("widget")

    assert page is not None
    assert "Bad &lt;Name&gt; &quot;quote&quot;" in page
    assert 'const appId = "widget";' in page


def _rendered_about_html(monkeypatch, tmp_path, about_text, name="Widget"):
    """Invoke _make_about's callback and capture the HTML it would open."""
    about_path = tmp_path / "about.md"
    about_path.write_text(about_text, encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        menubar.webbrowser, "open",
        lambda url: captured.setdefault("url", url),
    )
    callback = menubar._make_about(name, about_path)
    callback(None)
    file_path = Path(captured["url"][len("file://"):])
    return file_path.read_text(encoding="utf-8")


class TestMakeAboutSanitization:
    """about.md is registry-controlled — any registered app can supply one —
    so its rendered HTML must not carry an executable payload into the
    browser that opens it."""

    def test_script_tag_is_stripped(self, monkeypatch, tmp_path):
        doc = _rendered_about_html(
            monkeypatch, tmp_path,
            "# Hi\n\n<script>alert(1)</script>\n\nSome text.",
        )
        assert "<script" not in doc
        assert "alert(1)" not in doc

    def test_javascript_link_is_stripped(self, monkeypatch, tmp_path):
        doc = _rendered_about_html(
            monkeypatch, tmp_path,
            '[click me](javascript:alert(1))',
        )
        assert "javascript:" not in doc

    def test_safe_markdown_is_preserved(self, monkeypatch, tmp_path):
        doc = _rendered_about_html(
            monkeypatch, tmp_path,
            "# Title\n\nSome **bold** text and a [link](https://example.com).",
        )
        assert "<h1>Title</h1>" in doc
        assert "<strong>bold</strong>" in doc
        assert 'href="https://example.com"' in doc

    def test_display_name_title_injection_is_escaped(self, monkeypatch, tmp_path):
        doc = _rendered_about_html(
            monkeypatch, tmp_path, "Hello",
            name='Evil</title><script>alert(1)</script>',
        )
        # The payload must not survive as a live tag — only as escaped text.
        assert "<script>alert(1)</script>" not in doc
        assert "&lt;/title&gt;&lt;script&gt;" in doc


def test_hook_proxy_target_uses_registered_loopback_port(monkeypatch):
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: _entry(id=app_id, port=8123))
    monkeypatch.setattr(menubar.process, "is_running", lambda app_id: True)

    status, target, error = menubar._hook_proxy_target(
        "demo-app",
        "/api/oauth/callback",
        "code=abc&state=xyz",
    )

    assert status == 200
    assert error is None
    assert target == "http://127.0.0.1:8123/api/oauth/callback?code=abc&state=xyz"


def test_hook_proxy_target_rejects_missing_app(monkeypatch):
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: None)

    status, target, error = menubar._hook_proxy_target("missing", "/callback")

    assert status == 404
    assert target is None
    assert "not found" in error


def test_hook_proxy_target_rejects_unsafe_app_id_before_registry_lookup(monkeypatch):
    def fail_if_called(_app_id):
        raise AssertionError("unsafe app id reached registry lookup")

    monkeypatch.setattr(menubar.registry, "get", fail_if_called)

    status, target, error = menubar._hook_proxy_target("bad%0aid", "/callback")

    assert status == 404
    assert target is None
    assert "not found" in error


def test_hook_proxy_target_requires_running_app(monkeypatch):
    monkeypatch.setattr(menubar.registry, "get", lambda app_id: _entry(id=app_id))
    monkeypatch.setattr(menubar.process, "is_running", lambda app_id: False)

    status, target, error = menubar._hook_proxy_target("demo-app", "/callback")

    assert status == 503
    assert target is None
    assert "not running" in error


def test_hook_path_parts_extracts_app_id_and_target_path():
    assert menubar._hook_path_parts("/hooks/my%20app/api/oauth/callback") == (
        "my app",
        "/api/oauth/callback",
    )
    assert menubar._hook_path_parts("/hooks/no-target") is None


def test_hook_proxy_does_not_follow_upstream_redirects():
    sink_hits = []

    class _ThreadedTestServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class _Sink(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            sink_hits.append(self.path)
            self.send_response(200)
            self.end_headers()

    sink = _ThreadedTestServer(("127.0.0.1", 0), _Sink)
    sink_url = f"http://127.0.0.1:{sink.server_address[1]}/leak"

    class _Redirector(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", sink_url)
            self.end_headers()

    redirector = _ThreadedTestServer(("127.0.0.1", 0), _Redirector)
    try:
        threading.Thread(target=sink.serve_forever, daemon=True).start()
        threading.Thread(target=redirector.serve_forever, daemon=True).start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{redirector.server_address[1]}/callback"
        )

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            menubar._hook_urlopen(request)

        assert exc_info.value.code == 302
        assert exc_info.value.headers["Location"] == sink_url
        assert sink_hits == []
        exc_info.value.close()
    finally:
        redirector.shutdown()
        redirector.server_close()
        sink.shutdown()
        sink.server_close()


def test_hook_proxy_failure_does_not_log_oauth_query_secrets(
    monkeypatch, tmp_path, caplog
):
    hook_port = menubar._free_port()
    monkeypatch.setattr(menubar.registry, "APPISTRY_DIR", tmp_path)
    monkeypatch.setattr(menubar.hooks, "hook_port", lambda: hook_port)
    monkeypatch.setattr(
        menubar.registry,
        "get",
        lambda app_id: _entry(id=app_id, port=8123),
    )
    monkeypatch.setattr(menubar.process, "is_running", lambda _app_id: True)
    monkeypatch.setattr(
        menubar,
        "_hook_urlopen",
        lambda _request: (_ for _ in ()).throw(OSError("upstream unavailable")),
    )
    code = "oauth-code-must-not-be-logged"
    state = "oauth-state-must-not-be-logged"

    try:
        assert menubar._hook_server_start() == hook_port
        with caplog.at_level("WARNING", logger=menubar.log.name):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{hook_port}/hooks/demo-app/callback"
                    f"?code={code}&state={state}",
                    timeout=2,
                )
        assert exc_info.value.code == 502
        exc_info.value.close()
    finally:
        menubar._hook_server_shutdown()

    assert "app_id=demo-app" in caplog.text
    assert code not in caplog.text
    assert state not in caplog.text


def test_help_server_start_creates_missing_port_file_parent(monkeypatch, tmp_path):
    port_path = tmp_path / "fresh-profile" / ".appistry" / "menubar-http-port"
    monkeypatch.setattr(menubar.registry, "APPISTRY_DIR", tmp_path / "registry")
    monkeypatch.setattr(menubar, "_HELP_PORT_PATH", port_path)

    try:
        port = menubar._help_server_start()
        assert port_path.read_text(encoding="utf-8") == str(port)
    finally:
        menubar._help_server_shutdown()
