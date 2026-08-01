"""Selectable tray icon, defaulting to the raven.

The user picks which mark sits in the menu bar or system tray. The choice is
stored in Roost's own config and exposed both as a menu submenu (with the
active one marked) and as a CLI verb.

Two files back every icon, because the platforms consume a tray icon
incompatibly:

- macOS ``template=True`` reads **only the alpha channel** and discards RGB, so
  the shell can tint the icon for light mode, dark mode, and menu highlight. A
  template icon must therefore be a monochrome silhouette; handing it colour
  achieves nothing because the colour is thrown away.
- pystray on Windows has **no template concept**. It renders the RGBA bitmap
  literally, so the monochrome-on-transparent file that is correct on macOS is
  invisible against a taskbar of the same shade. Windows gets the full-colour
  variant.

That asymmetry is why a *user-supplied* colour PNG defaults to
``template=False``: passing a colour image with ``template=True`` renders it as a
flat silhouette, which reads as a bug rather than as a setting.

Icons are rasterized ahead of time by ``tools/build-icons.sh`` and checked in.
They are not generated at install time, because ``sips`` is macOS-only and Pillow
cannot read SVG — there is no cross-platform way to rasterize on demand.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from roost import paths

log = logging.getLogger(__name__)

_IS_MACOS = sys.platform == "darwin"

HERE = Path(__file__).resolve().parent
ASSETS_DIR = HERE / "assets"

#: Roost's config file, inside Roost's own state directory. Named rather than
#: left as a generic ``config.toml`` so it is self-describing if it is ever looked
#: at next to another tool's state.
CONFIG_NAME = "roost.toml"

#: The icon used when nothing is configured.
DEFAULT_ICON = "raven"

#: Extensions a user may point ``roost icon set`` at. SVG is absent on
#: purpose: neither rumps nor pystray can rasterize one, and the vector sources
#: in ``assets/`` are converted ahead of time by tools/build-icons.sh.
USER_ICON_SUFFIXES = {".png", ".ico"}

MAX_ICON_BYTES = 10 * 1024 * 1024

#: A built-in icon name becomes a filename, so it is restricted to a slug.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


@dataclass(frozen=True)
class IconChoice:
    """A resolved tray icon: which file to load and how to render it."""

    name: str
    path: Path
    template: bool
    builtin: bool

    @property
    def label(self) -> str:
        if self.builtin:
            return self.name.replace("-", " ").title()
        return self.path.name


def config_path() -> Path:
    return paths.STATE_DIR / CONFIG_NAME


# ── Built-in catalog ──────────────────────────────────────────────────────────

def _variant_paths(name: str) -> tuple[Path, Path]:
    """Return ``(template, colour)`` asset paths for a built-in icon name."""
    return (
        ASSETS_DIR / f"{name}-template.png",
        ASSETS_DIR / f"{name}.png",
    )


def builtin_names() -> list[str]:
    """Return the built-in icon names present on disk, default first.

    Discovered from ``assets/`` rather than hardcoded, so adding an icon is a
    matter of dropping an SVG in and running the build script.
    """
    names = set()
    try:
        for candidate in ASSETS_DIR.glob("*.png"):
            # Strip the scale suffix before the variant suffix: the filenames are
            # "raven-template@2x.png", so taking "-template" off first leaves
            # "raven@2x" and then "raven" — but doing it in the other order
            # leaves "raven-template", which would list the template variant as
            # an icon of its own.
            base = candidate.stem.split("@", 1)[0]
            if base.endswith("-template"):
                base = base[: -len("-template")]
            if _NAME_RE.fullmatch(base):
                names.add(base)
    except OSError:
        # Empty, not [DEFAULT_ICON]: naming an icon whose file cannot be read
        # would put an entry in the submenu that selects nothing. resolve()
        # already returns None for "no icon available", which the tray handles.
        log.debug("Icon assets directory is unreadable", exc_info=True)
        return []
    ordered = sorted(names - {DEFAULT_ICON})
    return ([DEFAULT_ICON] if DEFAULT_ICON in names else []) + ordered


def resolve_builtin(name: str) -> IconChoice | None:
    """Resolve a built-in icon name to the right variant for this platform.

    macOS gets the monochrome template (it keeps only alpha and tints it);
    Windows gets the full-colour PNG (it renders RGB literally, so a monochrome
    file would vanish into a same-shade taskbar). Each falls back to the other
    variant if its preferred one is missing — a wrong-looking icon beats no icon.
    """
    if not _NAME_RE.fullmatch(name or ""):
        return None
    template_path, colour_path = _variant_paths(name)
    if _IS_MACOS:
        if template_path.is_file():
            return IconChoice(name, template_path, template=True, builtin=True)
        if colour_path.is_file():
            return IconChoice(name, colour_path, template=False, builtin=True)
        return None
    if colour_path.is_file():
        return IconChoice(name, colour_path, template=False, builtin=True)
    if template_path.is_file():
        return IconChoice(name, template_path, template=False, builtin=True)
    return None


# ── User-supplied icons ───────────────────────────────────────────────────────

def resolve_user_icon(raw: str) -> IconChoice | None:
    """Resolve a user-supplied icon path, or None if it is unusable.

    ``template`` is False for a user icon: the file is presumed to carry colour,
    and colour plus ``template=True`` renders a flat silhouette on macOS, which
    users read as a bug rather than as their icon.
    """
    try:
        path = Path(raw).expanduser()
    except (OSError, ValueError):
        return None
    if not path.is_absolute():
        return None
    if path.suffix.lower() not in USER_ICON_SUFFIXES:
        return None
    try:
        if not path.is_file() or path.stat().st_size > MAX_ICON_BYTES:
            return None
    except OSError:
        return None
    return IconChoice(path.name, path, template=False, builtin=False)


# ── Config I/O ────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Read Roost's config, degrading to empty rather than raising.

    Every entry point loads this, including the CLI verb that would let the user
    fix a bad value, so an unparseable file must not be able to lock them out.
    """
    path = config_path()
    if not path.is_file():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except Exception:
        log.warning("Roost config at %s is unreadable; using defaults", path)
        return {}


def configured_icon() -> str:
    """Return the raw configured icon value, or "" when nothing is set."""
    value = _load_config().get("icon")
    return value if isinstance(value, str) else ""


def _toml_string(value: str) -> str:
    """Escape a TOML basic string.

    A literal control character inside a basic string makes the file
    unparseable, which would take the icon setting — and the verb that fixes
    it — down together. So they are escaped rather than embedded.
    """
    escapes = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t",
               "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    out = [
        escapes.get(ch) or (ch if ch >= " " and ch != "\x7f" else f"\\u{ord(ch):04X}")
        for ch in value
    ]
    return '"' + "".join(out) + '"'


def set_icon(value: str) -> None:
    """Persist the chosen icon.

    Written owner-only and atomically, so a concurrent read never sees a
    half-written config and a half-written config never becomes the permanent
    state after a crash.
    """
    config = _load_config()
    config["icon"] = value
    lines = ["# Roost configuration. Managed by `roost icon`.", ""]
    for key in sorted(config):
        raw = config[key]
        if isinstance(raw, str):
            lines.append(f"{key} = {_toml_string(raw)}")
        elif isinstance(raw, bool):
            lines.append(f"{key} = {'true' if raw else 'false'}")
        elif isinstance(raw, (int, float)):
            lines.append(f"{key} = {raw}")
        # Anything else was not written by us and is dropped rather than guessed
        # at — round-tripping an arbitrary TOML value is not this function's job.
    paths.atomic_write_text(config_path(), "\n".join(lines) + "\n")


def clear_icon() -> None:
    """Drop the icon setting, reverting to the default."""
    config = _load_config()
    config.pop("icon", None)
    lines = ["# Roost configuration. Managed by `roost icon`.", ""]
    for key in sorted(config):
        raw = config[key]
        if isinstance(raw, str):
            lines.append(f"{key} = {_toml_string(raw)}")
    paths.atomic_write_text(config_path(), "\n".join(lines) + "\n")


# ── Resolution ────────────────────────────────────────────────────────────────

def resolve(value: str | None = None) -> IconChoice | None:
    """Resolve an icon value (or the configured one) to a usable choice.

    Falls back to the default when the configured value no longer resolves — a
    user who deletes the PNG they pointed Roost at gets the raven back, not a
    tray with no icon at all.
    """
    raw = configured_icon() if value is None else value
    raw = (raw or "").strip()

    if raw:
        if _NAME_RE.fullmatch(raw):
            choice = resolve_builtin(raw)
            if choice is not None:
                return choice
        else:
            choice = resolve_user_icon(raw)
            if choice is not None:
                return choice
        log.warning("Configured tray icon %r is unusable; falling back", raw[:120])

    return resolve_builtin(DEFAULT_ICON)


def choices() -> list[IconChoice]:
    """Return every selectable icon, including a configured user icon.

    Used to build the icon submenu. The active one is marked by the caller
    comparing against :func:`resolve`.
    """
    resolved = [
        choice for choice in (resolve_builtin(name) for name in builtin_names())
        if choice is not None
    ]
    configured = configured_icon().strip()
    if configured and not _NAME_RE.fullmatch(configured):
        user = resolve_user_icon(configured)
        if user is not None:
            resolved.append(user)
    return resolved


def is_active(choice: IconChoice, active: IconChoice | None) -> bool:
    if active is None:
        return False
    return choice.path == active.path
