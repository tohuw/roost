"""Bird descriptor discovery, validation, and liveness.

A *bird* is a long-running local daemon that reports status into one shared
desktop menu. Each bird self-publishes a small JSON **descriptor** into a
well-known directory; Roost reads those files and renders what it finds. There
is no central registry the birds write through, so a bird that is not running
simply has no descriptor, and no bird can corrupt another's entry.

Two properties drive every design choice in this module:

**A descriptor is untrusted input.** It is a file written by another process. It
is never ``eval``'d, never trusted to be well-typed, and never trusted to be
truthful about its own name. Every field is range- and type-checked, and a file
that fails any check yields an :class:`UnavailableBird` carrying a
human-readable reason — never an exception that reaches the menu loop, and never
a partially-populated descriptor that later code has to re-validate.

**Version compatibility is a range, not an equality.** Roost advertises the
inclusive window ``MIN_API_VERSION..API_VERSION`` and accepts any bird whose own
declared window overlaps it. An exact ``!=`` comparison is the bug behind
huginn issue #38: a routine version bump silently disabled every participant,
with nothing on screen to say why. Here a genuinely incompatible bird is
reported as unavailable *with its declared range in the reason*, so the failure
is loud.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from roost import launcher
from roost import sanitize

_IS_WINDOWS = sys.platform == "win32"
log = logging.getLogger(__name__)

# ── Protocol version ──────────────────────────────────────────────────────────

#: The bird protocol version Roost implements.
API_VERSION = 1

#: The oldest protocol version Roost still speaks. Roost advertises the
#: inclusive range MIN_API_VERSION..API_VERSION and accepts any bird whose own
#: declared range overlaps it (huginn issue #38 — exact match silently disabled
#: every participant on a bump). Widening support is a one-line change here; a
#: genuinely breaking change raises MIN_API_VERSION and stale birds are then
#: refused loudly, on purpose.
MIN_API_VERSION = 1

#: Ceiling on what a descriptor may *declare*. Without it a hostile descriptor
#: could claim ``max_api = 2**63`` and stay "compatible" through every future
#: breaking change. Mirrors the bound huginn added for the same reason.
MAX_DECLARABLE_API = API_VERSION + 100

# ── Limits ────────────────────────────────────────────────────────────────────

#: A descriptor is a handful of short fields. Anything larger is not a
#: descriptor, and reading it into memory before deciding that would be the bug.
MAX_DESCRIPTOR_BYTES = 16 * 1024

#: Longest ``token_path`` contents Roost will read. A loopback token is tens
#: of bytes; this only exists so a descriptor cannot aim the host at a huge file.
MAX_TOKEN_BYTES = 4096

MAX_ENDPOINTS = 12
MAX_ENDPOINT_PATH_LENGTH = 256
MAX_NAME_LENGTH = 32
MAX_DISPLAY_LENGTH = 64

#: A bird name becomes a descriptor filename and appears in log lines, so it is
#: restricted to a slug with no path separators, dots, or case ambiguity.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

#: Endpoint keys are dict keys the host looks up by name; keep them boring.
_ENDPOINT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Environment override for the descriptor directory. Documented in SPEC.md so a
#: bird and the host can be pointed at the same alternate location (a test
#: harness, or a user who relocates state wholesale).
STATE_DIR_ENV = "BIRDS_STATE_DIR"

#: The same override under the name it carried when the flock was two ravens
#: rather than an open set of birds. Still honored, but *after* the current
#: name — see :func:`state_dir`.
LEGACY_STATE_DIR_ENV = "RAVENS_STATE_DIR"

#: The descriptor directory as it was named when this protocol had exactly two
#: participants and both were ravens. Huginn and Muninn publish here today: they
#: resolve the directory through the ``corvidae`` package, not through Roost, so
#: they will keep publishing here until that package is next released.
#:
#: Roost therefore **reads both** directories — see :func:`discover` — which is
#: what makes renaming the vocabulary cost no consumer an outage and requires no
#: commit in either raven's repository. Nothing writes here. When corvidae moves,
#: this becomes dead weight and can go; until then, removing it empties the menu.
LEGACY_DIR_WINDOWS = "Ravens"
LEGACY_DIR_POSIX = "ravens"

DESCRIPTOR_SUFFIX = ".json"

# ── Transport ─────────────────────────────────────────────────────────────────

#: A descriptor with no ``transport`` field speaks the original loopback-HTTP
#: surface. That absence, not a literal ``"http"``, is what most descriptors in
#: the wild say — Huginn and any bird that has not migrated never learn this
#: name exists. It is defined so the rest of this module has one spelling to
#: compare against, not because a bird is expected to write it.
TRANSPORT_HTTP = "http"

#: POSIX: a Unix domain socket. See docs/specs/021-unix-socket-transport.md in
#: Muninn's repository — the normative source for this transport, which Roost
#: implements the client side of.
TRANSPORT_UNIX = "unix"

#: Windows: a named pipe, since ``socket.AF_UNIX`` does not exist there.
TRANSPORT_PIPE = "pipe"

_TRANSPORTS = frozenset({TRANSPORT_HTTP, TRANSPORT_UNIX, TRANSPORT_PIPE})

#: The two transports that replace a loopback port with a
#: ``multiprocessing.connection`` address plus a rendered-pages directory.
SOCKET_TRANSPORTS = frozenset({TRANSPORT_UNIX, TRANSPORT_PIPE})

#: A socket path, a named-pipe path, or a pages directory is a filesystem path
#: a bird chose; bounded for the same reason ``token_path`` is — a hostile
#: descriptor must not be able to aim the host at an unbounded string.
MAX_ADDRESS_LENGTH = 4096
MAX_PAGES_DIR_LENGTH = 4096


# ── Path resolution ───────────────────────────────────────────────────────────

def _state_dir_named(windows_name: str, posix_name: str) -> Path:
    """Resolve the shared descriptor directory under a given pair of names."""
    if _IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / windows_name
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / posix_name


def state_dir() -> Path:
    """Return the directory birds publish their descriptors into.

    Resolution order:

    1. ``$BIRDS_STATE_DIR`` if set and non-empty.
    2. ``$RAVENS_STATE_DIR`` if set and non-empty — the same override under its
       former name, so a machine that already relocated its state keeps working.
    3. Windows: ``%LOCALAPPDATA%\\Birds`` (falling back to
       ``~\\AppData\\Local\\Birds`` when ``LOCALAPPDATA`` is unset).
    4. POSIX: ``$XDG_STATE_HOME/birds``, falling back to
       ``~/.local/state/birds``.

    This one rule is the contract every bird follows. Hardcoding a POSIX path —
    as the original proposal did — breaks Windows outright and ignores an
    ``XDG_STATE_HOME`` the user deliberately set. Note that honoring
    ``XDG_STATE_HOME`` is *not* optional here even though huginn's own state
    directory ignores it: that asymmetry is huginn's, and replicating it would
    put the shared directory somewhere no bird expects.

    This is where a bird **writes**. It is not the whole of where Roost
    **reads** — :func:`legacy_state_dir` is read too.
    """
    for env in (STATE_DIR_ENV, LEGACY_STATE_DIR_ENV):
        override = os.environ.get(env, "").strip()
        if override:
            return Path(override).expanduser()
    return _state_dir_named("Birds", "birds")


def legacy_state_dir() -> Path | None:
    """The pre-rename descriptor directory, or ``None`` when it is not in play.

    ``None`` when an explicit override is set — an override names *the* directory
    and quietly reading a second one behind the user's back would defeat the
    point of setting it, which matters most in a test harness pointed at a
    scratch directory.
    """
    for env in (STATE_DIR_ENV, LEGACY_STATE_DIR_ENV):
        if os.environ.get(env, "").strip():
            return None
    return _state_dir_named(LEGACY_DIR_WINDOWS, LEGACY_DIR_POSIX)


def descriptor_path(name: str) -> Path:
    """Return the descriptor path for ``name``, refusing anything but a slug.

    The name is validated *before* it is joined, so a caller cannot walk out of
    the descriptor directory with ``../`` or an absolute path.
    """
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError(
            f"Bird name {name!r} must match [a-z0-9][a-z0-9-]{{0,31}}"
        )
    base = state_dir()
    return base / f"{name}{DESCRIPTOR_SUFFIX}"


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BirdDescriptor:
    """A validated bird descriptor. Every field here has already been checked."""

    name: str
    display: str
    api_version: int
    min_api: int
    max_api: int
    pid: int
    #: The loopback port for the HTTP transport. ``None`` for a socket
    #: transport, which names its listener through :attr:`address` instead.
    port: int | None
    token_path: Path | None
    token_header: str
    endpoints: dict[str, str]
    host_priority: int
    started: float | None
    path: Path
    #: How to ask the OS to start this bird again, if it says. Optional: a
    #: bird predating the field keeps working, and gets no Start row.
    launch: "launcher.LaunchSpec | None" = None
    #: ``"http"`` when the descriptor omits the field, ``"unix"``, or
    #: ``"pipe"``. Absence means HTTP — this is the one field whose default
    #: encodes a whole surface's worth of unchanged behaviour, see
    #: :data:`TRANSPORT_HTTP`.
    transport: str = TRANSPORT_HTTP
    #: A socket path (``unix``) or a named-pipe path (``pipe``). ``None`` on
    #: the HTTP transport, which uses :attr:`port` instead.
    address: str | None = None
    #: Where a socket-transport bird has rendered the static pages its menu
    #: links point at. ``None`` on the HTTP transport, where a link resolves
    #: against :attr:`port` instead.
    pages_dir: Path | None = None

    @property
    def api_range(self) -> tuple[int, int]:
        """The inclusive protocol range this descriptor supports."""
        return (self.min_api, self.max_api)

    @property
    def is_socket_transport(self) -> bool:
        """True for ``unix``/``pipe`` — everything not the HTTP surface."""
        return self.transport in SOCKET_TRANSPORTS

    def endpoint(self, key: str) -> str | None:
        return self.endpoints.get(key)

    def base_url(self) -> str:
        """The loopback origin for an HTTP-transport bird.

        Meaningless for a socket transport, which has no port — callers must
        check :attr:`transport` (or :attr:`is_socket_transport`) first, the
        same way they must for :attr:`port` itself.
        """
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class UnavailableBird:
    """A bird Roost knows about but cannot use, and why.

    This is a first-class result, not an error path. An unreachable, stale, or
    malformed bird must render as a disabled section with a visible reason —
    never as a crash, never as a silent omission, and never as "trusted".
    """

    name: str
    display: str
    reason: str
    path: Path | None = None
    #: Present when the descriptor parsed far enough to say how to restart it.
    #: A bird that is *gone* is exactly the one worth offering to start, so
    #: this must survive the liveness check that made it unavailable.
    launch: "launcher.LaunchSpec | None" = None

    @property
    def available(self) -> bool:
        return False


@dataclass(frozen=True)
class AvailableBird:
    """A bird whose descriptor validated and whose process is alive."""

    descriptor: BirdDescriptor

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return self.descriptor.name

    @property
    def display(self) -> str:
        return self.descriptor.display


Bird = AvailableBird | UnavailableBird


# ── Field validators ──────────────────────────────────────────────────────────

class DescriptorError(ValueError):
    """A descriptor field failed validation. The message is user-facing."""


def _require_int(raw: object, field_name: str, low: int, high: int) -> int:
    """Return ``raw`` as an int in ``[low, high]``.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, so ``True`` would
    otherwise sail through as the port number 1.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise DescriptorError(f"{field_name} must be an integer")
    if not (low <= raw <= high):
        raise DescriptorError(f"{field_name} must be between {low} and {high}")
    return raw


def _require_str(raw: object, field_name: str, limit: int) -> str:
    if not isinstance(raw, str):
        raise DescriptorError(f"{field_name} must be a string")
    if not raw:
        raise DescriptorError(f"{field_name} must not be empty")
    if len(raw) > limit:
        raise DescriptorError(f"{field_name} must be {limit} characters or fewer")
    if sanitize.contains_unsafe_text(raw):
        # Repairing would be worse than refusing: a control character in a
        # descriptor means the file is not what it claims to be, and quietly
        # cleaning it up would hide that from the user.
        raise DescriptorError(f"{field_name} must not contain control characters")
    return raw


def _validate_endpoints(raw: object, transport: str) -> dict[str, str]:
    """Validate the endpoint map.

    On the HTTP transport every value is a rooted, relative path: an endpoint
    is joined onto ``http://127.0.0.1:{port}``, so a value carrying a scheme or
    an authority would redirect the host off the bird it is talking to — the
    descriptor equivalent of an open redirect. Only ``/``-rooted paths with no
    ``..`` segment are accepted.

    On a socket transport there is no URL space to route on at all (SPEC.md
    §9a; docs/specs/021-unix-socket-transport.md in Muninn's repository), so a
    value here is an *op name* sent verbatim as ``{"op": value}`` — the same
    shape as a raven's ``MENU_OP``/``ACTION_OP`` constants — and is bounded by
    the same character class as an endpoint key rather than by path rules that
    would not mean anything for it.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DescriptorError("endpoints must be an object")
    if len(raw) > MAX_ENDPOINTS:
        raise DescriptorError(f"endpoints must hold {MAX_ENDPOINTS} entries or fewer")
    endpoints: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _ENDPOINT_KEY_RE.fullmatch(key):
            raise DescriptorError("endpoint names must match [a-z][a-z0-9_]{0,31}")
        if transport != TRANSPORT_HTTP:
            op = _require_str(value, f"endpoints.{key}", MAX_ENDPOINT_PATH_LENGTH)
            if not _ENDPOINT_KEY_RE.fullmatch(op):
                raise DescriptorError(
                    f"endpoints.{key} must be an op name matching [a-z][a-z0-9_]{{0,31}}"
                )
            endpoints[key] = op
            continue
        path = _require_str(value, f"endpoints.{key}", MAX_ENDPOINT_PATH_LENGTH)
        if not path.startswith("/"):
            raise DescriptorError(f"endpoints.{key} must start with '/'")
        if path[1:2] in ("/", "\\"):
            # "//host/path" is a scheme-relative URL, not a local path, and some
            # URL parsers treat a leading "/\" the same way — both would point
            # the host at a different origin than the bird it is talking to.
            raise DescriptorError(f"endpoints.{key} must not start with '//' or '/\\'")
        if ".." in path.split("/"):
            raise DescriptorError(f"endpoints.{key} must not contain '..'")
        if "?" in path or "#" in path:
            raise DescriptorError(f"endpoints.{key} must not carry a query or fragment")
        endpoints[key] = path
    return endpoints


def _validate_token_path(raw: object, name: str) -> Path | None:
    """Validate a descriptor's ``token_path`` without reading the token.

    The path is only checked for *shape* here; whether a token can actually be
    read is decided at request time, because a bird may rotate its token at any
    moment. Roost never mints a credential on a bird's behalf, so a missing
    token file is the bird's problem to report, not Roost's to paper over.
    """
    if raw is None:
        return None
    text = _require_str(raw, "token_path", 4096)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise DescriptorError("token_path must resolve to an absolute path")
    log.debug("Bird %s declares a token at %s", sanitize.safe_for_log(name), candidate)
    return candidate


def _validate_token_header(raw: object) -> str:
    """Validate the header name a bird wants its token presented in.

    Restricted to an RFC 7230 token so the name cannot fold, split, or overwrite
    another header. Defaults per-bird rather than being a shared constant: the
    birds use ``X-<Name>-Token``, and a single well-known header name across
    birds would invite exactly the credential mixing this protocol forbids.
    """
    if raw is None:
        return ""
    text = _require_str(raw, "token_header", 64)
    if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", text):
        raise DescriptorError("token_header must be a valid HTTP header name")
    return text


def _validate_transport(raw: object) -> str:
    """Return the declared transport, defaulting absence to HTTP.

    ``None`` — the field omitted entirely — is the common case and means the
    original loopback-HTTP surface, unconditionally: a bird that has never
    heard of this field must validate exactly as it always has. Anything
    present must name one of the transports this host actually speaks; an
    unrecognised string is refused rather than guessed at; guessing is how a
    typo'd transport would silently fall back to a surface the bird never
    bound.
    """
    if raw is None:
        return TRANSPORT_HTTP
    if not isinstance(raw, str) or raw not in _TRANSPORTS:
        raise DescriptorError(
            f"transport must be one of {sorted(_TRANSPORTS)}"
        )
    return raw


def _check_api_range(payload: dict) -> tuple[int, int, int]:
    """Return ``(api_version, min_api, max_api)``, or raise if incompatible.

    ``min_api``/``max_api`` are optional and default to ``api_version``, so a
    bird that only speaks one version says so once.
    """
    api_version = _require_int(
        payload.get("api_version"), "api_version", 1, MAX_DECLARABLE_API
    )
    raw_min = payload.get("min_api")
    raw_max = payload.get("max_api")
    min_api = (
        api_version if raw_min is None
        else _require_int(raw_min, "min_api", 1, MAX_DECLARABLE_API)
    )
    max_api = (
        api_version if raw_max is None
        else _require_int(raw_max, "max_api", 1, MAX_DECLARABLE_API)
    )
    if min_api > max_api:
        raise DescriptorError(
            f"declared API range [{min_api}, {max_api}] is inverted"
        )
    # Inclusive ranges overlap unless one ends before the other begins.
    if not (min_api <= API_VERSION and max_api >= MIN_API_VERSION):
        raise DescriptorError(
            f"needs bird API [{min_api}, {max_api}]; "
            f"this menu bar speaks [{MIN_API_VERSION}, {API_VERSION}]"
        )
    return api_version, min_api, max_api


def parse_descriptor(text: str, path: Path, *, expected_name: str) -> BirdDescriptor:
    """Parse and fully validate one descriptor document.

    ``expected_name`` is the filename stem. A descriptor whose ``name`` disagrees
    with its filename is refused rather than reconciled: the filename is what
    discovery and the host lock key off, so allowing the two to differ would let
    one bird publish a descriptor that impersonates another.

    Raises :class:`DescriptorError` for anything malformed. Never raises anything
    else for content reasons.
    """
    try:
        payload = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DescriptorError(f"is not valid JSON ({exc.__class__.__name__})") from exc
    if not isinstance(payload, dict):
        raise DescriptorError("must be a JSON object")

    name = _require_str(payload.get("name"), "name", MAX_NAME_LENGTH)
    if not _NAME_RE.fullmatch(name):
        raise DescriptorError("name must match [a-z0-9][a-z0-9-]{0,31}")
    if name != expected_name:
        raise DescriptorError(
            f"declares name {name!r} but is filed as {expected_name!r}"
        )

    api_version, min_api, max_api = _check_api_range(payload)

    display_raw = payload.get("display")
    display = (
        name if display_raw is None
        else _require_str(display_raw, "display", MAX_DISPLAY_LENGTH)
    )

    pid = _require_int(payload.get("pid"), "pid", 1, 2**63 - 1)
    transport = _validate_transport(payload.get("transport"))

    if transport == TRANSPORT_HTTP:
        # Unchanged from every descriptor this host has ever read: a port is
        # mandatory, and there is no address or pages directory to speak of.
        port = _require_int(payload.get("port"), "port", 1, 65535)
        address = None
        pages_dir = None
    else:
        # ``port`` is simply absent from a socket-transport descriptor — see
        # Muninn's docs/specs/021 — so this branch never looks for it.
        port = None
        address = _require_str(
            payload.get("address"), "address", MAX_ADDRESS_LENGTH
        )
        pages_text = _require_str(
            payload.get("pages_dir"), "pages_dir", MAX_PAGES_DIR_LENGTH
        )
        pages_dir = Path(pages_text).expanduser()
        if not pages_dir.is_absolute():
            raise DescriptorError("pages_dir must resolve to an absolute path")

    started_raw = payload.get("started")
    if started_raw is None:
        started = None
    elif isinstance(started_raw, bool) or not isinstance(started_raw, (int, float)):
        raise DescriptorError("started must be a number")
    elif started_raw > 2**53:
        raise DescriptorError("started must be a plausible epoch time")
    elif started_raw <= 0:
        # Zero or negative is how a bird that could not read its own start time
        # records "unknown" -- corvidae's descriptor_is_live says so explicitly,
        # and a bird built on it will write exactly that. Treated as absent, not
        # as a value to compare: comparing would fail for *every* live process
        # rather than only recycled PIDs, and refusing the descriptor outright
        # (which a negative used to do) hid the bird behind a parse error. Both
        # break §8's rule that a missing cross-check must never turn a live bird
        # into a dead one.
        started = None
    else:
        started = float(started_raw)

    priority_raw = payload.get("host_priority")
    host_priority = (
        0 if priority_raw is None
        else _require_int(priority_raw, "host_priority", -1000, 1000)
    )

    try:
        launch = launcher.parse(payload.get("launch"))
    except launcher.LaunchError as exc:
        raise DescriptorError(str(exc)) from exc

    return BirdDescriptor(
        name=name,
        display=display,
        api_version=api_version,
        min_api=min_api,
        max_api=max_api,
        pid=pid,
        port=port,
        token_path=_validate_token_path(payload.get("token_path"), name),
        token_header=_validate_token_header(payload.get("token_header")),
        endpoints=_validate_endpoints(payload.get("endpoints"), transport),
        host_priority=host_priority,
        started=started,
        path=path,
        launch=launch,
        transport=transport,
        address=address,
        pages_dir=pages_dir,
    )


# ── Liveness ──────────────────────────────────────────────────────────────────

def pid_is_alive(pid: int, started: float | None = None) -> bool:
    """Return True if ``pid`` names a live process, resisting PID reuse.

    ``os.kill(pid, 0)`` sends no signal; it asks the kernel whether the process
    exists and is ours to signal. That alone cannot tell a live bird from an
    unrelated process that inherited a recycled PID, so when the descriptor
    carries ``started`` it is cross-checked against the OS's own record of when
    the process began. That check is why ``started`` is in the schema at all.

    A non-positive PID is refused before it reaches ``os.kill``: ``-1`` would
    address every process this user can signal and ``0`` this process's own
    group. Nothing here signals anything, but the same file feeds code that
    might, so the guard lives at the bottom.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False

    if _IS_WINDOWS:
        try:
            import psutil
        except ImportError:
            log.debug("psutil is unavailable; cannot verify bird liveness")
            return False
        try:
            candidate = psutil.Process(pid)
            if not candidate.is_running():
                return False
            if started is not None:
                # Windows recycles PIDs aggressively, so a match on PID alone is
                # not evidence. Two seconds of slack absorbs the difference
                # between the bird's own clock reading and the OS's.
                if abs(candidate.create_time() - started) > 2.0:
                    return False
            return True
        except psutil.AccessDenied:
            # It exists; we simply cannot inspect it. Treat as alive so a bird
            # running with different privileges is not silently dropped.
            return True
        except (psutil.Error, OSError):
            return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    if started is not None:
        actual = process_start_time(pid)
        if actual is not None and abs(actual - started) > 2.0:
            return False
    return True


def process_start_time(pid: int) -> float | None:
    """Return a process's start time as epoch seconds, or None if unknown.

    Uses ``ps -o lstart``-free arithmetic via ``psutil`` when it is installed,
    and otherwise gives up rather than guessing. Returning None means "cannot
    corroborate", which :func:`pid_is_alive` treats as "do not contradict" — a
    missing cross-check must not turn a live bird into a dead one.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def default_started() -> float:
    """This process's start time, for the ``started`` field of a descriptor.

    ``started`` describes when the *process* began, not when the descriptor was
    written — :func:`pid_is_alive` corroborates it against the OS's own record,
    so anything else fails that check. Writing ``time.time()`` here is only
    right for a bird that publishes the instant it starts; one that republishes
    later (on a state change, say) would stamp a fresh timestamp onto a process
    the OS says began long ago, and the host would declare the live bird dead.

    Falls back to the current time when the OS record is unavailable, which is
    the same "cannot corroborate" case :func:`process_start_time` returns None
    for — and which the liveness check treats as no evidence either way.
    """
    return process_start_time(os.getpid()) or time.time()


# ── Discovery ─────────────────────────────────────────────────────────────────

def _read_descriptor_text(path: Path) -> str:
    """Read a descriptor, refusing anything over the size cap.

    The cap is enforced on the read itself, not on the stat: a file can grow
    between the two, and the point of the cap is to bound what enters memory.
    """
    with path.open("rb") as handle:
        raw = handle.read(MAX_DESCRIPTOR_BYTES + 1)
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise DescriptorError(
            f"is larger than {MAX_DESCRIPTOR_BYTES} bytes"
        )
    return raw.decode("utf-8")


def load_bird(path: Path) -> Bird:
    """Load one descriptor file into an :class:`AvailableBird` or a reason.

    Every failure mode — unreadable, oversized, malformed, version-incompatible,
    dead process — comes back as :class:`UnavailableBird`. Nothing propagates.
    """
    stem = path.name[: -len(DESCRIPTOR_SUFFIX)] if path.name.endswith(DESCRIPTOR_SUFFIX) else path.stem
    display = sanitize.sanitize_label(stem, sanitize.DEFAULT_LOG_LIMIT) or "Unknown bird"
    safe_stem = stem if _NAME_RE.fullmatch(stem or "") else ""

    if not safe_stem:
        return UnavailableBird(
            name="", display=display,
            reason="Descriptor filename is not a valid bird name.",
            path=path,
        )

    try:
        text = _read_descriptor_text(path)
    except DescriptorError as exc:
        return UnavailableBird(safe_stem, display, f"Descriptor {exc}", path)
    except (OSError, UnicodeDecodeError) as exc:
        return UnavailableBird(
            safe_stem, display,
            f"Descriptor could not be read ({exc.__class__.__name__}).",
            path,
        )

    try:
        descriptor = parse_descriptor(text, path, expected_name=safe_stem)
    except DescriptorError as exc:
        # The reason text is composed from validator messages, which never echo
        # descriptor content back except through _require_str's field names —
        # so a hostile label cannot reach the menu through the failure path.
        return UnavailableBird(safe_stem, display, f"Descriptor {exc}.", path)

    if not pid_is_alive(descriptor.pid, descriptor.started):
        return UnavailableBird(
            descriptor.name, descriptor.display,
            "Not running (its recorded process is gone).",
            path,
            launch=descriptor.launch,
        )

    return AvailableBird(descriptor)


def discover(directory: Path | None = None) -> list[Bird]:
    """Return every bird found in the descriptor directory, best first.

    Ordering is by descending ``host_priority``, then by name, so the menu is
    stable across polls and the bird that declares itself primary leads. That
    ordering is data the birds supply — Roost does not know which bird
    "should" be first, and hardcoding one would be the same mistake as a
    hardcoded catalog id.

    Unavailable birds sort last among themselves by name, and are always
    returned: an unreachable bird that vanished from the menu would look like a
    bird that was never installed.

    **Two directories are read, not one.** The current one, and the one this
    contract used while it was named for ravens — because Huginn and Muninn
    resolve their publish location through ``corvidae`` rather than through
    Roost, and so still write to the old name. Reading both is what let the
    vocabulary change without a coordinated release across four repositories.

    A name found in both directories resolves to the **current** one. That is
    the migration case rather than a conflict: a bird that has moved leaves its
    old descriptor behind if it was killed before it could withdraw it, and
    preferring the stale copy would show a dead port for a running process.
    """
    if directory is not None:
        bases = [directory]
    else:
        bases = [state_dir()]
        if (legacy := legacy_state_dir()) is not None and legacy != bases[0]:
            bases.append(legacy)

    entries: dict[str, Path] = {}
    for base in bases:
        try:
            found = sorted(base.glob(f"*{DESCRIPTOR_SUFFIX}"))
        except OSError:
            log.debug("Bird descriptor directory %s is unreadable", base, exc_info=True)
            continue
        for path in found:
            # setdefault, so the first base listed — the current directory —
            # wins over a leftover of the same name in the legacy one.
            if path.is_file():
                entries.setdefault(path.stem, path)

    birds = [load_bird(path) for path in sorted(entries.values(), key=lambda p: p.stem)]

    def sort_key(bird: Bird) -> tuple[int, int, str]:
        if isinstance(bird, AvailableBird):
            return (0, -bird.descriptor.host_priority, bird.name)
        return (1, 0, bird.name)

    return sorted(birds, key=sort_key)


def available(birds: list[Bird]) -> list[AvailableBird]:
    return [bird for bird in birds if isinstance(bird, AvailableBird)]


# ── Publishing (used by the reference implementations and the tests) ──────────

@dataclass
class DescriptorDocument:
    """The descriptor a bird publishes. Builds the exact schema the host reads.

    Birds are welcome to write the JSON themselves — the schema is the contract,
    not this class. It exists so the reference implementations in ``examples/``
    and Roost's own tests cannot drift from the parser.
    """

    name: str
    display: str
    #: Required for the (default) HTTP transport; left ``None`` for a
    #: socket transport, which is named by ``address``/``pages_dir`` instead.
    port: int | None = None
    transport: str = TRANSPORT_HTTP
    address: str | None = None
    pages_dir: str | None = None
    pid: int = field(default_factory=os.getpid)
    started: float = field(default_factory=lambda: default_started())
    token_path: str | None = None
    token_header: str = ""
    endpoints: dict[str, str] = field(default_factory=dict)
    host_priority: int = 0
    api_version: int = API_VERSION
    min_api: int = MIN_API_VERSION
    max_api: int = API_VERSION

    def to_dict(self) -> dict:
        payload: dict = {
            "api_version": self.api_version,
            "min_api": self.min_api,
            "max_api": self.max_api,
            "name": self.name,
            "display": self.display,
            "pid": self.pid,
            "started": self.started,
            "host_priority": self.host_priority,
            "endpoints": dict(self.endpoints),
        }
        if self.transport == TRANSPORT_HTTP:
            payload["port"] = self.port
        else:
            payload["transport"] = self.transport
            payload["address"] = self.address
            payload["pages_dir"] = self.pages_dir
        if self.token_path:
            payload["token_path"] = self.token_path
        if self.token_header:
            payload["token_header"] = self.token_header
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
