"""Bounded, per-bird HTTP client for fetching menus and forwarding actions.

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
    anywhere except the bird that offered it.
    """
    if not path.startswith("/"):
        raise BirdRequestError("Refused a link that is not bird-local.")
    return f"http://127.0.0.1:{descriptor.port}{path}"
