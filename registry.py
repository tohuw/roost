"""
registry.py

Data layer for Appistry. Reads and writes ~/.appistry/registry.toml,
provides the AppEntry dataclass, and exposes load/save/get/upsert/remove
helpers. TOML is written manually (no tomli-w dependency); stdlib tomllib
(Python 3.11+) is used for reading.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


APPISTRY_DIR = Path.home() / ".appistry"
REGISTRY_PATH = APPISTRY_DIR / "registry.toml"

# App ids are used to build filesystem paths (~/.appistry/pids/{id}.pid,
# ~/.appistry/{id}.log) and are embedded in generated shell launchers, so they
# must be restricted to a safe slug shape — no path separators, dots, or shell
# metacharacters.
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Display names are used verbatim as /Applications/{name}.app directory names.
# Strip anything that isn't safe in a Finder-visible path component.
_BUNDLE_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._-]+")


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """Convert a display name to a lowercase, hyphen-separated slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def validate_app_id(app_id: str) -> str:
    """Return app_id if it's a safe slug, else raise ValueError.

    App ids are interpolated into filesystem paths and shell launcher scripts,
    so anything outside [a-z0-9-] (path separators, quotes, whitespace) could
    escape the intended directory or inject into the generated launcher.
    """
    if not _APP_ID_RE.fullmatch(app_id or ""):
        raise ValueError(
            f"App id {app_id!r} must be a lowercase slug: [a-z0-9][a-z0-9-]{{0,63}}"
        )
    return app_id


def bundle_name_for(display_name: str, app_id: str) -> str:
    """Return a filesystem-safe name for /Applications/{name}.app.

    Strips characters that aren't safe as a path component and collapses
    runs of dots (macOS treats a leading dot or `..` specially). Falls back
    to the validated app id if the display name sanitizes to nothing — the
    id itself is validated here since callers may pass an id that was never
    checked (e.g. a stale registry entry written before validation existed).
    """
    safe_id = validate_app_id(app_id)
    cleaned = _BUNDLE_SAFE_RE.sub("-", display_name or "").strip(" .-_")
    cleaned = re.sub(r"\.{2,}", ".", cleaned)[:80].strip(" .-_")
    return cleaned or safe_id


def _toml_str(value: str) -> str:
    """Escape and quote a TOML string value."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_entry(entry: "AppEntry") -> str:
    """Render a single AppEntry as a TOML [[apps]] block."""
    lines = ["[[apps]]"]
    lines.append(f'id           = {_toml_str(entry.id)}')
    lines.append(f'name         = {_toml_str(entry.name)}')
    lines.append(f'cwd          = {_toml_str(entry.cwd)}')
    lines.append(f'command      = {_toml_str(entry.command)}')
    lines.append(f'port         = {entry.port}')
    lines.append(f'github_url   = {_toml_str(entry.github_url)}')
    if entry.icon is not None:
        lines.append(f'icon         = {_toml_str(entry.icon)}')
    lines.append(f'registered_at = {_toml_str(entry.registered_at)}')
    return "\n".join(lines)


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class AppEntry:
    id: str
    name: str
    cwd: str
    command: str
    port: int
    github_url: str = ""
    icon: Optional[str] = None
    registered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "")
    )

    @classmethod
    def from_dict(cls, d: dict) -> "AppEntry":
        return cls(
            id=d["id"],
            name=d["name"],
            cwd=d["cwd"],
            command=d["command"],
            port=int(d["port"]),
            github_url=d.get("github_url", ""),
            icon=d.get("icon"),
            registered_at=d.get("registered_at", ""),
        )

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "cwd": self.cwd,
            "command": self.command,
            "port": self.port,
            "github_url": self.github_url,
            "registered_at": self.registered_at,
        }
        if self.icon is not None:
            d["icon"] = self.icon
        return d


# ── I/O ───────────────────────────────────────────────────────────────────────

def load() -> list[AppEntry]:
    """Load all entries from the registry. Returns an empty list if absent."""
    if not REGISTRY_PATH.exists():
        return []
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]  # Python < 3.11 backport
    with REGISTRY_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    return [AppEntry.from_dict(a) for a in data.get("apps", [])]


def save(entries: list[AppEntry]) -> None:
    """Write all entries to the registry, overwriting any existing file."""
    APPISTRY_DIR.mkdir(parents=True, exist_ok=True)
    blocks = [_format_entry(e) for e in entries]
    content = "\n\n".join(blocks)
    if content:
        content += "\n"
    REGISTRY_PATH.write_text(content, encoding="utf-8")


def get(app_id: str) -> Optional[AppEntry]:
    """Return the AppEntry for app_id, or None if not found."""
    for entry in load():
        if entry.id == app_id:
            return entry
    return None


def upsert(entry: AppEntry) -> None:
    """Insert or update an entry by id, preserving registered_at on update."""
    entries = load()
    for i, existing in enumerate(entries):
        if existing.id == entry.id:
            # Preserve original registration timestamp on update
            entry.registered_at = existing.registered_at
            entries[i] = entry
            save(entries)
            return
    save(entries + [entry])


def remove(app_id: str) -> bool:
    """Remove an entry by id. Returns True if removed, False if not found."""
    entries = load()
    new_entries = [e for e in entries if e.id != app_id]
    if len(new_entries) == len(entries):
        return False
    save(new_entries)
    return True
