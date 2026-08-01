"""Host election and menu aggregation tests.

Two things are pinned here. First, exactly one process hosts: the lock is
exclusive, it survives nothing, and it is released by the kernel when the holder
dies. Second, aggregation is total — a broken, hostile, or hanging raven produces
a disabled section with a visible reason and never prevents another raven's
section from rendering.
"""

import http.server
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import host
import menu_spec
import raven_client
import ravens
import sanitize

_POLL_INTERVAL = 0.01


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _MenuRaven:
    """A raven that serves one menu payload on loopback."""

    def __init__(self, payload, *, status=200, delay=0.0):
        holder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if holder._delay:
                    time.sleep(holder._delay)
                body = json.dumps(holder.payload).encode("utf-8")
                self.send_response(holder._status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        self.payload = payload
        self._status = status
        self._delay = delay
        self._server = _Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": _POLL_INTERVAL},
            daemon=True,
        ).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _write_descriptor(directory: Path, name: str, port: int, **overrides) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    document = ravens.DescriptorDocument(
        name=name, display=name.capitalize(), port=port, **overrides
    )
    path = directory / f"{name}.json"
    path.write_text(document.to_json(), encoding="utf-8")
    return path


def _menu(title, *labels, badge=0):
    return {
        "api_version": 1,
        "title": title,
        "badge": badge,
        "sections": [{
            "id": "main",
            "title": "Sessions",
            "items": [{"id": f"act:{index}", "label": label}
                      for index, label in enumerate(labels)],
        }],
    }


# ── Host election ─────────────────────────────────────────────────────────────

class TestHostLock:
    def test_first_acquirer_becomes_the_host(self, tmp_path):
        lock = host.HostLock(tmp_path / "menubar.lock")
        try:
            assert lock.acquire() is True
            assert lock.held is True
        finally:
            lock.release()

    def test_second_acquirer_is_refused(self, tmp_path):
        path = tmp_path / "menubar.lock"
        first, second = host.HostLock(path), host.HostLock(path)
        try:
            assert first.acquire() is True
            assert second.acquire() is False
            assert second.held is False
        finally:
            first.release()
            second.release()

    def test_release_hands_the_host_role_over(self, tmp_path):
        path = tmp_path / "menubar.lock"
        first, second = host.HostLock(path), host.HostLock(path)
        try:
            assert first.acquire() is True
            first.release()
            assert second.acquire() is True
        finally:
            first.release()
            second.release()

    def test_reacquire_is_idempotent(self, tmp_path):
        lock = host.HostLock(tmp_path / "menubar.lock")
        try:
            assert lock.acquire() is True
            assert lock.acquire() is True
        finally:
            lock.release()

    def test_release_is_safe_when_never_acquired(self, tmp_path):
        host.HostLock(tmp_path / "menubar.lock").release()

    def test_the_lock_records_the_hosting_pid(self, tmp_path):
        path = tmp_path / "menubar.lock"
        lock = host.HostLock(path)
        try:
            lock.acquire()
            assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            lock.release()

    def test_holder_pid_reports_a_live_host(self, tmp_path):
        path = tmp_path / "menubar.lock"
        lock = host.HostLock(path)
        try:
            lock.acquire()
            assert host.holder_pid(path) == os.getpid()
        finally:
            lock.release()

    def test_holder_pid_ignores_a_dead_pid(self, tmp_path, monkeypatch):
        """A stale recorded pid must not be reported as a live host."""
        path = tmp_path / "menubar.lock"
        path.write_text("999999", encoding="utf-8")
        monkeypatch.setattr(ravens, "pid_is_alive", lambda *_a, **_k: False)
        assert host.holder_pid(path) is None

    @pytest.mark.parametrize("content", ["", "not-a-pid", "-1", "0"])
    def test_holder_pid_ignores_junk(self, tmp_path, content):
        path = tmp_path / "menubar.lock"
        path.write_text(content, encoding="utf-8")
        assert host.holder_pid(path) is None

    def test_holder_pid_when_no_lock_exists(self, tmp_path):
        assert host.holder_pid(tmp_path / "absent.lock") is None

    def test_context_manager_releases(self, tmp_path):
        path = tmp_path / "menubar.lock"
        with host.HostLock(path) as acquired:
            assert acquired is True
        assert host.HostLock(path).acquire() is True

    def test_lock_path_lives_under_the_appistry_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(host.paths, "APPISTRY_DIR", tmp_path)
        assert host.host_lock_path() == tmp_path / host.HOST_LOCK_NAME


# ── Aggregation ───────────────────────────────────────────────────────────────

class TestBuildMenu:
    def test_a_live_raven_contributes_its_sections(self, tmp_path):
        server = _MenuRaven(_menu("Huginn", "Approve: deploy", badge=2))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            model = host.build_model(tmp_path)
            assert len(model.menus) == 1
            menu = model.menus[0]
            assert menu.available is True
            assert menu.display == "Huginn"
            assert menu.spec.badge == 2
            assert menu.spec.sections[0].items[0].label == "Approve: deploy"
        finally:
            server.close()

    def test_an_unreachable_raven_is_disabled_with_a_reason(self, tmp_path):
        server = _MenuRaven(_menu("Huginn"))
        port = server.port
        server.close()
        _write_descriptor(tmp_path, "huginn", port)
        model = host.build_model(tmp_path, timeout=1.0)
        menu = model.menus[0]
        assert menu.available is False
        assert menu.reason
        assert menu.display == "Huginn"

    def test_a_hanging_raven_does_not_hang_the_model(self, tmp_path):
        server = _MenuRaven(_menu("Huginn"), delay=5.0)
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            started = time.monotonic()
            model = host.build_model(tmp_path, timeout=0.3)
            assert time.monotonic() - started < 3.0
            assert model.menus[0].available is False
        finally:
            server.close()

    def test_a_stale_descriptor_is_disabled_with_a_reason(self, tmp_path, monkeypatch):
        _write_descriptor(tmp_path, "huginn", 47100)
        monkeypatch.setattr(ravens, "pid_is_alive", lambda *_a, **_k: False)
        model = host.build_model(tmp_path)
        assert model.menus[0].available is False
        assert "Not running" in model.menus[0].reason

    def test_a_malformed_descriptor_is_disabled_with_a_reason(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "huginn.json").write_text("}{ not json", encoding="utf-8")
        model = host.build_model(tmp_path)
        assert model.menus[0].available is False
        assert "JSON" in model.menus[0].reason

    def test_one_broken_raven_does_not_hide_a_working_one(self, tmp_path):
        """Failure must not be contagious across sections."""
        server = _MenuRaven(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port, host_priority=100)
            (tmp_path / "muninn.json").write_text("broken", encoding="utf-8")
            model = host.build_model(tmp_path, timeout=1.0)
            by_name = {menu.name: menu for menu in model.menus}
            assert by_name["huginn"].available is True
            assert by_name["muninn"].available is False
            assert model.any_available is True
        finally:
            server.close()

    def test_a_raven_returning_junk_is_up_but_empty(self, tmp_path):
        """Distinct from unreachable: it answered, it just said nothing usable."""
        server = _MenuRaven({"sections": "not a list"})
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            assert menu.available is True
            assert menu.spec.is_empty is True
        finally:
            server.close()

    def test_an_erroring_raven_reports_its_status(self, tmp_path):
        server = _MenuRaven({}, status=500)
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            assert menu.available is False
            assert "500" in menu.reason
        finally:
            server.close()

    def test_hostile_labels_never_reach_the_model_unsanitised(self, tmp_path):
        payload = _menu("Hu\x1b[31mginn", "Quit\r\nQuit All", "a‮b")
        server = _MenuRaven(payload)
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            assert not sanitize.contains_unsafe_text(menu.display)
            for item in menu.spec.sections[0].items:
                assert not sanitize.contains_unsafe_text(item.label)
                assert "\n" not in item.label
        finally:
            server.close()

    def test_an_oversized_menu_payload_is_bounded(self, tmp_path):
        payload = {
            "sections": [
                {"id": f"s{index}", "title": "T", "items": [
                    {"id": f"a{index}-{inner}", "label": f"Row {inner}"}
                    for inner in range(200)
                ]}
                for index in range(40)
            ]
        }
        server = _MenuRaven(payload)
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            assert len(menu.spec.sections) <= menu_spec.MAX_SECTIONS
            total = sum(len(section.items) for section in menu.spec.sections)
            assert total <= menu_spec.MAX_TOTAL_ITEMS
        finally:
            server.close()

    def test_an_unexpected_client_failure_is_contained(self, tmp_path, monkeypatch):
        """A bug in the client must not take the whole menu down."""
        def _boom(*_args, **_kwargs):
            raise RuntimeError("client bug")

        _write_descriptor(tmp_path, "huginn", 47100)
        monkeypatch.setattr(raven_client, "fetch_menu", _boom)
        menu = host.build_model(tmp_path).menus[0]
        assert menu.available is False
        assert menu.reason == "Could not be read."

    def test_menu_title_can_override_the_descriptor_display(self, tmp_path):
        server = _MenuRaven(_menu("Huginn (3 active)", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            assert host.build_model(tmp_path).menus[0].display == "Huginn (3 active)"
        finally:
            server.close()

    def test_descriptor_display_is_used_when_the_menu_has_no_title(self, tmp_path):
        server = _MenuRaven({"sections": [
            {"items": [{"id": "a", "label": "Row"}]}
        ]})
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            assert host.build_model(tmp_path).menus[0].display == "Huginn"
        finally:
            server.close()


class TestMenuModel:
    def test_empty_directory_yields_an_empty_model(self, tmp_path):
        model = host.build_model(tmp_path)
        assert model.menus == ()
        assert model.any_available is False
        assert model.badge_total == 0

    def test_missing_directory_yields_an_empty_model(self, tmp_path):
        assert host.build_model(tmp_path / "absent").menus == ()

    def test_badge_total_sums_only_available_ravens(self, tmp_path):
        first = _MenuRaven(_menu("Huginn", "Row", badge=2))
        second = _MenuRaven(_menu("Muninn", "Row", badge=3))
        try:
            _write_descriptor(tmp_path, "huginn", first.port, host_priority=100)
            _write_descriptor(tmp_path, "muninn", second.port)
            assert host.build_model(tmp_path).badge_total == 5
        finally:
            first.close()
            second.close()

    def test_host_priority_orders_the_menu(self, tmp_path):
        """Order is raven-declared data; Appistry knows neither name."""
        low = _MenuRaven(_menu("Muninn", "Row"))
        high = _MenuRaven(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "muninn", low.port, host_priority=10)
            _write_descriptor(tmp_path, "huginn", high.port, host_priority=100)
            model = host.build_model(tmp_path)
            assert [menu.name for menu in model.menus] == ["huginn", "muninn"]
        finally:
            low.close()
            high.close()

    def test_a_companion_alone_renders_the_same_menu(self, tmp_path):
        """Huginn absent: the companion's section simply leads."""
        server = _MenuRaven(_menu("Muninn", "Row"))
        try:
            _write_descriptor(tmp_path, "muninn", server.port, host_priority=10)
            model = host.build_model(tmp_path)
            assert [menu.name for menu in model.menus] == ["muninn"]
            assert model.any_available is True
        finally:
            server.close()

    def test_find_locates_a_menu_by_name(self, tmp_path):
        server = _MenuRaven(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            model = host.build_model(tmp_path)
            assert model.find("huginn") is not None
            assert model.find("nope") is None
        finally:
            server.close()

    def test_signature_is_stable_and_hashable(self, tmp_path):
        server = _MenuRaven(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            first = host.build_model(tmp_path).signature()
            second = host.build_model(tmp_path).signature()
            assert first == second
            assert hash(first)
        finally:
            server.close()

    def test_signature_changes_when_a_raven_changes(self, tmp_path):
        server = _MenuRaven(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            before = host.build_model(tmp_path).signature()
            server.payload = _menu("Huginn", "Different row")
            assert host.build_model(tmp_path).signature() != before
        finally:
            server.close()


class TestActivate:
    def test_a_url_item_returns_a_loopback_url(self, tmp_path):
        server = _MenuRaven({"sections": [
            {"items": [{"label": "Open", "url": "/console"}]}
        ]})
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            item = menu.spec.sections[0].items[0]
            assert host.activate(menu, item) == f"http://127.0.0.1:{server.port}/console"
        finally:
            server.close()

    def test_an_action_item_is_forwarded_and_returns_no_url(self, tmp_path, monkeypatch):
        sent = []
        server = _MenuRaven(_menu("Huginn", "Approve"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            item = menu.spec.sections[0].items[0]
            monkeypatch.setattr(
                raven_client, "send_action",
                lambda descriptor, action_id: sent.append((descriptor.name, action_id)),
            )
            assert host.activate(menu, item) is None
            assert sent == [("huginn", "act:0")]
        finally:
            server.close()

    def test_a_refused_action_is_logged_not_raised(self, tmp_path, monkeypatch):
        server = _MenuRaven(_menu("Huginn", "Approve"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            item = menu.spec.sections[0].items[0]

            def _refuse(*_args, **_kwargs):
                raise raven_client.RavenRequestError("nope")

            monkeypatch.setattr(raven_client, "send_action", _refuse)
            assert host.activate(menu, item) is None
        finally:
            server.close()

    def test_an_inert_item_does_nothing(self):
        menu = menu_spec.RavenMenu("huginn", "Huginn")
        item = menu_spec.MenuItem(label="Text only", enabled=False)
        assert host.activate(menu, item) is None

    def test_an_unavailable_raven_has_nothing_to_activate(self):
        menu = menu_spec.RavenMenu("huginn", "Huginn", reason="Not running.")
        item = menu_spec.MenuItem(label="Row", action_id="a")
        assert host.activate(menu, item) is None

    def test_a_separator_is_never_activated(self, tmp_path):
        server = _MenuRaven(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            assert host.activate(menu, menu_spec.MenuItem(separator=True)) is None
        finally:
            server.close()
