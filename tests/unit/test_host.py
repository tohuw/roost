"""Host election and menu aggregation tests.

Two things are pinned here. First, exactly one process hosts: the lock is
exclusive, it survives nothing, and it is released by the kernel when the holder
dies. Second, aggregation is total — a broken, hostile, or hanging bird produces
a disabled section with a visible reason and never prevents another bird's
section from rendering.
"""

import http.server
import json
import os
import socketserver
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import host
from roost import menu_spec
from roost import bird_client
from roost import birds
from roost import sanitize

_POLL_INTERVAL = 0.01

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX mode bits are not meaningful on Windows"
)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _MenuBird:
    """A bird that serves one menu payload on loopback."""

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
    document = birds.DescriptorDocument(
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
        lock = host.HostLock(tmp_path / host.HOST_LOCK_NAME)
        try:
            assert lock.acquire() is True
            assert lock.held is True
        finally:
            lock.release()

    def test_second_acquirer_is_refused(self, tmp_path):
        path = tmp_path / host.HOST_LOCK_NAME
        first, second = host.HostLock(path), host.HostLock(path)
        try:
            assert first.acquire() is True
            assert second.acquire() is False
            assert second.held is False
        finally:
            first.release()
            second.release()

    def test_release_hands_the_host_role_over(self, tmp_path):
        path = tmp_path / host.HOST_LOCK_NAME
        first, second = host.HostLock(path), host.HostLock(path)
        try:
            assert first.acquire() is True
            first.release()
            assert second.acquire() is True
        finally:
            first.release()
            second.release()

    def test_reacquire_is_idempotent(self, tmp_path):
        lock = host.HostLock(tmp_path / host.HOST_LOCK_NAME)
        try:
            assert lock.acquire() is True
            assert lock.acquire() is True
        finally:
            lock.release()

    def test_release_is_safe_when_never_acquired(self, tmp_path):
        host.HostLock(tmp_path / host.HOST_LOCK_NAME).release()

    def test_the_lock_records_the_hosting_pid(self, tmp_path):
        path = tmp_path / host.HOST_LOCK_NAME
        lock = host.HostLock(path)
        try:
            lock.acquire()
            assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            lock.release()

    def test_holder_pid_reports_a_live_host(self, tmp_path):
        path = tmp_path / host.HOST_LOCK_NAME
        lock = host.HostLock(path)
        try:
            lock.acquire()
            assert host.holder_pid(path) == os.getpid()
        finally:
            lock.release()

    def test_holder_pid_ignores_a_dead_pid(self, tmp_path, monkeypatch):
        """A stale recorded pid must not be reported as a live host."""
        path = tmp_path / host.HOST_LOCK_NAME
        path.write_text("999999", encoding="utf-8")
        monkeypatch.setattr(birds, "pid_is_alive", lambda *_a, **_k: False)
        assert host.holder_pid(path) is None

    @pytest.mark.parametrize("content", ["", "not-a-pid", "-1", "0"])
    def test_holder_pid_ignores_junk(self, tmp_path, content):
        path = tmp_path / host.HOST_LOCK_NAME
        path.write_text(content, encoding="utf-8")
        assert host.holder_pid(path) is None

    def test_holder_pid_when_no_lock_exists(self, tmp_path):
        assert host.holder_pid(tmp_path / "absent.lock") is None

    def test_context_manager_releases(self, tmp_path):
        path = tmp_path / host.HOST_LOCK_NAME
        with host.HostLock(path) as acquired:
            assert acquired is True
        assert host.HostLock(path).acquire() is True

    def test_lock_path_lives_under_the_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(host.paths, "STATE_DIR", tmp_path)
        assert host.host_lock_path() == tmp_path / host.HOST_LOCK_NAME

    def test_lock_path_is_not_inside_the_install_tree(self, monkeypatch, tmp_path):
        """The lock must not live beside the code.

        A lock file in the install directory cannot be created from a read-only
        or shared install, and a mode that lets other local users read it
        publishes the host's PID.
        """
        monkeypatch.setattr(host.paths, "STATE_DIR", tmp_path)
        repo = Path(host.__file__).resolve().parent
        assert repo not in host.host_lock_path().resolve().parents


@_POSIX_ONLY
class TestHostLockPermissions:
    def test_the_lock_file_is_owner_only(self, tmp_path):
        path = tmp_path / host.HOST_LOCK_NAME
        lock = host.HostLock(path)
        try:
            assert lock.acquire() is True
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            lock.release()

    def test_a_preexisting_world_readable_lock_is_re_restricted(self, tmp_path):
        """An older build created this file 0644; reopening keeps the old mode."""
        path = tmp_path / host.HOST_LOCK_NAME
        path.write_text("1", encoding="utf-8")
        path.chmod(0o644)
        lock = host.HostLock(path)
        try:
            assert lock.acquire() is True
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            lock.release()

    def test_the_lock_mode_is_not_left_to_umask(self, tmp_path):
        previous = os.umask(0o000)
        try:
            path = tmp_path / host.HOST_LOCK_NAME
            lock = host.HostLock(path)
            try:
                assert lock.acquire() is True
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
            finally:
                lock.release()
        finally:
            os.umask(previous)


@_POSIX_ONLY
class TestHostLockOnAnUnwritablePath:
    """An unusable lock path must be reportable, never an exception.

    The tray acquires the lock before it has a UI to show an error in, so an
    uncaught PermissionError here is a silent failure to launch.
    """

    def test_an_uncreatable_state_directory_does_not_raise(self, tmp_path):
        """The state directory cannot be created because a file is in its place."""
        blocker = tmp_path / "state"
        blocker.write_text("not a directory", encoding="utf-8")

        lock = host.HostLock(blocker / host.HOST_LOCK_NAME)

        assert lock.acquire() is False
        assert lock.failure == host.UNWRITABLE
        assert lock.reason
        assert lock.held is False

    def test_an_unopenable_lock_path_does_not_raise(self, tmp_path):
        """The lock path exists but cannot be opened for writing."""
        occupied = tmp_path / host.HOST_LOCK_NAME
        occupied.mkdir()  # os.open(dir, O_RDWR) raises IsADirectoryError

        lock = host.HostLock(occupied)

        assert lock.acquire() is False
        assert lock.failure == host.UNWRITABLE
        assert lock.reason
        assert lock.held is False

    def test_a_read_only_install_directory_is_never_the_lock_location(
        self, monkeypatch, tmp_path
    ):
        """Regression: the lock used to live in the repo, so a read-only install
        made it uncreatable and the tray died before drawing anything."""
        install = tmp_path / "install"
        install.mkdir()
        install.chmod(0o500)
        state = tmp_path / "state"
        monkeypatch.setattr(host.paths, "STATE_DIR", state)
        try:
            lock = host.HostLock()
            try:
                assert lock.acquire() is True
                assert install not in lock.path.parents
            finally:
                lock.release()
        finally:
            install.chmod(0o700)

    def test_contention_is_distinguished_from_unwritability(self, tmp_path):
        path = tmp_path / host.HOST_LOCK_NAME
        first, second = host.HostLock(path), host.HostLock(path)
        try:
            assert first.acquire() is True
            assert second.acquire() is False
            assert second.failure == host.CONTENDED
        finally:
            first.release()
            second.release()

    def test_a_failed_acquire_clears_a_previous_reason(self, tmp_path):
        path = tmp_path / host.HOST_LOCK_NAME
        lock = host.HostLock(path)
        lock.failure = host.UNWRITABLE
        lock.reason = "stale"
        try:
            assert lock.acquire() is True
            assert lock.failure == ""
            assert lock.reason == ""
        finally:
            lock.release()


# ── Aggregation ───────────────────────────────────────────────────────────────

class TestBuildMenu:
    def test_a_live_bird_contributes_its_sections(self, tmp_path):
        server = _MenuBird(_menu("Huginn", "Approve: deploy", badge=2))
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

    def test_an_unreachable_bird_is_disabled_with_a_reason(self, tmp_path):
        server = _MenuBird(_menu("Huginn"))
        port = server.port
        server.close()
        _write_descriptor(tmp_path, "huginn", port)
        model = host.build_model(tmp_path, timeout=1.0)
        menu = model.menus[0]
        assert menu.available is False
        assert menu.reason
        assert menu.display == "Huginn"

    def test_a_hanging_bird_does_not_hang_the_model(self, tmp_path):
        server = _MenuBird(_menu("Huginn"), delay=5.0)
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
        monkeypatch.setattr(birds, "pid_is_alive", lambda *_a, **_k: False)
        model = host.build_model(tmp_path)
        assert model.menus[0].available is False
        assert "Not running" in model.menus[0].reason

    def test_a_malformed_descriptor_is_disabled_with_a_reason(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "huginn.json").write_text("}{ not json", encoding="utf-8")
        model = host.build_model(tmp_path)
        assert model.menus[0].available is False
        assert "JSON" in model.menus[0].reason

    def test_one_broken_bird_does_not_hide_a_working_one(self, tmp_path):
        """Failure must not be contagious across sections."""
        server = _MenuBird(_menu("Huginn", "Row"))
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

    def test_a_bird_returning_junk_is_up_but_empty(self, tmp_path):
        """Distinct from unreachable: it answered, it just said nothing usable."""
        server = _MenuBird({"sections": "not a list"})
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            assert menu.available is True
            assert menu.spec.is_empty is True
        finally:
            server.close()

    def test_an_erroring_bird_reports_its_status(self, tmp_path):
        server = _MenuBird({}, status=500)
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            assert menu.available is False
            assert "500" in menu.reason
        finally:
            server.close()

    def test_hostile_labels_never_reach_the_model_unsanitised(self, tmp_path):
        payload = _menu("Hu\x1b[31mginn", "Quit\r\nQuit All", "a‮b")
        server = _MenuBird(payload)
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
        server = _MenuBird(payload)
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
        monkeypatch.setattr(bird_client, "fetch_menu", _boom)
        menu = host.build_model(tmp_path).menus[0]
        assert menu.available is False
        assert menu.reason == "Could not be read."

    def test_menu_title_can_override_the_descriptor_display(self, tmp_path):
        server = _MenuBird(_menu("Huginn (3 active)", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            assert host.build_model(tmp_path).menus[0].display == "Huginn (3 active)"
        finally:
            server.close()

    def test_descriptor_display_is_used_when_the_menu_has_no_title(self, tmp_path):
        server = _MenuBird({"sections": [
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

    def test_badge_total_sums_only_available_birds(self, tmp_path):
        first = _MenuBird(_menu("Huginn", "Row", badge=2))
        second = _MenuBird(_menu("Muninn", "Row", badge=3))
        try:
            _write_descriptor(tmp_path, "huginn", first.port, host_priority=100)
            _write_descriptor(tmp_path, "muninn", second.port)
            assert host.build_model(tmp_path).badge_total == 5
        finally:
            first.close()
            second.close()

    def test_host_priority_orders_the_menu(self, tmp_path):
        """Order is bird-declared data; Roost knows neither name."""
        low = _MenuBird(_menu("Muninn", "Row"))
        high = _MenuBird(_menu("Huginn", "Row"))
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
        server = _MenuBird(_menu("Muninn", "Row"))
        try:
            _write_descriptor(tmp_path, "muninn", server.port, host_priority=10)
            model = host.build_model(tmp_path)
            assert [menu.name for menu in model.menus] == ["muninn"]
            assert model.any_available is True
        finally:
            server.close()

    def test_find_locates_a_menu_by_name(self, tmp_path):
        server = _MenuBird(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            model = host.build_model(tmp_path)
            assert model.find("huginn") is not None
            assert model.find("nope") is None
        finally:
            server.close()

    def test_signature_is_stable_and_hashable(self, tmp_path):
        server = _MenuBird(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            first = host.build_model(tmp_path).signature()
            second = host.build_model(tmp_path).signature()
            assert first == second
            assert hash(first)
        finally:
            server.close()

    def test_signature_changes_when_a_bird_changes(self, tmp_path):
        server = _MenuBird(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            before = host.build_model(tmp_path).signature()
            server.payload = _menu("Huginn", "Different row")
            assert host.build_model(tmp_path).signature() != before
        finally:
            server.close()


def _attention_menu(name="huginn", *, section="s", action_id="a", label="Approve",
                     detail="", style="attention"):
    item = menu_spec.MenuItem(
        label=label, action_id=action_id, detail=detail, style=style,
    )
    return menu_spec.BirdMenu(
        name=name, display=name.title(),
        spec=menu_spec.MenuSpec(sections=(
            menu_spec.MenuSection(id=section, items=(item,)),
        )),
    )


class TestAttentionState:
    def test_only_attention_styled_items_are_kept(self):
        model = host.MenuModel((
            menu_spec.BirdMenu(
                name="huginn", display="Huginn",
                spec=menu_spec.MenuSpec(sections=(
                    menu_spec.MenuSection(id="s", items=(
                        menu_spec.MenuItem(label="Normal", action_id="n", style="normal"),
                        menu_spec.MenuItem(label="Muted", action_id="m", style="muted"),
                        menu_spec.MenuItem(label="Approve", action_id="a", style="attention"),
                    )),
                )),
            ),
        ))
        state = host.attention_state(model)
        assert list(state.values()) == [host.AttentionItem(
            "huginn", "Huginn", model.menus[0].spec.sections[0].items[2],
        )]

    def test_an_unavailable_bird_contributes_nothing(self):
        model = host.MenuModel((
            menu_spec.BirdMenu(name="huginn", display="Huginn", reason="Down."),
        ))
        assert host.attention_state(model) == {}

    def test_a_separator_is_skipped(self):
        model = host.MenuModel((
            menu_spec.BirdMenu(
                name="huginn", display="Huginn",
                spec=menu_spec.MenuSpec(sections=(
                    menu_spec.MenuSection(id="s", items=(
                        menu_spec.MenuItem(separator=True, style="attention"),
                    )),
                )),
            ),
        ))
        assert host.attention_state(model) == {}

    def test_an_item_with_no_action_id_keys_on_its_label(self):
        model = host.MenuModel((_attention_menu(action_id="", label="Needs a login"),))
        state = host.attention_state(model)
        assert ("huginn", "s", "Needs a login") in state


class TestNewlyAttention:
    def test_the_first_read_never_notifies(self):
        """Every pre-existing attention item at startup must not fire at once."""
        current = host.attention_state(host.MenuModel((_attention_menu(),)))
        assert host.newly_attention(None, current) == []

    def test_an_item_present_in_both_reads_does_not_notify_again(self):
        model = host.MenuModel((_attention_menu(),))
        state = host.attention_state(model)
        assert host.newly_attention(state, state) == []

    def test_an_item_that_just_became_attention_notifies_once(self):
        before = host.attention_state(host.MenuModel(()))
        after = host.attention_state(host.MenuModel((_attention_menu(),)))
        fresh = host.newly_attention(before, after)
        assert [entry.display for entry in fresh] == ["Huginn"]

    def test_an_entry_names_the_bird_it_came_from(self):
        """A toast has to act on the item, and only the bird's name addresses it."""
        after = host.attention_state(host.MenuModel((_attention_menu(name="muninn"),)))
        entry = host.newly_attention(host.attention_state(host.MenuModel(())), after)[0]

        assert (entry.bird, entry.display) == ("muninn", "Muninn")
        assert entry.item.action_id == "a"

    def test_a_resolved_item_is_simply_absent_next_time(self):
        """Nothing here fires a 'resolved' toast; the item just stops appearing."""
        before = host.attention_state(host.MenuModel((_attention_menu(),)))
        after = host.attention_state(host.MenuModel(()))
        assert host.newly_attention(before, after) == []

    def test_two_items_that_differ_only_by_section_do_not_collide(self):
        model = host.MenuModel((
            menu_spec.BirdMenu(
                name="huginn", display="Huginn",
                spec=menu_spec.MenuSpec(sections=(
                    menu_spec.MenuSection(id="one", items=(
                        menu_spec.MenuItem(label="Approve", action_id="", style="attention"),
                    )),
                    menu_spec.MenuSection(id="two", items=(
                        menu_spec.MenuItem(label="Approve", action_id="", style="attention"),
                    )),
                )),
            ),
        ))
        state = host.attention_state(model)
        assert len(state) == 2


class TestActivate:
    def test_a_url_item_returns_a_loopback_url(self, tmp_path):
        server = _MenuBird({"sections": [
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
        server = _MenuBird(_menu("Huginn", "Approve"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            item = menu.spec.sections[0].items[0]
            monkeypatch.setattr(
                bird_client, "send_action",
                lambda descriptor, action_id: sent.append((descriptor.name, action_id)),
            )
            assert host.activate(menu, item) is None
            assert sent == [("huginn", "act:0")]
        finally:
            server.close()

    def test_a_refused_action_is_logged_not_raised(self, tmp_path, monkeypatch):
        server = _MenuBird(_menu("Huginn", "Approve"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            item = menu.spec.sections[0].items[0]

            def _refuse(*_args, **_kwargs):
                raise bird_client.BirdRequestError("nope")

            monkeypatch.setattr(bird_client, "send_action", _refuse)
            assert host.activate(menu, item) is None
        finally:
            server.close()

    def test_an_inert_item_does_nothing(self):
        menu = menu_spec.BirdMenu("huginn", "Huginn")
        item = menu_spec.MenuItem(label="Text only", enabled=False)
        assert host.activate(menu, item) is None

    def test_an_unavailable_bird_has_nothing_to_activate(self):
        menu = menu_spec.BirdMenu("huginn", "Huginn", reason="Not running.")
        item = menu_spec.MenuItem(label="Row", action_id="a")
        assert host.activate(menu, item) is None

    def test_a_separator_is_never_activated(self, tmp_path):
        server = _MenuBird(_menu("Huginn", "Row"))
        try:
            _write_descriptor(tmp_path, "huginn", server.port)
            menu = host.build_model(tmp_path).menus[0]
            assert host.activate(menu, menu_spec.MenuItem(separator=True)) is None
        finally:
            server.close()
