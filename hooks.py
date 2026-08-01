"""Stable hook URL helpers for Appistry."""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

import registry


DEFAULT_HOOK_PORT = 47658
HOOK_PORT_ENV = "APPISTRY_HOOK_PORT"
HOOK_PORT_FILE = "stable-hook-port"


def _parse_hook_port(raw: str) -> int | None:
    try:
        port = int(raw)
    except ValueError:
        return None
    if 1024 <= port <= 65535:
        return port
    return None


def hook_port() -> int:
    raw = os.environ.get(HOOK_PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_HOOK_PORT
    return _parse_hook_port(raw) or DEFAULT_HOOK_PORT


def hook_port_path() -> Path:
    return registry.APPISTRY_DIR / HOOK_PORT_FILE


def active_hook_port() -> int:
    try:
        port = _parse_hook_port(hook_port_path().read_text(encoding="utf-8").strip())
    except OSError:
        port = None
    return port or hook_port()


def hook_url(app_id: str, target_path: str, *, port: int | None = None) -> str:
    """Return the stable Appistry proxy URL for an app-local path."""
    parsed = urllib.parse.urlsplit(target_path or "/")
    if parsed.scheme or parsed.netloc:
        raise ValueError("Hook target path must be app-local, not an absolute URL")
    path = "/" + parsed.path.lstrip("/")
    encoded_app = urllib.parse.quote(app_id, safe="")
    encoded_path = urllib.parse.quote(path.lstrip("/"), safe="/:@-._~!$&'()*+,;=")
    active_port = port if port is not None else active_hook_port()
    url = f"http://127.0.0.1:{active_port}/hooks/{encoded_app}/{encoded_path}"
    if parsed.query:
        url += f"?{parsed.query}"
    return url
