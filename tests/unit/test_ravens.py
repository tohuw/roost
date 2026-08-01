"""Descriptor discovery, validation, and liveness tests.

A descriptor is a file another process wrote, so the interesting cases are the
adversarial ones: hostile field types, a name that disagrees with its filename,
control characters aimed at the menu and the log, an endpoint that points off the
raven, a version range that would silently disable everything, a PID that has
been recycled, and a file large enough to matter. Every one of them must produce
an UnavailableRaven with a reason — not an exception, and not a descriptor.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import ravens
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
    """The one path-resolution rule both ravens must follow."""

    def test_explicit_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ravens.STATE_DIR_ENV, str(tmp_path / "elsewhere"))
        assert ravens.state_dir() == tmp_path / "elsewhere"

    def test_posix_honors_xdg_state_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv(ravens.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(ravens, "_IS_WINDOWS", False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        assert ravens.state_dir() == tmp_path / "xdg" / "ravens"

    def test_posix_falls_back_to_local_state(self, monkeypatch):
        monkeypatch.delenv(ravens.STATE_DIR_ENV, raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(ravens, "_IS_WINDOWS", False)
        assert ravens.state_dir() == Path.home() / ".local" / "state" / "ravens"

    def test_blank_xdg_is_treated_as_unset(self, monkeypatch):
        monkeypatch.delenv(ravens.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(ravens, "_IS_WINDOWS", False)
        monkeypatch.setenv("XDG_STATE_HOME", "   ")
        assert ravens.state_dir() == Path.home() / ".local" / "state" / "ravens"

    def test_windows_uses_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.delenv(ravens.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(ravens, "_IS_WINDOWS", True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert ravens.state_dir() == tmp_path / "Local" / "Ravens"

    def test_windows_ignores_xdg(self, monkeypatch, tmp_path):
        """XDG is a POSIX convention; on Windows LOCALAPPDATA is the answer."""
        monkeypatch.delenv(ravens.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(ravens, "_IS_WINDOWS", True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        assert ravens.state_dir() == tmp_path / "Local" / "Ravens"

    def test_windows_falls_back_without_localappdata(self, monkeypatch):
        monkeypatch.delenv(ravens.STATE_DIR_ENV, raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(ravens, "_IS_WINDOWS", True)
        assert ravens.state_dir() == Path.home() / "AppData" / "Local" / "Ravens"


class TestDescriptorPath:
    def test_valid_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ravens.STATE_DIR_ENV, str(tmp_path))
        assert ravens.descriptor_path("muninn") == tmp_path / "muninn.json"

    @pytest.mark.parametrize("name", [
        "", "../escape", "/absolute", "Huginn", "has space", "dot.name",
        "a" * 33, "-leading", "trailing\n",
    ])
    def test_rejects_unsafe_names(self, name):
        """A name becomes a filename, so traversal and case games are refused."""
        with pytest.raises(ValueError):
            ravens.descriptor_path(name)


# ── Happy path ────────────────────────────────────────────────────────────────

class TestValidDescriptor:
    def test_round_trips_through_the_publisher(self, tmp_path):
        document = ravens.DescriptorDocument(
            name="muninn", display="Muninn", port=47101,
            token_path=str(tmp_path / "token"),
            token_header="X-Muninn-Token",
            endpoints={"menu": "/api/menu", "open": "/"},
        )
        path = _write(tmp_path, "muninn", document.to_json())
        raven = ravens.load_raven(path)
        assert isinstance(raven, ravens.AvailableRaven)
        assert raven.descriptor.display == "Muninn"
        assert raven.descriptor.port == 47101
        assert raven.descriptor.token_header == "X-Muninn-Token"
        assert raven.descriptor.endpoint("menu") == "/api/menu"
        assert raven.descriptor.base_url() == "http://127.0.0.1:47101"

    def test_display_defaults_to_name(self, tmp_path):
        payload = _payload()
        del payload["display"]
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.AvailableRaven)
        assert raven.descriptor.display == "huginn"

    def test_optional_api_bounds_default_to_api_version(self, tmp_path):
        payload = _payload()
        del payload["min_api"]
        del payload["max_api"]
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.AvailableRaven)
        assert raven.descriptor.api_range == (1, 1)

    def test_token_path_expands_tilde(self, tmp_path):
        payload = _payload(token_path="~/.local/state/huginn/token")
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.AvailableRaven)
        assert raven.descriptor.token_path == Path.home() / ".local/state/huginn/token"

    def test_endpoints_are_optional(self, tmp_path):
        payload = _payload()
        del payload["endpoints"]
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.AvailableRaven)
        assert raven.descriptor.endpoints == {}


# ── Version compatibility: ranges, not equality (huginn #38) ──────────────────

class TestApiCompatibility:
    """A version bump must never silently disable a raven — it must say so."""

    def test_overlapping_range_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ravens, "API_VERSION", 3)
        monkeypatch.setattr(ravens, "MIN_API_VERSION", 2)
        payload = _payload(api_version=2, min_api=1, max_api=2)
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.AvailableRaven), getattr(raven, "reason", "")

    def test_range_entirely_below_is_refused_with_both_ranges(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ravens, "API_VERSION", 5)
        monkeypatch.setattr(ravens, "MIN_API_VERSION", 4)
        payload = _payload(api_version=1, min_api=1, max_api=2)
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "[1, 2]" in raven.reason
        assert "[4, 5]" in raven.reason

    def test_range_entirely_above_is_refused(self, tmp_path):
        payload = _payload(api_version=50, min_api=50, max_api=60)
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "raven API" in raven.reason

    def test_inverted_range_is_refused(self, tmp_path):
        payload = _payload(min_api=5, max_api=2)
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "inverted" in raven.reason

    def test_absurd_max_api_is_refused(self, tmp_path):
        """Without a ceiling a descriptor stays 'compatible' forever."""
        payload = _payload(max_api=2**62)
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "max_api" in raven.reason

    def test_boolean_api_version_is_refused(self, tmp_path):
        """True is an int subclass and would otherwise validate as version 1."""
        payload = _payload(api_version=True)
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "api_version must be an integer" in raven.reason


# ── Hostile and malformed descriptors ─────────────────────────────────────────

class TestHostileDescriptor:
    def test_not_json(self, tmp_path):
        raven = ravens.load_raven(_write(tmp_path, "huginn", "not json at all"))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "not valid JSON" in raven.reason

    def test_json_array_is_not_a_descriptor(self, tmp_path):
        raven = ravens.load_raven(_write(tmp_path, "huginn", "[1, 2, 3]"))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "JSON object" in raven.reason

    def test_empty_file(self, tmp_path):
        raven = ravens.load_raven(_write(tmp_path, "huginn", ""))
        assert isinstance(raven, ravens.UnavailableRaven)

    def test_oversized_descriptor_is_refused_by_size(self, tmp_path):
        payload = _payload(display="x")
        blob = json.dumps(payload) + " " * (ravens.MAX_DESCRIPTOR_BYTES + 10)
        raven = ravens.load_raven(_write(tmp_path, "huginn", blob))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "larger than" in raven.reason

    def test_name_disagreeing_with_filename_is_refused(self, tmp_path):
        """Otherwise one raven could publish a descriptor impersonating another."""
        payload = _payload(name="muninn")
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "filed as" in raven.reason

    def test_filename_that_is_not_a_slug_is_refused(self, tmp_path):
        path = _write(tmp_path, "Not A Raven", _payload())
        raven = ravens.load_raven(path)
        assert isinstance(raven, ravens.UnavailableRaven)
        assert raven.name == ""
        assert "filename" in raven.reason

    @pytest.mark.parametrize("port", [0, -1, 65536, 99999, "8000", 8000.5, True, None])
    def test_bad_ports(self, tmp_path, port):
        raven = ravens.load_raven(_write(tmp_path, "huginn", _payload(port=port)))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "port" in raven.reason

    @pytest.mark.parametrize("pid", [0, -1, "123", 1.5, True, None])
    def test_bad_pids(self, tmp_path, pid):
        raven = ravens.load_raven(_write(tmp_path, "huginn", _payload(pid=pid)))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "pid" in raven.reason

    @pytest.mark.parametrize("display", [
        "Hu\x00ginn",              # NUL
        "Hu\x1b[31mginn",          # ANSI colour
        "Huginn\r\nX-Evil: 1",     # header/log injection shape
        "Hu‮ginn",            # bidi override
        "Huginn\x07",              # BEL
    ])
    def test_control_characters_in_display_are_refused(self, tmp_path, display):
        """Repairing would hide a malformed file; refusing surfaces it."""
        raven = ravens.load_raven(_write(tmp_path, "huginn", _payload(display=display)))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "control characters" in raven.reason

    def test_unavailable_reason_never_echoes_hostile_content(self, tmp_path):
        hostile = "\x1b[2J\x1b[HFAKE MENU"
        raven = ravens.load_raven(_write(tmp_path, "huginn", _payload(display=hostile)))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "\x1b" not in raven.reason
        assert "FAKE MENU" not in raven.reason
        assert not sanitize.contains_unsafe_text(raven.reason)

    def test_overlong_display_is_refused(self, tmp_path):
        payload = _payload(display="x" * (ravens.MAX_DISPLAY_LENGTH + 1))
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "characters or fewer" in raven.reason

    def test_missing_required_field(self, tmp_path):
        payload = _payload()
        del payload["pid"]
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)

    def test_unreadable_file_is_a_reason_not_a_crash(self, tmp_path):
        missing = tmp_path / "huginn.json"
        raven = ravens.load_raven(missing)
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "could not be read" in raven.reason


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
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "endpoints.menu" in raven.reason

    def test_endpoints_must_be_an_object(self, tmp_path):
        payload = _payload(endpoints=["/api/menu"])
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "endpoints must be an object" in raven.reason

    def test_too_many_endpoints(self, tmp_path):
        payload = _payload(endpoints={
            f"key{index}": "/x" for index in range(ravens.MAX_ENDPOINTS + 1)
        })
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "entries or fewer" in raven.reason

    @pytest.mark.parametrize("key", ["Menu", "1menu", "menu-open", "", "menu path"])
    def test_endpoint_keys_are_slugs(self, tmp_path, key):
        payload = _payload(endpoints={key: "/api/menu"})
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "endpoint names" in raven.reason


class TestTokenFieldValidation:
    def test_relative_token_path_is_refused(self, tmp_path):
        payload = _payload(token_path="state/token")
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "absolute" in raven.reason

    @pytest.mark.parametrize("header", [
        "X-Token: injected", "X-Token\r\nEvil: 1", "X Token", "", 42,
    ])
    def test_token_header_must_be_a_valid_header_name(self, tmp_path, header):
        payload = _payload(token_header=header)
        raven = ravens.load_raven(_write(tmp_path, "huginn", payload))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "token_header" in raven.reason

    def test_token_path_absent_means_no_credential(self, tmp_path):
        raven = ravens.load_raven(_write(tmp_path, "huginn", _payload()))
        assert isinstance(raven, ravens.AvailableRaven)
        assert raven.descriptor.token_path is None


# ── Liveness ──────────────────────────────────────────────────────────────────

class TestLiveness:
    def test_own_pid_is_alive(self):
        assert ravens.pid_is_alive(os.getpid()) is True

    @pytest.mark.parametrize("pid", [0, -1, -99999, True, False, None, "123", 1.5])
    def test_non_positive_and_non_int_pids_are_dead(self, pid):
        """-1 would address every signalable process; 0 our own group."""
        assert ravens.pid_is_alive(pid) is False

    def test_unused_pid_is_dead(self, monkeypatch):
        def _boom(_pid, _sig):
            raise ProcessLookupError

        monkeypatch.setattr(ravens, "_IS_WINDOWS", False)
        monkeypatch.setattr(ravens.os, "kill", _boom)
        assert ravens.pid_is_alive(424242) is False

    def test_permission_error_counts_as_alive(self, monkeypatch):
        def _denied(_pid, _sig):
            raise PermissionError

        monkeypatch.setattr(ravens, "_IS_WINDOWS", False)
        monkeypatch.setattr(ravens.os, "kill", _denied)
        assert ravens.pid_is_alive(1) is True

    def test_recycled_pid_is_rejected_by_start_time(self, monkeypatch):
        """A live PID whose start time disagrees is a different process."""
        monkeypatch.setattr(ravens, "_IS_WINDOWS", False)
        monkeypatch.setattr(ravens.os, "kill", lambda _pid, _sig: None)
        monkeypatch.setattr(ravens, "_posix_process_start_time", lambda _pid: 5_000.0)
        assert ravens.pid_is_alive(1234, started=9_000.0) is False
        assert ravens.pid_is_alive(1234, started=5_000.5) is True

    def test_unknown_start_time_does_not_contradict_liveness(self, monkeypatch):
        monkeypatch.setattr(ravens, "_IS_WINDOWS", False)
        monkeypatch.setattr(ravens.os, "kill", lambda _pid, _sig: None)
        monkeypatch.setattr(ravens, "_posix_process_start_time", lambda _pid: None)
        assert ravens.pid_is_alive(1234, started=9_000.0) is True

    def test_stale_descriptor_renders_as_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ravens, "pid_is_alive", lambda *_args, **_kw: False)
        raven = ravens.load_raven(_write(tmp_path, "huginn", _payload()))
        assert isinstance(raven, ravens.UnavailableRaven)
        assert "Not running" in raven.reason
        assert raven.display == "Huginn"


# ── Discovery ─────────────────────────────────────────────────────────────────

class TestDiscovery:
    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        assert ravens.discover(tmp_path / "nope") == []

    def test_non_json_files_are_ignored(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "README.md").write_text("not a descriptor", encoding="utf-8")
        _write(tmp_path, "huginn", _payload())
        found = ravens.discover(tmp_path)
        assert [raven.name for raven in found] == ["huginn"]

    def test_available_sort_before_unavailable(self, tmp_path):
        _write(tmp_path, "huginn", _payload())
        _write(tmp_path, "muninn", "broken")
        found = ravens.discover(tmp_path)
        assert [raven.name for raven in found] == ["huginn", "muninn"]
        assert isinstance(found[0], ravens.AvailableRaven)
        assert isinstance(found[1], ravens.UnavailableRaven)

    def test_host_priority_orders_available_ravens(self, tmp_path):
        _write(tmp_path, "aardvark", _payload(name="aardvark", host_priority=0))
        _write(tmp_path, "huginn", _payload(host_priority=100))
        found = ravens.available(ravens.discover(tmp_path))
        assert [raven.name for raven in found] == ["huginn", "aardvark"]

    def test_unavailable_ravens_are_still_reported(self, tmp_path):
        """A vanished section looks like an uninstalled raven; keep it visible."""
        _write(tmp_path, "muninn", "}{")
        found = ravens.discover(tmp_path)
        assert len(found) == 1
        assert isinstance(found[0], ravens.UnavailableRaven)
        assert found[0].reason

    def test_available_filters_to_usable_ravens(self, tmp_path):
        _write(tmp_path, "huginn", _payload())
        _write(tmp_path, "muninn", "broken")
        assert [raven.name for raven in ravens.available(ravens.discover(tmp_path))] == ["huginn"]

    def test_discovery_uses_state_dir_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ravens.STATE_DIR_ENV, str(tmp_path))
        _write(tmp_path, "huginn", _payload())
        assert [raven.name for raven in ravens.discover()] == ["huginn"]


class TestDescriptorDocument:
    def test_publisher_output_parses(self, tmp_path):
        document = ravens.DescriptorDocument(name="huginn", display="Huginn", port=47100)
        parsed = ravens.parse_descriptor(
            document.to_json(), tmp_path / "huginn.json", expected_name="huginn"
        )
        assert parsed.name == "huginn"
        assert parsed.api_range == (ravens.MIN_API_VERSION, ravens.API_VERSION)

    def test_optional_fields_are_omitted_when_unset(self):
        document = ravens.DescriptorDocument(name="huginn", display="Huginn", port=47100)
        payload = document.to_dict()
        assert "token_path" not in payload
        assert "token_header" not in payload

    def test_document_records_this_process_by_default(self):
        document = ravens.DescriptorDocument(name="huginn", display="Huginn", port=1)
        assert document.pid == os.getpid()
