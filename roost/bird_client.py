"""Bounded, per-bird client for fetching menus and forwarding actions.

Two transports live here. A descriptor with no ``transport`` field (or
``transport: "http"``) speaks the original loopback-HTTP surface, unchanged
below. A descriptor declaring ``transport: "unix"`` or ``"pipe"`` speaks over
``multiprocessing.connection`` instead — see
docs/specs/021-unix-socket-transport.md in Muninn's repository for the
normative wire contract, and SPEC.md's own transport section for the client
side of it. Every public function here dispatches on ``descriptor.transport``
right at the top and does not let the two paths blend: a bird that has never
heard of the socket transport must go on validating and failing exactly as it
always has, byte for byte.

Two invariants govern this module.

**Per-bird token isolation.** Each bird keeps its own loopback token. Roost
reads a token from the ``token_path`` in *that bird's own descriptor* and sends
it only to *that bird's own port*, under the header name that bird asked for.
It never caches a token across birds, never sends one bird's credential to
another, and never mints a credential on a bird's behalf. A bird with no
``token_path`` gets an unauthenticated request — whether to accept that is the
bird's decision, not Roost's, and the failure surfaces as an ordinary HTTP
error with a visible reason.

**Every call is bounded.** A bird is another process that can hang, and this
client runs on the thread that builds a desktop menu. So every request has a
timeout, every response body has a cap enforced on the read rather than on the
declared ``Content-Length``, and every failure returns a reason instead of
raising. A hung bird must degrade to a disabled section, never to a frozen menu.

Nothing here forwards anything from an inbound request. This is an outbound
client with a fixed, allowlisted request header set — the ambient-credential
laundering that a proxy has to defend against cannot arise, because there is no
inbound request in scope to launder.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from multiprocessing import connection
from pathlib import Path

from roost import birds
from roost import sanitize
from roost.birds import BirdDescriptor

log = logging.getLogger(__name__)

#: A menu is a few dozen short rows. The cap is enforced on the read, so a bird
#: that streams forever is cut off rather than believed.
MAX_RESPONSE_BYTES = 256 * 1024
_READ_CHUNK = 32 * 1024

#: Menu fetches happen on the menu-build path, so this timeout is a UI budget,
#: not a network one. An action is user-initiated and may legitimately take
#: longer, but still cannot block indefinitely.
MENU_TIMEOUT = 2.0
ACTION_TIMEOUT = 5.0

#: The default header a bird's token is presented in when the descriptor does
#: not name one. Per-bird by construction (the name embeds the bird), because
#: one well-known shared header name is exactly the shape that invites sending
#: the wrong bird the wrong credential.
def default_token_header(name: str) -> str:
    slug = "".join(part.capitalize() for part in name.split("-") if part)
    return f"X-{slug or 'Bird'}-Token"


class BirdRequestError(Exception):
    """A bird call failed. ``reason`` is safe to render in the menu."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def read_token(descriptor: BirdDescriptor) -> str | None:
    """Read this bird's own token, or None if it has none to offer.

    Read fresh on every call, never cached. A bird rotates its token whenever it
    restarts (huginn mints a new one per daemon start), and a cached token would
    make the host authenticate with a dead credential and report the bird as
    broken. The size cap exists because ``token_path`` is descriptor-controlled:
    a hostile descriptor could otherwise aim this read at an arbitrarily large
    file.
    """
    path = descriptor.token_path
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(birds.MAX_TOKEN_BYTES + 1)
    except OSError:
        # Not an error worth failing the whole fetch on: the bird may be
        # mid-rotation. The unauthenticated request that follows will produce a
        # 401 with a reason the user can act on.
        log.debug("Could not read token for bird %s", sanitize.safe_for_log(descriptor.name))
        return None
    if len(raw) > birds.MAX_TOKEN_BYTES:
        log.warning(
            "Refusing oversized token file for bird %s",
            sanitize.safe_for_log(descriptor.name),
        )
        return None
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not token or sanitize.contains_unsafe_text(token) or any(c in token for c in "\r\n"):
        # A token carrying CR/LF would inject headers into our own request.
        log.warning(
            "Refusing malformed token for bird %s",
            sanitize.safe_for_log(descriptor.name),
        )
        return None
    return token


def _read_authkey(descriptor: BirdDescriptor) -> bytes | None:
    """Read a Windows named-pipe authkey from this bird's ``token_path``.

    Deliberately not :func:`read_token`: that function decodes its file as
    UTF-8 text and refuses anything carrying CR/LF or a control character,
    which is the right rule for a credential that rides in an HTTP header and
    the wrong one for 32 raw bytes handed to ``multiprocessing.connection``
    unmodified. Read fresh on every call for the same reason a token is — a
    bird mints a fresh key per run (docs/specs/021) and a cached one would
    authenticate with a dead credential.

    ``None`` on POSIX's own ``unix`` transport is normal, not a failure: that
    transport carries no token by design, and a missing file there simply
    means the descriptor never declared one, exactly like an HTTP bird with no
    ``token_path``.
    """
    path = descriptor.token_path
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(birds.MAX_TOKEN_BYTES + 1)
    except OSError:
        log.debug(
            "Could not read authkey for bird %s", sanitize.safe_for_log(descriptor.name)
        )
        return None
    if not raw or len(raw) > birds.MAX_TOKEN_BYTES:
        log.warning(
            "Refusing missing or oversized authkey file for bird %s",
            sanitize.safe_for_log(descriptor.name),
        )
        return None
    return raw


def _socket_family(descriptor: BirdDescriptor) -> str:
    return "AF_UNIX" if descriptor.transport == birds.TRANSPORT_UNIX else "AF_PIPE"


def _decode_socket_reply(raw: bytes) -> dict:
    """Parse one ``{"ok": ..., ...}`` reply. Raises :class:`BirdRequestError`.

    The wire shape is Muninn's docs/specs/021: ``{"ok": true, "body": ...}`` or
    ``{"ok": false, "error": "..."}``. An ``ok`` reply that is not literally
    ``True`` is treated as a failure — including a missing ``ok`` key entirely
    — because there is no HTTP status code here to carry that signal instead.
    """
    if not raw:
        raise BirdRequestError("Answered with an empty body.")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BirdRequestError("Answered with something that is not JSON.") from None
    if not isinstance(payload, dict):
        raise BirdRequestError("Answered with a JSON value that is not an object.")
    if payload.get("ok") is not True:
        reason = payload.get("error")
        if not isinstance(reason, str) or not reason:
            reason = "unknown error"
        raise BirdRequestError(sanitize.sanitize_label(reason, 120) or "Answered with an error.")
    return payload


def _socket_request(descriptor: BirdDescriptor, op_body: dict, *, timeout: float) -> dict:
    """Perform one bounded ``multiprocessing.connection`` request-reply.

    One connection per call, matching the transport's own contract (SPEC.md,
    docs/specs/021): connect, send one message, wait bounded for one reply,
    close. ``Client``/``Connection`` take no timeout parameter, so the bound on
    the wait comes from :func:`multiprocessing.connection.wait` rather than
    from the connect or the send, which are local IPC and do not block on
    anything remote.
    """
    authkey = (
        _read_authkey(descriptor) if descriptor.transport == birds.TRANSPORT_PIPE else None
    )
    try:
        conn = connection.Client(
            descriptor.address, family=_socket_family(descriptor), authkey=authkey
        )
    except (OSError, ValueError):
        raise BirdRequestError("Is not answering on its recorded address.") from None
    try:
        try:
            conn.send_bytes(json.dumps(op_body).encode("utf-8"))
        except OSError:
            raise BirdRequestError("Is not answering on its recorded address.") from None
        if not connection.wait([conn], timeout=timeout):
            raise BirdRequestError("Did not answer in time.")
        try:
            raw = conn.recv_bytes(maxlength=MAX_RESPONSE_BYTES)
        except OSError as exc:
            # multiprocessing.connection raises OSError both for "the peer hung
            # up" and for "the reply exceeded maxlength" -- the two read as
            # different problems, so text-match rather than collapse them into
            # one misleading reason.
            reason = (
                "Sent a response that is too large."
                if "too long" in str(exc).lower()
                else "Is not answering on its recorded address."
            )
            raise BirdRequestError(reason) from None
    finally:
        try:
            conn.close()
        except OSError:
            pass
    return _decode_socket_reply(raw)


def _build_headers(descriptor: BirdDescriptor, *, json_body: bool) -> dict[str, str]:
    """Build the outbound header set from scratch — never from an inbound request.

    The token goes only to the descriptor that declared it. Because the headers
    are built per call from a fixed allowlist, there is no path by which one
    bird's credential can appear in another bird's request.
    """
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    token = read_token(descriptor)
    if token:
        name = descriptor.token_header or default_token_header(descriptor.name)
        headers[name] = token
    return headers


def _endpoint_url(descriptor: BirdDescriptor, key: str, default: str) -> str:
    """Return the absolute loopback URL for one of a bird's endpoints.

    The host is pinned to ``127.0.0.1`` here, not taken from the descriptor. A
    bird declares a *path*; it never gets to say where that path lives.
    """
    path = descriptor.endpoint(key) or default
    return urllib.parse.urlunsplit(
        ("http", f"127.0.0.1:{descriptor.port}", path, "", "")
    )


def _read_capped(response) -> bytes:
    """Read a response body in bounded chunks, refusing anything over the cap.

    The declared ``Content-Length`` is checked first as a fast reject, but it is
    never used as a read size: a negative or absent value would make ``read()``
    consume until EOF, and an inflated one would make us wait for bytes that
    never arrive. The chunked read is what actually enforces the bound.
    """
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise BirdRequestError("Sent an invalid Content-Length.") from None
        if length < 0:
            raise BirdRequestError("Sent a negative Content-Length.")
        if length > MAX_RESPONSE_BYTES:
            raise BirdRequestError("Sent a response that is too large.")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise BirdRequestError("Sent a response that is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects.

    A bird that answers a menu fetch with a redirect is misbehaving, and
    following it would let the descriptor's own port send Roost — and the
    token it just attached — to an origin the descriptor never declared.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(
    descriptor: BirdDescriptor,
    url: str,
    *,
    timeout: float,
    body: bytes | None = None,
) -> dict:
    """Perform one bounded JSON request against a bird, or raise with a reason."""
    request = urllib.request.Request(
        url,
        data=body,
        headers=_build_headers(descriptor, json_body=body is not None),
        method="POST" if body is not None else "GET",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = _read_capped(response)
    except urllib.error.HTTPError as exc:
        # Read and discard: leaving the body unread on an error response keeps
        # the connection in an indeterminate state.
        #
        # Bounded, like the success path above and for the same reason. A bare
        # ``exc.read()`` reads until EOF, so a bird answering 500 with an
        # enormous body would be pulled into the tray's memory in full -- the one
        # read in this module that the cap did not cover. The descriptor
        # directory is writable by anything running as this user, which is the
        # threat model SPEC.md reasons about everywhere else, so "a bird would
        # not do that" is not an assumption available here. Draining is only to
        # settle the connection; the content is never looked at, so a partial
        # drain is as good as a whole one.
        try:
            drained = 0
            while drained < MAX_RESPONSE_BYTES:
                chunk = exc.read(_READ_CHUNK)
                if not chunk:
                    break
                drained += len(chunk)
        except Exception:
            pass
        if exc.code in (401, 403):
            raise BirdRequestError(
                "Rejected the credential from its own token file."
            ) from None
        raise BirdRequestError(f"Answered HTTP {exc.code}.") from None
    except TimeoutError:
        raise BirdRequestError("Did not answer in time.") from None
    except urllib.error.URLError as exc:
        # The reason may embed an OS error string; keep it out of the menu.
        log.debug("Bird request to port %d failed: %s", descriptor.port, exc)
        raise BirdRequestError("Is not answering on its recorded port.") from None
    except BirdRequestError:
        raise
    except OSError:
        raise BirdRequestError("Is not answering on its recorded port.") from None

    if not raw:
        raise BirdRequestError("Answered with an empty body.")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BirdRequestError("Answered with something that is not JSON.") from None
    if not isinstance(payload, dict):
        raise BirdRequestError("Answered with a JSON value that is not an object.")
    return payload


def fetch_menu(descriptor: BirdDescriptor, *, timeout: float = MENU_TIMEOUT) -> dict:
    """Fetch a bird's raw menu payload. Raises :class:`BirdRequestError`."""
    if descriptor.is_socket_transport:
        op = descriptor.endpoint("menu") or "menu"
        reply = _socket_request(descriptor, {"op": op}, timeout=timeout)
        body = reply.get("body")
        if not isinstance(body, dict):
            raise BirdRequestError("Answered with a JSON value that is not an object.")
        return body
    return _request(
        descriptor,
        _endpoint_url(descriptor, "menu", "/api/menu"),
        timeout=timeout,
    )


def send_action(
    descriptor: BirdDescriptor,
    action_id: str,
    *,
    timeout: float = ACTION_TIMEOUT,
) -> dict:
    """Forward an action id back to the bird that published it.

    Roost does not interpret ``action_id``. It came from this bird's menu and
    it goes back to this bird, unchanged, with this bird's own token — which is
    the whole of the host's involvement in what the action means.
    """
    if not action_id or sanitize.contains_unsafe_text(action_id):
        raise BirdRequestError("Refused an action id that is not printable text.")
    if descriptor.is_socket_transport:
        op = descriptor.endpoint("action") or "action"
        return _socket_request(
            descriptor, {"op": op, "id": action_id}, timeout=timeout
        )
    body = json.dumps({"id": action_id}).encode("utf-8")
    return _request(
        descriptor,
        _endpoint_url(descriptor, "action", "/api/menu/action"),
        timeout=timeout,
        body=body,
    )


def open_url(descriptor: BirdDescriptor, path: str) -> str:
    """Return the browser URL for a bird-local menu link.

    Built from the descriptor's own port, so a menu item cannot navigate the user
    anywhere except the bird that offered it. For a socket transport, resolved
    against its ``pages_dir`` instead — see :func:`_resolve_page_url`, which is
    the same invariant restated for a filesystem rather than a port.
    """
    if not path.startswith("/"):
        raise BirdRequestError("Refused a link that is not bird-local.")
    if descriptor.is_socket_transport:
        return _resolve_page_url(descriptor, path)
    return f"http://127.0.0.1:{descriptor.port}{path}"


def _resolve_page_url(descriptor: BirdDescriptor, path: str) -> str:
    """Resolve a socket-transport link ``path`` against this bird's ``pages_dir``.

    Mirrors docs/specs/021's client-side rule exactly: ``/`` maps to
    ``pages_dir/index.html``; any other path maps to
    ``pages_dir/<path-without-its-leading-slash>.html``. The candidate's own
    realpath must be contained under ``pages_dir``'s realpath and must name a
    file that already exists — anything else is refused. That containment
    check, not the string manipulation above it, is what actually enforces "a
    menu item cannot navigate the user anywhere except the bird that offered
    it": a page render is authoritative, a path string is not.
    """
    pages_dir = descriptor.pages_dir
    if pages_dir is None:
        raise BirdRequestError("Publishes no pages directory for this link.")

    relative = "index.html" if path == "/" else f"{path[1:]}.html"
    # Belt and braces ahead of the realpath check below, and for a reason
    # specific to pathlib: ``Path("/a") / "/b"`` evaluates to ``Path("/b")`` —
    # joining an absolute path onto another silently discards the left side
    # rather than raising. A path carrying a second leading slash (making
    # "without its leading slash" absolute again) or a ".." segment must
    # never reach that join at all, not rely on the resolve below to catch
    # what the join itself already threw away.
    if relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise BirdRequestError("Refused a link that would escape its pages directory.")

    candidate = pages_dir / relative
    try:
        real_pages = pages_dir.resolve(strict=False)
        real_candidate = candidate.resolve(strict=False)
    except OSError:
        raise BirdRequestError("Could not resolve its own pages directory.") from None
    try:
        real_candidate.relative_to(real_pages)
    except ValueError:
        raise BirdRequestError("Refused a link that would escape its pages directory.") from None
    if not real_candidate.is_file():
        raise BirdRequestError("Has no rendered page for that link.")
    # as_uri() rather than an f-string: a rendered page lives wherever the bird
    # put it, and "file://" + str(path) is only a valid URL when that path
    # happens to contain no spaces and no drive letter. On Windows it produced
    # "file://C:\dir\page.html" -- two slashes, so "C:" reads as the host, and
    # backslashes a browser is not obliged to fix -- and on either platform a
    # directory with a space in it produced a URL that stopped at the space.
    return real_candidate.as_uri()
