"""Descriptor discovery, validation, and liveness tests.

A descriptor is a file another process wrote, so the interesting cases are the
adversarial ones: hostile field types, a name that disagrees with its filename,
control characters aimed at the menu and the log, an endpoint that points off the
bird, a version range that would silently disable everything, a PID that has
been recycled, and a file large enough to matter. Every one of them must produce
an UnavailableBird with a reason — not an exception, and not a descriptor.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import birds
from roost import sanitize


def _payload(**overrides) -> dict:
    payload = {
        "api_version": 1,
        "min_api": 1,
        "max_api": 1,
        "name": "huginn",
        "display": "Huginn",
        "pid": os.getpid(),
        "port": 47100,
        "endpoints": {"menu": "/api/menu", "open": "/"},
    }
    payload.update(overrides)
    return payload


def _write(directory: Path, name: str, payload) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


# ── Path resolution ───────────────────────────────────────────────────────────

class TestStateDir:
    """The one path-resolution rule both birds must follow."""

    def test_explicit_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(birds.STATE_DIR_ENV, str(tmp_path / "elsewhere"))
        assert birds.state_dir() == tmp_path / "elsewhere"

    def test_posix_honors_xdg_state_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv(birds.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(birds, "_IS_WINDOWS", False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        assert birds.state_dir() == tmp_path / "xdg" / "birds"

    def test_posix_falls_back_to_local_state(self, monkeypatch):
        monkeypatch.delenv(birds.STATE_DIR_ENV, raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(birds, "_IS_WINDOWS", False)
        assert birds.state_dir() == Path.home() / ".local" / "state" / "birds"

    def test_blank_xdg_is_treated_as_unset(self, monkeypatch):
        monkeypatch.delenv(birds.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(birds, "_IS_WINDOWS", False)
        monkeypatch.setenv("XDG_STATE_HOME", "   ")
        assert birds.state_dir() == Path.home() / ".local" / "state" / "birds"

    def test_windows_uses_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.delenv(birds.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(birds, "_IS_WINDOWS", True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert birds.state_dir() == tmp_path / "Local" / "Birds"

    def test_windows_ignores_xdg(self, monkeypatch, tmp_path):
        """XDG is a POSIX convention; on Windows LOCALAPPDATA is the answer."""
        monkeypatch.delenv(birds.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(birds, "_IS_WINDOWS", True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        assert birds.state_dir() == tmp_path / "Local" / "Birds"

    def test_windows_falls_back_without_localappdata(self, monkeypatch):
        monkeypatch.delenv(birds.STATE_DIR_ENV, raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(birds, "_IS_WINDOWS", True)
        assert birds.state_dir() == Path.home() / "AppData" / "Local" / "Birds"


class TestDescriptorPath:
    def test_valid_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv(birds.STATE_DIR_ENV, str(tmp_path))
        assert birds.descriptor_path("muninn") == tmp_path / "muninn.json"

    @pytest.mark.parametrize("name", [
        "", "../escape", "/absolute", "Huginn", "has space", "dot.name",
        "a" * 33, "-leading", "trailing\n",
    ])
    def test_rejects_unsafe_names(self, name):
        """A name becomes a filename, so traversal and case games are refused."""
        with pytest.raises(ValueError):
            birds.descriptor_path(name)


# ── Happy path ────────────────────────────────────────────────────────────────

class TestValidDescriptor:
    def test_round_trips_through_the_publisher(self, tmp_path):
        document = birds.DescriptorDocument(
            name="muninn", display="Muninn", port=47101,
            token_path=str(tmp_path / "token"),
            token_header="X-Muninn-Token",
            endpoints={"menu": "/api/menu", "open": "/"},
        )
        path = _write(tmp_path, "muninn", document.to_json())
        bird = birds.load_bird(path)
        assert isinstance(bird, birds.AvailableBird)
        assert bird.descriptor.display == "Muninn"
        assert bird.descriptor.port == 47101
        assert bird.descriptor.token_header == "X-Muninn-Token"
        assert bird.descriptor.endpoint("menu") == "/api/menu"
        assert bird.descriptor.base_url() == "http://127.0.0.1:47101"

    def test_display_defaults_to_name(self, tmp_path):
        payload = _payload()
        del payload["display"]
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.AvailableBird)
        assert bird.descriptor.display == "huginn"

    def test_optional_api_bounds_default_to_api_version(self, tmp_path):
        payload = _payload()
        del payload["min_api"]
        del payload["max_api"]
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.AvailableBird)
        assert bird.descriptor.api_range == (1, 1)

    def test_token_path_expands_tilde(self, tmp_path):
        payload = _payload(token_path="~/.local/state/huginn/token")
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.AvailableBird)
        assert bird.descriptor.token_path == Path.home() / ".local/state/huginn/token"

    def test_endpoints_are_optional(self, tmp_path):
        payload = _payload()
        del payload["endpoints"]
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.AvailableBird)
        assert bird.descriptor.endpoints == {}

    @pytest.mark.parametrize("unknown", [0, 0.0, -1, -1.5])
    def test_a_non_positive_started_means_unknown_not_dead(self, tmp_path, unknown):
        """Zero and negative are how a bird records "I could not tell".

        corvidae's descriptor_is_live says so outright, so a bird built on it
        writes exactly that. Comparing against the value would fail for every
        live process rather than only recycled PIDs, and a negative used to be
        refused as a malformed descriptor — hiding a running bird behind a
        parse error. Both break §8: a missing cross-check must never turn a live
        bird into a dead one.
        """
        payload = _payload(pid=os.getpid(), started=unknown)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.AvailableBird), getattr(bird, "reason", "")
        assert bird.descriptor.started is None

    def test_an_absurd_started_is_still_refused(self, tmp_path):
        """Unknown is one thing; a number that cannot be a time is another."""
        payload = _payload(started=2**60)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "started" in bird.reason


# ── Version compatibility: ranges, not equality (huginn #38) ──────────────────

class TestApiCompatibility:
    """A version bump must never silently disable a bird — it must say so."""

    def test_overlapping_range_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(birds, "API_VERSION", 3)
        monkeypatch.setattr(birds, "MIN_API_VERSION", 2)
        payload = _payload(api_version=2, min_api=1, max_api=2)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.AvailableBird), getattr(bird, "reason", "")

    def test_range_entirely_below_is_refused_with_both_ranges(self, tmp_path, monkeypatch):
        monkeypatch.setattr(birds, "API_VERSION", 5)
        monkeypatch.setattr(birds, "MIN_API_VERSION", 4)
        payload = _payload(api_version=1, min_api=1, max_api=2)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "[1, 2]" in bird.reason
        assert "[4, 5]" in bird.reason

    def test_range_entirely_above_is_refused(self, tmp_path):
        payload = _payload(api_version=50, min_api=50, max_api=60)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "bird API" in bird.reason

    def test_inverted_range_is_refused(self, tmp_path):
        payload = _payload(min_api=5, max_api=2)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "inverted" in bird.reason

    def test_absurd_max_api_is_refused(self, tmp_path):
        """Without a ceiling a descriptor stays 'compatible' forever."""
        payload = _payload(max_api=2**62)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "max_api" in bird.reason

    def test_boolean_api_version_is_refused(self, tmp_path):
        """True is an int subclass and would otherwise validate as version 1."""
        payload = _payload(api_version=True)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "api_version must be an integer" in bird.reason


# ── Hostile and malformed descriptors ─────────────────────────────────────────

class TestHostileDescriptor:
    def test_not_json(self, tmp_path):
        bird = birds.load_bird(_write(tmp_path, "huginn", "not json at all"))
        assert isinstance(bird, birds.UnavailableBird)
        assert "not valid JSON" in bird.reason

    def test_json_array_is_not_a_descriptor(self, tmp_path):
        bird = birds.load_bird(_write(tmp_path, "huginn", "[1, 2, 3]"))
        assert isinstance(bird, birds.UnavailableBird)
        assert "JSON object" in bird.reason

    def test_empty_file(self, tmp_path):
        bird = birds.load_bird(_write(tmp_path, "huginn", ""))
        assert isinstance(bird, birds.UnavailableBird)

    def test_oversized_descriptor_is_refused_by_size(self, tmp_path):
        payload = _payload(display="x")
        blob = json.dumps(payload) + " " * (birds.MAX_DESCRIPTOR_BYTES + 10)
        bird = birds.load_bird(_write(tmp_path, "huginn", blob))
        assert isinstance(bird, birds.UnavailableBird)
        assert "larger than" in bird.reason

    def test_name_disagreeing_with_filename_is_refused(self, tmp_path):
        """Otherwise one bird could publish a descriptor impersonating another."""
        payload = _payload(name="muninn")
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "filed as" in bird.reason

    def test_filename_that_is_not_a_slug_is_refused(self, tmp_path):
        path = _write(tmp_path, "Not A Bird", _payload())
        bird = birds.load_bird(path)
        assert isinstance(bird, birds.UnavailableBird)
        assert bird.name == ""
        assert "filename" in bird.reason

    @pytest.mark.parametrize("port", [0, -1, 65536, 99999, "8000", 8000.5, True, None])
    def test_bad_ports(self, tmp_path, port):
        bird = birds.load_bird(_write(tmp_path, "huginn", _payload(port=port)))
        assert isinstance(bird, birds.UnavailableBird)
        assert "port" in bird.reason

    @pytest.mark.parametrize("pid", [0, -1, "123", 1.5, True, None])
    def test_bad_pids(self, tmp_path, pid):
        bird = birds.load_bird(_write(tmp_path, "huginn", _payload(pid=pid)))
        assert isinstance(bird, birds.UnavailableBird)
        assert "pid" in bird.reason

    @pytest.mark.parametrize("display", [
        "Hu\x00ginn",              # NUL
        "Hu\x1b[31mginn",          # ANSI colour
        "Huginn\r\nX-Evil: 1",     # header/log injection shape
        "Hu‮ginn",            # bidi override
        "Huginn\x07",              # BEL
    ])
    def test_control_characters_in_display_are_refused(self, tmp_path, display):
        """Repairing would hide a malformed file; refusing surfaces it."""
        bird = birds.load_bird(_write(tmp_path, "huginn", _payload(display=display)))
        assert isinstance(bird, birds.UnavailableBird)
        assert "control characters" in bird.reason

    def test_unavailable_reason_never_echoes_hostile_content(self, tmp_path):
        hostile = "\x1b[2J\x1b[HFAKE MENU"
        bird = birds.load_bird(_write(tmp_path, "huginn", _payload(display=hostile)))
        assert isinstance(bird, birds.UnavailableBird)
        assert "\x1b" not in bird.reason
        assert "FAKE MENU" not in bird.reason
        assert not sanitize.contains_unsafe_text(bird.reason)

    def test_overlong_display_is_refused(self, tmp_path):
        payload = _payload(display="x" * (birds.MAX_DISPLAY_LENGTH + 1))
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "characters or fewer" in bird.reason

    def test_missing_required_field(self, tmp_path):
        payload = _payload()
        del payload["pid"]
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)

    def test_unreadable_file_is_a_reason_not_a_crash(self, tmp_path):
        missing = tmp_path / "huginn.json"
        bird = birds.load_bird(missing)
        assert isinstance(bird, birds.UnavailableBird)
        assert "could not be read" in bird.reason


class TestEndpointValidation:
    @pytest.mark.parametrize("value", [
        "http://evil.example/api/menu",   # absolute URL
        "//evil.example/api/menu",        # scheme-relative
        "/\\evil.example/api/menu",       # backslash variant some parsers accept
        "api/menu",                       # not rooted
        "/api/../../../etc/passwd",       # traversal
        "/api/menu?token=x",              # query
        "/api/menu#frag",                 # fragment
        "",                               # empty
        None,                             # wrong type
        123,                              # wrong type
    ])
    def test_endpoint_must_be_a_local_rooted_path(self, tmp_path, value):
        payload = _payload(endpoints={"menu": value})
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "endpoints.menu" in bird.reason

    def test_endpoints_must_be_an_object(self, tmp_path):
        payload = _payload(endpoints=["/api/menu"])
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "endpoints must be an object" in bird.reason

    def test_too_many_endpoints(self, tmp_path):
        payload = _payload(endpoints={
            f"key{index}": "/x" for index in range(birds.MAX_ENDPOINTS + 1)
        })
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "entries or fewer" in bird.reason

    @pytest.mark.parametrize("key", ["Menu", "1menu", "menu-open", "", "menu path"])
    def test_endpoint_keys_are_slugs(self, tmp_path, key):
        payload = _payload(endpoints={key: "/api/menu"})
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "endpoint names" in bird.reason


class TestTokenFieldValidation:
    def test_relative_token_path_is_refused(self, tmp_path):
        payload = _payload(token_path="state/token")
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "absolute" in bird.reason

    @pytest.mark.parametrize("header", [
        "X-Token: injected", "X-Token\r\nEvil: 1", "X Token", "", 42,
    ])
    def test_token_header_must_be_a_valid_header_name(self, tmp_path, header):
        payload = _payload(token_header=header)
        bird = birds.load_bird(_write(tmp_path, "huginn", payload))
        assert isinstance(bird, birds.UnavailableBird)
        assert "token_header" in bird.reason

    def test_token_path_absent_means_no_credential(self, tmp_path):
        bird = birds.load_bird(_write(tmp_path, "huginn", _payload()))
        assert isinstance(bird, birds.AvailableBird)
        assert bird.descriptor.token_path is None


# ── Liveness ──────────────────────────────────────────────────────────────────

class TestLiveness:
    def test_own_pid_is_alive(self):
        assert birds.pid_is_alive(os.getpid()) is True

    @pytest.mark.parametrize("pid", [0, -1, -99999, True, False, None, "123", 1.5])
    def test_non_positive_and_non_int_pids_are_dead(self, pid):
        """-1 would address every signalable process; 0 our own group."""
        assert birds.pid_is_alive(pid) is False

    def test_unused_pid_is_dead(self, monkeypatch):
        def _boom(_pid, _sig):
            raise ProcessLookupError

        monkeypatch.setattr(birds, "_IS_WINDOWS", False)
        monkeypatch.setattr(birds.os, "kill", _boom)
        assert birds.pid_is_alive(424242) is False

    def test_permission_error_counts_as_alive(self, monkeypatch):
        def _denied(_pid, _sig):
            raise PermissionError

        monkeypatch.setattr(birds, "_IS_WINDOWS", False)
        monkeypatch.setattr(birds.os, "kill", _denied)
        assert birds.pid_is_alive(1) is True

    def test_recycled_pid_is_rejected_by_start_time(self, monkeypatch):
        """A live PID whose start time disagrees is a different process."""
        monkeypatch.setattr(birds, "_IS_WINDOWS", False)
        monkeypatch.setattr(birds.os, "kill", lambda _pid, _sig: None)
        monkeypatch.setattr(birds, "process_start_time", lambda _pid: 5_000.0)
        assert birds.pid_is_alive(1234, started=9_000.0) is False
        assert birds.pid_is_alive(1234, started=5_000.5) is True

    def test_unknown_start_time_does_not_contradict_liveness(self, monkeypatch):
        monkeypatch.setattr(birds, "_IS_WINDOWS", False)
        monkeypatch.setattr(birds.os, "kill", lambda _pid, _sig: None)
        monkeypatch.setattr(birds, "process_start_time", lambda _pid: None)
        assert birds.pid_is_alive(1234, started=9_000.0) is True

    def test_stale_descriptor_renders_as_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(birds, "pid_is_alive", lambda *_args, **_kw: False)
        bird = birds.load_bird(_write(tmp_path, "huginn", _payload()))
        assert isinstance(bird, birds.UnavailableBird)
        assert "Not running" in bird.reason
        assert bird.display == "Huginn"


# ── Discovery ─────────────────────────────────────────────────────────────────

class TestDiscovery:
    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        assert birds.discover(tmp_path / "nope") == []

    def test_non_json_files_are_ignored(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "README.md").write_text("not a descriptor", encoding="utf-8")
        _write(tmp_path, "huginn", _payload())
        found = birds.discover(tmp_path)
        assert [bird.name for bird in found] == ["huginn"]

    def test_available_sort_before_unavailable(self, tmp_path):
        _write(tmp_path, "huginn", _payload())
        _write(tmp_path, "muninn", "broken")
        found = birds.discover(tmp_path)
        assert [bird.name for bird in found] == ["huginn", "muninn"]
        assert isinstance(found[0], birds.AvailableBird)
        assert isinstance(found[1], birds.UnavailableBird)

    def test_host_priority_orders_available_birds(self, tmp_path):
        _write(tmp_path, "aardvark", _payload(name="aardvark", host_priority=0))
        _write(tmp_path, "huginn", _payload(host_priority=100))
        found = birds.available(birds.discover(tmp_path))
        assert [bird.name for bird in found] == ["huginn", "aardvark"]

    def test_unavailable_birds_are_still_reported(self, tmp_path):
        """A vanished section looks like an uninstalled bird; keep it visible."""
        _write(tmp_path, "muninn", "}{")
        found = birds.discover(tmp_path)
        assert len(found) == 1
        assert isinstance(found[0], birds.UnavailableBird)
        assert found[0].reason

    def test_available_filters_to_usable_birds(self, tmp_path):
        _write(tmp_path, "huginn", _payload())
        _write(tmp_path, "muninn", "broken")
        assert [bird.name for bird in birds.available(birds.discover(tmp_path))] == ["huginn"]

    def test_discovery_uses_state_dir_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv(birds.STATE_DIR_ENV, str(tmp_path))
        _write(tmp_path, "huginn", _payload())
        assert [bird.name for bird in birds.discover()] == ["huginn"]


class TestLegacyDirectory:
    """Huginn and Muninn still publish to the pre-rename directory.

    They resolve it through ``corvidae``, not through Roost, so they keep writing
    to ``ravens`` until that package is next released. Every assertion here is
    load-bearing: weaken one and two live daemons drop out of the menu with
    nothing on screen to say why.
    """

    @staticmethod
    def _both(monkeypatch, tmp_path):
        """Point Roost at a scratch pair of current and legacy directories."""
        current, legacy = tmp_path / "birds", tmp_path / "ravens"
        current.mkdir()
        legacy.mkdir()
        monkeypatch.delenv(birds.STATE_DIR_ENV, raising=False)
        monkeypatch.delenv(birds.LEGACY_STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(birds, "state_dir", lambda: current)
        monkeypatch.setattr(birds, "legacy_state_dir", lambda: legacy)
        return current, legacy

    def test_a_bird_in_the_legacy_directory_is_found(self, tmp_path, monkeypatch):
        _, legacy = self._both(monkeypatch, tmp_path)
        _write(legacy, "huginn", _payload())
        assert [bird.name for bird in birds.discover()] == ["huginn"]

    def test_both_directories_are_merged(self, tmp_path, monkeypatch):
        current, legacy = self._both(monkeypatch, tmp_path)
        _write(legacy, "muninn", _payload(name="muninn", display="Muninn"))
        _write(current, "plexavator", _payload(name="plexavator", display="Plexavator"))
        assert sorted(bird.name for bird in birds.discover()) == ["muninn", "plexavator"]

    def test_the_current_directory_wins_a_duplicate_name(self, tmp_path, monkeypatch):
        """A bird that has migrated may have left a stale descriptor behind.

        Preferring the old copy would advertise a dead port for a live process,
        which is worse than reading either directory alone.
        """
        current, legacy = self._both(monkeypatch, tmp_path)
        _write(legacy, "huginn", _payload(port=1111))
        _write(current, "huginn", _payload(port=2222))
        found = birds.available(birds.discover())
        assert [bird.descriptor.port for bird in found] == [2222]

    def test_an_explicit_override_suppresses_the_legacy_read(self, monkeypatch, tmp_path):
        """An override names *the* directory. Quietly reading a second one behind
        the user's back would defeat the point, and would break a test harness
        pointed at a scratch directory."""
        monkeypatch.setenv(birds.STATE_DIR_ENV, str(tmp_path))
        assert birds.legacy_state_dir() is None

    def test_the_former_env_name_is_still_honored(self, monkeypatch, tmp_path):
        monkeypatch.delenv(birds.STATE_DIR_ENV, raising=False)
        monkeypatch.setenv(birds.LEGACY_STATE_DIR_ENV, str(tmp_path / "old"))
        assert birds.state_dir() == tmp_path / "old"

    def test_the_current_env_name_wins_over_the_former(self, monkeypatch, tmp_path):
        monkeypatch.setenv(birds.STATE_DIR_ENV, str(tmp_path / "new"))
        monkeypatch.setenv(birds.LEGACY_STATE_DIR_ENV, str(tmp_path / "old"))
        assert birds.state_dir() == tmp_path / "new"


class TestDescriptorDocument:
    def test_publisher_output_parses(self, tmp_path):
        document = birds.DescriptorDocument(name="huginn", display="Huginn", port=47100)
        parsed = birds.parse_descriptor(
            document.to_json(), tmp_path / "huginn.json", expected_name="huginn"
        )
        assert parsed.name == "huginn"
        assert parsed.api_range == (birds.MIN_API_VERSION, birds.API_VERSION)

    def test_optional_fields_are_omitted_when_unset(self):
        document = birds.DescriptorDocument(name="huginn", display="Huginn", port=47100)
        payload = document.to_dict()
        assert "token_path" not in payload
        assert "token_header" not in payload

    def test_document_records_this_process_by_default(self):
        document = birds.DescriptorDocument(name="huginn", display="Huginn", port=1)
        assert document.pid == os.getpid()
