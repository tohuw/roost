"""Raven descriptor discovery, validation, and liveness.

A *raven* is a long-running local daemon that reports status into one shared
desktop menu. Each raven self-publishes a small JSON **descriptor** into a
well-known directory; Roost reads those files and renders what it finds. There
is no central registry the ravens write through, so a raven that is not running
simply has no descriptor, and no raven can corrupt another's entry.

Two properties drive every design choice in this module:

**A descriptor is untrusted input.** It is a file written by another process. It
is never ``eval``'d, never trusted to be well-typed, and never trusted to be
truthful about its own name. Every field is range- and type-checked, and a file
that fails any check yields an :class:`UnavailableRaven` carrying a
human-readable reason — never an exception that reaches the menu loop, and never
a partially-populated descriptor that later code has to re-validate.

**Version compatibility is a range, not an equality.** Roost advertises the
inclusive window ``MIN_API_VERSION..API_VERSION`` and accepts any raven whose own
declared window overlaps it. An exact ``!=`` comparison is the bug behind
huginn issue #38: a routine version bump silently disabled every participant,
with nothing on screen to say why. Here a genuinely incompatible raven is
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

#: The raven protocol version Roost implements.
API_VERSION = 1

#: The oldest protocol version Roost still speaks. Roost advertises the
#: inclusive range MIN_API_VERSION..API_VERSION and accepts any raven whose own
#: declared range overlaps it (huginn issue #38 — exact match silently disabled
#: every participant on a bump). Widening support is a one-line change here; a
#: genuinely breaking change raises MIN_API_VERSION and stale ravens are then
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

#: A raven name becomes a descriptor filename and appears in log lines, so it is
#: restricted to a slug with no path separators, dots, or case ambiguity.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

#: Endpoint keys are dict keys the host looks up by name; keep them boring.
_ENDPOINT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Environment override for the descriptor directory. Documented in SPEC.md so a
#: raven and the host can be pointed at the same alternate location (a test
#: harness, or a user who relocates state wholesale).
STATE_DIR_ENV = "RAVENS_STATE_DIR"

DESCRIPTOR_SUFFIX = ".json"


# ── Path resolution ───────────────────────────────────────────────────────────

def state_dir() -> Path:
    """Return the directory ravens publish their descriptors into.

    Resolution order:

    1. ``$RAVENS_STATE_DIR`` if set and non-empty.
    2. Windows: ``%LOCALAPPDATA%\\Ravens`` (falling back to
       ``~\\AppData\\Local\\Ravens`` when ``LOCALAPPDATA`` is unset).
    3. POSIX: ``$XDG_STATE_HOME/ravens``, falling back to
       ``~/.local/state/ravens``.

    This one rule is the contract both ravens follow. Hardcoding a POSIX path —
    as the original proposal did — breaks Windows outright and ignores an
    ``XDG_STATE_HOME`` the user deliberately set. Note that honoring
    ``XDG_STATE_HOME`` is *not* optional here even though huginn's own state
    directory ignores it: that asymmetry is huginn's, and replicating it would
    put the shared directory somewhere neither raven expects.
    """
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if _IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "Ravens"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "ravens"


def descriptor_path(name: str) -> Path:
    """Return the descriptor path for ``name``, refusing anything but a slug.

    The name is validated *before* it is joined, so a caller cannot walk out of
    the descriptor directory with ``../`` or an absolute path.
    """
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError(
            f"Raven name {name!r} must match [a-z0-9][a-z0-9-]{{0,31}}"
        )
    base = state_dir()
    return base / f"{name}{DESCRIPTOR_SUFFIX}"


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RavenDescriptor:
    """A validated raven descriptor. Every field here has already been checked."""

    name: str
    display: str
    api_version: int
    min_api: int
    max_api: int
    pid: int
    port: int
    token_path: Path | None
    token_header: str
    endpoints: dict[str, str]
    host_priority: int
    started: float | None
    path: Path
    #: How to ask the OS to start this raven again, if it says. Optional: a
    #: raven predating the field keeps working, and gets no Start row.
    launch: "launcher.LaunchSpec | None" = None

    @property
    def api_range(self) -> tuple[int, int]:
        """The inclusive protocol range this descriptor supports."""
        return (self.min_api, self.max_api)

    def endpoint(self, key: str) -> str | None:
        return self.endpoints.get(key)

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class UnavailableRaven:
    """A raven Roost knows about but cannot use, and why.

    This is a first-class result, not an error path. An unreachable, stale, or
    malformed raven must render as a disabled section with a visible reason —
    never as a crash, never as a silent omission, and never as "trusted".
    """

    name: str
    display: str
    reason: str
    path: Path | None = None
    #: Present when the descriptor parsed far enough to say how to restart it.
    #: A raven that is *gone* is exactly the one worth offering to start, so
    #: this must survive the liveness check that made it unavailable.
    launch: "launcher.LaunchSpec | None" = None

    @property
    def available(self) -> bool:
        return False


@dataclass(frozen=True)
class AvailableRaven:
    """A raven whose descriptor validated and whose process is alive."""

    descriptor: RavenDescriptor

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return self.descriptor.name

    @property
    def display(self) -> str:
        return self.descriptor.display


Raven = AvailableRaven | UnavailableRaven


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


def _validate_endpoints(raw: object) -> dict[str, str]:
    """Validate the endpoint map. Every value must be a rooted, relative path.

    An endpoint is joined onto ``http://127.0.0.1:{port}``, so a value carrying a
    scheme or an authority would redirect the host off the raven it is talking
    to — the descriptor equivalent of an open redirect. Only ``/``-rooted paths
    with no ``..`` segment are accepted.
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
        path = _require_str(value, f"endpoints.{key}", MAX_ENDPOINT_PATH_LENGTH)
        if not path.startswith("/"):
            raise DescriptorError(f"endpoints.{key} must start with '/'")
        if path[1:2] in ("/", "\\"):
            # "//host/path" is a scheme-relative URL, not a local path, and some
            # URL parsers treat a leading "/\" the same way — both would point
            # the host at a different origin than the raven it is talking to.
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
    read is decided at request time, because a raven may rotate its token at any
    moment. Roost never mints a credential on a raven's behalf, so a missing
    token file is the raven's problem to report, not Roost's to paper over.
    """
    if raw is None:
        return None
    text = _require_str(raw, "token_path", 4096)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise DescriptorError("token_path must resolve to an absolute path")
    log.debug("Raven %s declares a token at %s", sanitize.safe_for_log(name), candidate)
    return candidate


def _validate_token_header(raw: object) -> str:
    """Validate the header name a raven wants its token presented in.

    Restricted to an RFC 7230 token so the name cannot fold, split, or overwrite
    another header. Defaults per-raven rather than being a shared constant: the
    ravens use ``X-<Name>-Token``, and a single well-known header name across
    ravens would invite exactly the credential mixing this protocol forbids.
    """
    if raw is None:
        return ""
    text = _require_str(raw, "token_header", 64)
    if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", text):
        raise DescriptorError("token_header must be a valid HTTP header name")
    return text


def _check_api_range(payload: dict) -> tuple[int, int, int]:
    """Return ``(api_version, min_api, max_api)``, or raise if incompatible.

    ``min_api``/``max_api`` are optional and default to ``api_version``, so a
    raven that only speaks one version says so once.
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
            f"needs raven API [{min_api}, {max_api}]; "
            f"this menu bar speaks [{MIN_API_VERSION}, {API_VERSION}]"
        )
    return api_version, min_api, max_api


def parse_descriptor(text: str, path: Path, *, expected_name: str) -> RavenDescriptor:
    """Parse and fully validate one descriptor document.

    ``expected_name`` is the filename stem. A descriptor whose ``name`` disagrees
    with its filename is refused rather than reconciled: the filename is what
    discovery and the host lock key off, so allowing the two to differ would let
    one raven publish a descriptor that impersonates another.

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
    port = _require_int(payload.get("port"), "port", 1, 65535)

    started_raw = payload.get("started")
    if started_raw is None:
        started = None
    elif isinstance(started_raw, bool) or not isinstance(started_raw, (int, float)):
        raise DescriptorError("started must be a number")
    elif not (0 <= started_raw <= 2**53):
        raise DescriptorError("started must be a plausible epoch time")
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

    return RavenDescriptor(
        name=name,
        display=display,
        api_version=api_version,
        min_api=min_api,
        max_api=max_api,
        pid=pid,
        port=port,
        token_path=_validate_token_path(payload.get("token_path"), name),
        token_header=_validate_token_header(payload.get("token_header")),
        endpoints=_validate_endpoints(payload.get("endpoints")),
        host_priority=host_priority,
        started=started,
        path=path,
        launch=launch,
    )


# ── Liveness ──────────────────────────────────────────────────────────────────

def pid_is_alive(pid: int, started: float | None = None) -> bool:
    """Return True if ``pid`` names a live process, resisting PID reuse.

    ``os.kill(pid, 0)`` sends no signal; it asks the kernel whether the process
    exists and is ours to signal. That alone cannot tell a live raven from an
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
            log.debug("psutil is unavailable; cannot verify raven liveness")
            return False
        try:
            candidate = psutil.Process(pid)
            if not candidate.is_running():
                return False
            if started is not None:
                # Windows recycles PIDs aggressively, so a match on PID alone is
                # not evidence. Two seconds of slack absorbs the difference
                # between the raven's own clock reading and the OS's.
                if abs(candidate.create_time() - started) > 2.0:
                    return False
            return True
        except psutil.AccessDenied:
            # It exists; we simply cannot inspect it. Treat as alive so a raven
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
        actual = _posix_process_start_time(pid)
        if actual is not None and abs(actual - started) > 2.0:
            return False
    return True


def _posix_process_start_time(pid: int) -> float | None:
    """Return a process's start time as epoch seconds, or None if unknown.

    Uses ``ps -o lstart``-free arithmetic via ``psutil`` when it is installed,
    and otherwise gives up rather than guessing. Returning None means "cannot
    corroborate", which :func:`pid_is_alive` treats as "do not contradict" — a
    missing cross-check must not turn a live raven into a dead one.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


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


def load_raven(path: Path) -> Raven:
    """Load one descriptor file into an :class:`AvailableRaven` or a reason.

    Every failure mode — unreadable, oversized, malformed, version-incompatible,
    dead process — comes back as :class:`UnavailableRaven`. Nothing propagates.
    """
    stem = path.name[: -len(DESCRIPTOR_SUFFIX)] if path.name.endswith(DESCRIPTOR_SUFFIX) else path.stem
    display = sanitize.sanitize_label(stem, sanitize.DEFAULT_LOG_LIMIT) or "Unknown raven"
    safe_stem = stem if _NAME_RE.fullmatch(stem or "") else ""

    if not safe_stem:
        return UnavailableRaven(
            name="", display=display,
            reason="Descriptor filename is not a valid raven name.",
            path=path,
        )

    try:
        text = _read_descriptor_text(path)
    except DescriptorError as exc:
        return UnavailableRaven(safe_stem, display, f"Descriptor {exc}", path)
    except (OSError, UnicodeDecodeError) as exc:
        return UnavailableRaven(
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
        return UnavailableRaven(safe_stem, display, f"Descriptor {exc}.", path)

    if not pid_is_alive(descriptor.pid, descriptor.started):
        return UnavailableRaven(
            descriptor.name, descriptor.display,
            "Not running (its recorded process is gone).",
            path,
            launch=descriptor.launch,
        )

    return AvailableRaven(descriptor)


def discover(directory: Path | None = None) -> list[Raven]:
    """Return every raven found in the descriptor directory, best first.

    Ordering is by descending ``host_priority``, then by name, so the menu is
    stable across polls and the raven that declares itself primary leads. That
    ordering is data the ravens supply — Roost does not know which raven
    "should" be first, and hardcoding one would be the same mistake as a
    hardcoded catalog id.

    Unavailable ravens sort last among themselves by name, and are always
    returned: an unreachable raven that vanished from the menu would look like a
    raven that was never installed.
    """
    base = directory or state_dir()
    try:
        entries = sorted(base.glob(f"*{DESCRIPTOR_SUFFIX}"))
    except OSError:
        log.debug("Raven descriptor directory %s is unreadable", base, exc_info=True)
        return []

    ravens = [load_raven(path) for path in entries if path.is_file()]

    def sort_key(raven: Raven) -> tuple[int, int, str]:
        if isinstance(raven, AvailableRaven):
            return (0, -raven.descriptor.host_priority, raven.name)
        return (1, 0, raven.name)

    return sorted(ravens, key=sort_key)


def available(ravens: list[Raven]) -> list[AvailableRaven]:
    return [raven for raven in ravens if isinstance(raven, AvailableRaven)]


# ── Publishing (used by the reference implementations and the tests) ──────────

@dataclass
class DescriptorDocument:
    """The descriptor a raven publishes. Builds the exact schema the host reads.

    Ravens are welcome to write the JSON themselves — the schema is the contract,
    not this class. It exists so the reference implementations in ``examples/``
    and Roost's own tests cannot drift from the parser.
    """

    name: str
    display: str
    port: int
    pid: int = field(default_factory=os.getpid)
    started: float = field(default_factory=time.time)
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
            "port": self.port,
            "started": self.started,
            "host_priority": self.host_priority,
            "endpoints": dict(self.endpoints),
        }
        if self.token_path:
            payload["token_path"] = self.token_path
        if self.token_header:
            payload["token_header"] = self.token_header
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
