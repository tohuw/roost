"""Per-bird token isolation and bounded-request tests.

These run against real loopback HTTP servers, because the properties being pinned
are wire properties: which header a token rides in, which port it reaches, what
happens when a bird hangs or floods, and — the hard rule from the shared-menubar
proposal — that one bird's credential never reaches another bird.
"""

import http.server
import json
import socketserver
import sys
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import bird_client
from roost import birds


class _Recorder(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# serve_forever's default 0.5s poll interval is also how long shutdown() blocks,
# which would add half a second to every test in this file.
_POLL_INTERVAL = 0.01


class _Bird:
    """A stand-in bird that records exactly what the client sent it."""

    def __init__(self, *, body=None, status=200, delay=0.0,
                 content_length=None, raw_body=None, location=None):
        self.requests = []
        self._body = {"sections": []} if body is None else body
        self._status = status
        self._delay = delay
        self._content_length = content_length
        self._raw_body = raw_body
        self._location = location
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self._respond(None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or "0")
                self._respond(self.rfile.read(length) if length else None)

            def _respond(self, body):
                recorder.requests.append({
                    "path": self.path,
                    "method": self.command,
                    "headers": dict(self.headers.items()),
                    "body": body,
                })
                if recorder._delay:
                    time.sleep(recorder._delay)
                payload = (
                    recorder._raw_body
                    if recorder._raw_body is not None
                    else json.dumps(recorder._body).encode("utf-8")
                )
                self.send_response(recorder._status)
                self.send_header("Content-Type", "application/json")
                if recorder._location:
                    self.send_header("Location", recorder._location)
                declared = (
                    recorder._content_length
                    if recorder._content_length is not None
                    else len(payload)
                )
                self.send_header("Content-Length", str(declared))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass

        self._server = _Recorder(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": _POLL_INTERVAL},
            daemon=True,
        ).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def header(self, name):
        for key, value in self.requests[-1]["headers"].items():
            if key.lower() == name.lower():
                return value
        return None


def _descriptor(port, tmp_path=None, **overrides):
    values = {
        "name": "huginn",
        "display": "Huginn",
        "api_version": 1,
        "min_api": 1,
        "max_api": 1,
        "pid": 1,
        "port": port,
        "token_path": None,
        "token_header": "",
        "endpoints": {},
        "host_priority": 0,
        "started": None,
        "path": (tmp_path or Path("/tmp")) / "huginn.json",
    }
    values.update(overrides)
    return birds.BirdDescriptor(**values)


@pytest.fixture
def bird():
    server = _Bird()
    try:
        yield server
    finally:
        server.close()


# ── Token isolation: the hard rule ────────────────────────────────────────────

class TestTokenIsolation:
    def test_token_is_sent_in_the_declared_header(self, bird, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("s3cret-value", encoding="utf-8")
        descriptor = _descriptor(
            bird.port, tmp_path,
            token_path=token_file, token_header="X-Huginn-Token",
        )
        bird_client.fetch_menu(descriptor)
        assert bird.header("X-Huginn-Token") == "s3cret-value"

    def test_default_header_is_derived_per_bird(self, bird, tmp_path):
        """No shared well-known header name: that shape invites credential mixing."""
        token_file = tmp_path / "token"
        token_file.write_text("abc", encoding="utf-8")
        descriptor = _descriptor(
            bird.port, tmp_path, name="muninn", token_path=token_file,
        )
        bird_client.fetch_menu(descriptor)
        assert bird.header("X-Muninn-Token") == "abc"
        assert bird.header("X-Huginn-Token") is None

    def test_one_birds_token_never_reaches_another(self, tmp_path):
        """The core invariant from the proposal, tested against two real servers."""
        first, second = _Bird(), _Bird()
        try:
            huginn_token = tmp_path / "huginn-token"
            huginn_token.write_text("huginn-secret", encoding="utf-8")
            muninn_token = tmp_path / "muninn-token"
            muninn_token.write_text("muninn-secret", encoding="utf-8")

            bird_client.fetch_menu(_descriptor(
                first.port, tmp_path, name="huginn",
                token_path=huginn_token, token_header="X-Huginn-Token",
            ))
            bird_client.fetch_menu(_descriptor(
                second.port, tmp_path, name="muninn",
                token_path=muninn_token, token_header="X-Muninn-Token",
            ))

            first_headers = json.dumps(first.requests[-1]["headers"])
            second_headers = json.dumps(second.requests[-1]["headers"])
            assert "huginn-secret" in first_headers
            assert "huginn-secret" not in second_headers
            assert "muninn-secret" in second_headers
            assert "muninn-secret" not in first_headers
        finally:
            first.close()
            second.close()

    def test_no_token_path_means_no_credential(self, bird, tmp_path):
        """Roost never mints a credential on a bird's behalf."""
        bird_client.fetch_menu(_descriptor(bird.port, tmp_path))
        headers = json.dumps(bird.requests[-1]["headers"]).lower()
        assert "token" not in headers
        assert "authorization" not in headers

    def test_token_is_reread_every_call_not_cached(self, bird, tmp_path):
        """A bird rotates its token on restart; a cached one would look broken."""
        token_file = tmp_path / "token"
        token_file.write_text("first", encoding="utf-8")
        descriptor = _descriptor(
            bird.port, tmp_path, token_path=token_file, token_header="X-T",
        )
        bird_client.fetch_menu(descriptor)
        assert bird.header("X-T") == "first"
        token_file.write_text("second", encoding="utf-8")
        bird_client.fetch_menu(descriptor)
        assert bird.header("X-T") == "second"

    def test_no_ambient_headers_are_sent(self, bird, tmp_path):
        """The outbound header set is built from scratch, not from anything inbound."""
        bird_client.fetch_menu(_descriptor(bird.port, tmp_path))
        names = {key.lower() for key in bird.requests[-1]["headers"]}
        for forbidden in ("cookie", "origin", "referer", "authorization"):
            assert forbidden not in names


class TestReadToken:
    def test_missing_file_is_not_fatal(self, tmp_path):
        descriptor = _descriptor(1, tmp_path, token_path=tmp_path / "absent")
        assert bird_client.read_token(descriptor) is None

    def test_no_token_path(self, tmp_path):
        assert bird_client.read_token(_descriptor(1, tmp_path)) is None

    def test_whitespace_is_stripped(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("  value\n", encoding="utf-8")
        descriptor = _descriptor(1, tmp_path, token_path=token_file)
        assert bird_client.read_token(descriptor) == "value"

    def test_empty_token_is_none(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("   \n", encoding="utf-8")
        descriptor = _descriptor(1, tmp_path, token_path=token_file)
        assert bird_client.read_token(descriptor) is None

    def test_oversized_token_is_refused(self, tmp_path):
        """token_path is descriptor-controlled, so the read must be bounded."""
        token_file = tmp_path / "token"
        token_file.write_text("z" * (birds.MAX_TOKEN_BYTES + 10), encoding="utf-8")
        descriptor = _descriptor(1, tmp_path, token_path=token_file)
        assert bird_client.read_token(descriptor) is None

    @pytest.mark.parametrize("content", [
        "abc\r\nX-Evil: 1", "abc\x00def", "abc\x1b[31m",
    ])
    def test_token_with_injection_characters_is_refused(self, tmp_path, content):
        """A CR/LF in a token would inject headers into our own request."""
        token_file = tmp_path / "token"
        token_file.write_bytes(content.encode("utf-8"))
        descriptor = _descriptor(1, tmp_path, token_path=token_file)
        assert bird_client.read_token(descriptor) is None

    def test_non_utf8_token_is_refused(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_bytes(b"\xff\xfe\xfd")
        descriptor = _descriptor(1, tmp_path, token_path=token_file)
        assert bird_client.read_token(descriptor) is None


class TestDefaultTokenHeader:
    @pytest.mark.parametrize("name,expected", [
        ("huginn", "X-Huginn-Token"),
        ("muninn", "X-Muninn-Token"),
        ("my-bird", "X-MyBird-Token"),
        ("", "X-Bird-Token"),
    ])
    def test_names(self, name, expected):
        assert bird_client.default_token_header(name) == expected


# ── Boundedness ───────────────────────────────────────────────────────────────

class TestBoundedRequests:
    def test_a_hanging_bird_times_out(self, tmp_path):
        """A hung bird must not freeze the menu-build thread."""
        server = _Bird(delay=5.0)
        try:
            descriptor = _descriptor(server.port, tmp_path)
            started = time.monotonic()
            with pytest.raises(bird_client.BirdRequestError) as exc:
                bird_client.fetch_menu(descriptor, timeout=0.3)
            assert time.monotonic() - started < 3.0
            assert "in time" in exc.value.reason or "answering" in exc.value.reason
        finally:
            server.close()

    def test_an_unreachable_port_is_a_reason(self, tmp_path):
        server = _Bird()
        port = server.port
        server.close()
        with pytest.raises(bird_client.BirdRequestError) as exc:
            bird_client.fetch_menu(_descriptor(port, tmp_path), timeout=1.0)
        assert "not answering" in exc.value.reason

    def test_oversized_response_is_refused_by_declared_length(self, tmp_path):
        server = _Bird(content_length=bird_client.MAX_RESPONSE_BYTES + 1)
        try:
            with pytest.raises(bird_client.BirdRequestError) as exc:
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
            assert "too large" in exc.value.reason
        finally:
            server.close()

    def test_oversized_response_is_refused_on_the_read(self, tmp_path):
        """The cap holds even when Content-Length understates the body."""
        blob = json.dumps({"sections": [], "pad": "z" * (bird_client.MAX_RESPONSE_BYTES + 1024)})
        server = _Bird(raw_body=blob.encode("utf-8"), content_length=10)
        try:
            with pytest.raises(bird_client.BirdRequestError):
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
        finally:
            server.close()

    def test_an_oversized_error_body_is_not_read_whole(self, tmp_path):
        """The one read in this module the cap did not cover.

        Every read on the success path is bounded, and the error path called a
        bare ``exc.read()`` -- which reads to EOF. A bird answering 500 with an
        enormous body would therefore be pulled into the tray's memory in full,
        by the error handler rather than the parser. The descriptor directory is
        writable by anything running as this user, so a bird behaving badly is
        inside the threat model rather than outside it.

        Asserted on how much was read rather than on elapsed time, which would
        be a machine-speed test dressed up as a correctness one.
        """
        oversized = b"x" * (bird_client.MAX_RESPONSE_BYTES * 4)
        read_sizes: list[int | None] = []

        class _HugeError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://127.0.0.1/x", 500, "boom", {}, None)
                self._left = oversized

            def read(self, amt=None):
                read_sizes.append(amt)
                if amt is None:          # the unbounded call this test forbids
                    chunk, self._left = self._left, b""
                    return chunk
                chunk, self._left = self._left[:amt], self._left[amt:]
                return chunk

        with patch.object(bird_client._OPENER, "open", side_effect=_HugeError()):
            with pytest.raises(bird_client.BirdRequestError):
                bird_client.fetch_menu(_descriptor(47100, tmp_path))

        assert read_sizes, "the error body must still be drained"
        assert None not in read_sizes, "drained with an unbounded read()"
        assert sum(s for s in read_sizes if s) <= (
            bird_client.MAX_RESPONSE_BYTES + bird_client._READ_CHUNK)

    def test_negative_content_length_is_refused(self, tmp_path):
        server = _Bird(content_length=-1)
        try:
            with pytest.raises(bird_client.BirdRequestError):
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
        finally:
            server.close()

    def test_non_numeric_content_length_is_refused(self, tmp_path):
        server = _Bird(content_length="banana")
        try:
            with pytest.raises(bird_client.BirdRequestError):
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
        finally:
            server.close()

    def test_redirects_are_not_followed(self, tmp_path):
        """Following one would send the attached token to an undeclared origin."""
        server = _Bird(status=302, location="http://evil.example/")
        try:
            with pytest.raises(bird_client.BirdRequestError) as exc:
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
            assert "302" in exc.value.reason
        finally:
            server.close()


class TestResponseHandling:
    def test_empty_body(self, tmp_path):
        server = _Bird(raw_body=b"")
        try:
            with pytest.raises(bird_client.BirdRequestError) as exc:
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
            assert "empty body" in exc.value.reason
        finally:
            server.close()

    def test_non_json_body(self, tmp_path):
        server = _Bird(raw_body=b"<html>nope</html>")
        try:
            with pytest.raises(bird_client.BirdRequestError) as exc:
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
            assert "not JSON" in exc.value.reason
        finally:
            server.close()

    def test_json_array_body(self, tmp_path):
        server = _Bird(body=[1, 2, 3])
        try:
            with pytest.raises(bird_client.BirdRequestError) as exc:
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
            assert "not an object" in exc.value.reason
        finally:
            server.close()

    @pytest.mark.parametrize("status,fragment", [
        (401, "credential"), (403, "credential"), (500, "HTTP 500"), (404, "HTTP 404"),
    ])
    def test_error_statuses_become_reasons(self, tmp_path, status, fragment):
        server = _Bird(status=status)
        try:
            with pytest.raises(bird_client.BirdRequestError) as exc:
                bird_client.fetch_menu(_descriptor(server.port, tmp_path))
            assert fragment in exc.value.reason
        finally:
            server.close()

    def test_reason_never_leaks_an_os_error_string(self, tmp_path):
        server = _Bird()
        port = server.port
        server.close()
        with pytest.raises(bird_client.BirdRequestError) as exc:
            bird_client.fetch_menu(_descriptor(port, tmp_path), timeout=1.0)
        assert "Errno" not in exc.value.reason
        assert str(port) not in exc.value.reason


class TestEndpoints:
    def test_default_menu_endpoint(self, bird, tmp_path):
        bird_client.fetch_menu(_descriptor(bird.port, tmp_path))
        assert bird.requests[-1]["path"] == "/api/menu"

    def test_declared_menu_endpoint_is_used(self, bird, tmp_path):
        descriptor = _descriptor(
            bird.port, tmp_path, endpoints={"menu": "/custom/menu"}
        )
        bird_client.fetch_menu(descriptor)
        assert bird.requests[-1]["path"] == "/custom/menu"

    def test_host_is_pinned_to_loopback(self, bird, tmp_path):
        """A bird declares a path, never where that path lives."""
        bird_client.fetch_menu(_descriptor(bird.port, tmp_path))
        host = bird.header("Host")
        assert host.startswith("127.0.0.1")


class TestSendAction:
    def test_action_is_posted_verbatim(self, bird, tmp_path):
        descriptor = _descriptor(bird.port, tmp_path)
        bird_client.send_action(descriptor, "focus:session/abc-123")
        request = bird.requests[-1]
        assert request["method"] == "POST"
        assert request["path"] == "/api/menu/action"
        assert json.loads(request["body"]) == {"id": "focus:session/abc-123"}

    def test_action_carries_this_birds_token(self, bird, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("tok", encoding="utf-8")
        descriptor = _descriptor(
            bird.port, tmp_path, token_path=token_file, token_header="X-T"
        )
        bird_client.send_action(descriptor, "go")
        assert bird.header("X-T") == "tok"

    def test_declared_action_endpoint_is_used(self, bird, tmp_path):
        descriptor = _descriptor(
            bird.port, tmp_path, endpoints={"action": "/do"}
        )
        bird_client.send_action(descriptor, "go")
        assert bird.requests[-1]["path"] == "/do"

    @pytest.mark.parametrize("bad", ["", "a\r\nb", "a\x00b", "\x1b[31m"])
    def test_unsafe_action_ids_are_refused_before_the_wire(self, bird, tmp_path, bad):
        with pytest.raises(bird_client.BirdRequestError):
            bird_client.send_action(_descriptor(bird.port, tmp_path), bad)
        assert bird.requests == []


class TestOpenUrl:
    def test_builds_a_loopback_url_from_the_descriptor_port(self, tmp_path):
        descriptor = _descriptor(47100, tmp_path)
        assert bird_client.open_url(descriptor, "/console") == \
            "http://127.0.0.1:47100/console"

    def test_refuses_a_non_local_path(self, tmp_path):
        with pytest.raises(bird_client.BirdRequestError):
            bird_client.open_url(_descriptor(47100, tmp_path), "http://evil.example/")
