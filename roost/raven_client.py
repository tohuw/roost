"""Bounded, per-raven HTTP client for fetching menus and forwarding actions.

Two invariants govern this module.

**Per-raven token isolation.** Each raven keeps its own loopback token. Roost
reads a token from the ``token_path`` in *that raven's own descriptor* and sends
it only to *that raven's own port*, under the header name that raven asked for.
It never caches a token across ravens, never sends one raven's credential to
another, and never mints a credential on a raven's behalf. A raven with no
``token_path`` gets an unauthenticated request — whether to accept that is the
raven's decision, not Roost's, and the failure surfaces as an ordinary HTTP
error with a visible reason.

**Every call is bounded.** A raven is another process that can hang, and this
client runs on the thread that builds a desktop menu. So every request has a
timeout, every response body has a cap enforced on the read rather than on the
declared ``Content-Length``, and every failure returns a reason instead of
raising. A hung raven must degrade to a disabled section, never to a frozen menu.

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

from roost import ravens
from roost import sanitize
from roost.ravens import RavenDescriptor

log = logging.getLogger(__name__)

#: A menu is a few dozen short rows. The cap is enforced on the read, so a raven
#: that streams forever is cut off rather than believed.
MAX_RESPONSE_BYTES = 256 * 1024
_READ_CHUNK = 32 * 1024

#: Menu fetches happen on the menu-build path, so this timeout is a UI budget,
#: not a network one. An action is user-initiated and may legitimately take
#: longer, but still cannot block indefinitely.
MENU_TIMEOUT = 2.0
ACTION_TIMEOUT = 5.0

#: The default header a raven's token is presented in when the descriptor does
#: not name one. Per-raven by construction (the name embeds the raven), because
#: one well-known shared header name is exactly the shape that invites sending
#: the wrong raven the wrong credential.
def default_token_header(name: str) -> str:
    slug = "".join(part.capitalize() for part in name.split("-") if part)
    return f"X-{slug or 'Raven'}-Token"


class RavenRequestError(Exception):
    """A raven call failed. ``reason`` is safe to render in the menu."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def read_token(descriptor: RavenDescriptor) -> str | None:
    """Read this raven's own token, or None if it has none to offer.

    Read fresh on every call, never cached. A raven rotates its token whenever it
    restarts (huginn mints a new one per daemon start), and a cached token would
    make the host authenticate with a dead credential and report the raven as
    broken. The size cap exists because ``token_path`` is descriptor-controlled:
    a hostile descriptor could otherwise aim this read at an arbitrarily large
    file.
    """
    path = descriptor.token_path
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(ravens.MAX_TOKEN_BYTES + 1)
    except OSError:
        # Not an error worth failing the whole fetch on: the raven may be
        # mid-rotation. The unauthenticated request that follows will produce a
        # 401 with a reason the user can act on.
        log.debug("Could not read token for raven %s", sanitize.safe_for_log(descriptor.name))
        return None
    if len(raw) > ravens.MAX_TOKEN_BYTES:
        log.warning(
            "Refusing oversized token file for raven %s",
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
            "Refusing malformed token for raven %s",
            sanitize.safe_for_log(descriptor.name),
        )
        return None
    return token


def _build_headers(descriptor: RavenDescriptor, *, json_body: bool) -> dict[str, str]:
    """Build the outbound header set from scratch — never from an inbound request.

    The token goes only to the descriptor that declared it. Because the headers
    are built per call from a fixed allowlist, there is no path by which one
    raven's credential can appear in another raven's request.
    """
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    token = read_token(descriptor)
    if token:
        name = descriptor.token_header or default_token_header(descriptor.name)
        headers[name] = token
    return headers


def _endpoint_url(descriptor: RavenDescriptor, key: str, default: str) -> str:
    """Return the absolute loopback URL for one of a raven's endpoints.

    The host is pinned to ``127.0.0.1`` here, not taken from the descriptor. A
    raven declares a *path*; it never gets to say where that path lives.
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
            raise RavenRequestError("Sent an invalid Content-Length.") from None
        if length < 0:
            raise RavenRequestError("Sent a negative Content-Length.")
        if length > MAX_RESPONSE_BYTES:
            raise RavenRequestError("Sent a response that is too large.")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise RavenRequestError("Sent a response that is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects.

    A raven that answers a menu fetch with a redirect is misbehaving, and
    following it would let the descriptor's own port send Roost — and the
    token it just attached — to an origin the descriptor never declared.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(
    descriptor: RavenDescriptor,
    url: str,
    *,
    timeout: float,
    body: bytes | None = None,
) -> dict:
    """Perform one bounded JSON request against a raven, or raise with a reason."""
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
        try:
            exc.read()
        except Exception:
            pass
        if exc.code in (401, 403):
            raise RavenRequestError(
                "Rejected the credential from its own token file."
            ) from None
        raise RavenRequestError(f"Answered HTTP {exc.code}.") from None
    except TimeoutError:
        raise RavenRequestError("Did not answer in time.") from None
    except urllib.error.URLError as exc:
        # The reason may embed an OS error string; keep it out of the menu.
        log.debug("Raven request to port %d failed: %s", descriptor.port, exc)
        raise RavenRequestError("Is not answering on its recorded port.") from None
    except RavenRequestError:
        raise
    except OSError:
        raise RavenRequestError("Is not answering on its recorded port.") from None

    if not raw:
        raise RavenRequestError("Answered with an empty body.")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise RavenRequestError("Answered with something that is not JSON.") from None
    if not isinstance(payload, dict):
        raise RavenRequestError("Answered with a JSON value that is not an object.")
    return payload


def fetch_menu(descriptor: RavenDescriptor, *, timeout: float = MENU_TIMEOUT) -> dict:
    """Fetch a raven's raw menu payload. Raises :class:`RavenRequestError`."""
    return _request(
        descriptor,
        _endpoint_url(descriptor, "menu", "/api/menu"),
        timeout=timeout,
    )


def send_action(
    descriptor: RavenDescriptor,
    action_id: str,
    *,
    timeout: float = ACTION_TIMEOUT,
) -> dict:
    """Forward an action id back to the raven that published it.

    Roost does not interpret ``action_id``. It came from this raven's menu and
    it goes back to this raven, unchanged, with this raven's own token — which is
    the whole of the host's involvement in what the action means.
    """
    if not action_id or sanitize.contains_unsafe_text(action_id):
        raise RavenRequestError("Refused an action id that is not printable text.")
    body = json.dumps({"id": action_id}).encode("utf-8")
    return _request(
        descriptor,
        _endpoint_url(descriptor, "action", "/api/menu/action"),
        timeout=timeout,
        body=body,
    )


def open_url(descriptor: RavenDescriptor, path: str) -> str:
    """Return the browser URL for a raven-local menu link.

    Built from the descriptor's own port, so a menu item cannot navigate the user
    anywhere except the raven that offered it.
    """
    if not path.startswith("/"):
        raise RavenRequestError("Refused a link that is not raven-local.")
    return f"http://127.0.0.1:{descriptor.port}{path}"
