#!/usr/bin/env python3
"""Reference bird: the companion, on the Unix-socket transport.

The second worked implementation of ``SPEC.md``, standing in for Muninn — the
agent-history companion. It is deliberately the *plainer* of the two examples,
because that is the more useful demonstration: the same contract, a different
bird, and no coordination between them beyond the shared directory and the
declared priority.

It is also, since [§9a](../SPEC.md#9a-the-unix-socket-and-named-pipe-transport),
the one example on the socket transport rather than HTTP — modeled on the real
Muninn's own move away from a loopback port, which is documented in full in
Muninn's docs/specs/021-unix-socket-transport.md. ``huginn_bird.py`` still shows
the HTTP transport, unchanged; both are legitimate choices a bird makes for
itself, and Roost dispatches on the descriptor's ``transport`` field to speak
either one.

Read it against ``huginn_bird.py``. The differences that are not about the
transport are all things the contract permits a bird to decide for itself:

- **A lower ``host_priority``**, so Muninn sorts after Huginn when both are
  present and sorts first — alone — when Huginn is not running. Neither bird
  knows the other exists; the ordering is entirely these two numbers, and
  Roost knows neither name.
- **No ``token_path``.** A Unix domain socket needs none: opening it already
  requires filesystem permission on its own inode, which is the guarantee a
  token would otherwise exist to approximate. (A ``pipe``-transport bird on
  Windows is the one case in this protocol where publishing a token is the
  right call — see SPEC.md §9a and Muninn's own docs/specs/021 for why.)
- **Link rows rather than actions.** Every row here opens a page this bird
  itself rendered under its own ``pages_dir``. A bird that has nothing to be
  clicked does not need an action op, and Roost renders it identically.
- **A section that is sometimes empty.** When there is no history the menu has
  no sections, and Roost draws the bird with "Nothing to report." — which is
  visibly different from a bird it could not reach.

Documentation, not a library: Roost does not import this, and neither does the
real Muninn.

Run it (POSIX only — this transport has no Windows named-pipe demonstration
here; see SPEC.md §9a for that side of the contract):

    python3 examples/muninn_bird.py
"""

from __future__ import annotations

import atexit
import html
import json
import os
import signal
import sys
import tempfile
import threading
import time
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any

NAME = "muninn"
DISPLAY = "Muninn"

#: A range, not a version. Equality is the bug behind huginn issue #38: one
#: routine bump silently disabled every participant with nothing on screen to
#: explain it. Widening the window this bird accepts is a one-line change here.
MIN_API = 1
MAX_API = 1

#: Lower than Huginn's, so Huginn leads when both are present. This number is the
#: *only* thing that decides the order; the host has no opinion and no list of
#: known birds.
HOST_PRIORITY = 50

#: The op this bird answers. Muninn publishes no ``action`` op — every row is a
#: link — so this is the only one the descriptor's ``endpoints`` ever names.
MENU_OP = "menu"

#: Mirrors ravenserve.MAX_REQUEST_BODY: a request here is never more than
#: ``{"op": "menu"}``, so anything larger is not a legitimate one.
MAX_REQUEST_BODY = 512


def state_dir() -> Path:
    """Return the shared descriptor directory.

    Byte-for-byte the same rule as ``huginn_bird.state_dir``, and that identity
    is the point: it is the contract, not either bird's preference. A bird that
    resolves this differently publishes where the host is not looking, and the
    failure is silent — an empty menu with nothing to explain it.
    """
    override = os.environ.get("BIRDS_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "Birds"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "birds"


def socket_path(directory: Path) -> Path:
    """Where the Unix domain socket is bound."""
    return directory / f"{NAME}.sock"


def pages_dir(directory: Path) -> Path:
    """Where every link row's target is rendered to a static file.

    A subdirectory of the descriptor directory rather than a sibling of
    ``muninn.json``: pages are per-build render output, not protocol state,
    and giving them their own directory means they can be wiped independently
    of everything else other birds keep alongside them.
    """
    return directory / NAME / "pages"


# ── This bird's state ────────────────────────────────────────────────────────
#
# Hardcoded on purpose. A real bird reads its own store; Roost cannot tell.

HISTORY = [
    {"id": "h-1", "title": "Deployed staging", "when": "12m ago"},
    {"id": "h-2", "title": "Reverted parser change", "when": "2h ago"},
    {"id": "h-3", "title": "Merged #412", "when": "yesterday"},
]


def build_menu() -> dict:
    """Return this bird's menu contribution.

    Every row is a link, so this bird needs no action op at all. It still
    declares ``menu`` in its descriptor's endpoints; omitting an action op is
    how a bird says it has nothing to be clicked, and Roost renders the rows
    the same way either way.

    The ``url`` values here are unchanged from the HTTP-transport version of
    this same example: ``/history/<id>`` and ``/``. What changed is entirely on
    the client side — Roost now resolves them against ``pages_dir`` instead of
    a port (SPEC.md §9a) — which is the whole point of the transport being an
    implementation detail the menu payload does not know about.
    """
    if not HISTORY:
        # No sections is a legitimate answer. Roost draws the bird with
        # "Nothing to report." — distinct from a bird it could not reach, which
        # gets its own reason instead.
        return {"api_version": MAX_API, "title": DISPLAY, "sections": []}

    items = [
        {
            "label": entry["title"],
            "detail": entry["when"],
            # Resolved by Roost against this bird's own pages_dir, never
            # against a port: a row cannot navigate the user anywhere this
            # bird did not itself render (SPEC.md §9a).
            "url": f"/history/{entry['id']}",
            "style": "muted",
        }
        for entry in HISTORY
    ]
    items.append({"separator": True})
    items.append({"label": "Open History", "url": "/"})

    return {
        "api_version": MAX_API,
        "title": DISPLAY,
        # No badge: nothing here wants attention. Omitting it is the same as
        # zero, and the host sums badges across birds either way.
        "sections": [{"id": "recent", "title": "Recent", "items": items}],
    }


# ── Pages: what a link row actually opens ─────────────────────────────────────
#
# Rendered fresh on every menu fetch, inside the same request that builds the
# payload — never on a stale schedule — so that every ``url`` the payload just
# emitted has a real file waiting for it by the time the reply goes out. A
# ``url`` with no corresponding file is exactly what Roost's containment check
# treats as "refused," so a page rendered late, or for a row that got dropped,
# is a link that silently stops working rather than one that silently opens
# something wrong.

def _page(title: str, body_html: str) -> str:
    safe_title = html.escape(title)
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{safe_title}</title></head><body><h1>{safe_title}</h1>"
            f"{body_html}</body></html>")


def _atomic_write_html(target: Path, content: str) -> None:
    """Stage in the same directory and replace, so a reader never sees a
    partial file — the same discipline ``publish`` below uses for the
    descriptor itself.
    """
    directory = target.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(directory))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def write_pages(pages: Path, payload: dict) -> None:
    """Render every link row in ``payload`` to a static file under ``pages``.

    Titles and details are this bird's own hardcoded data, but they are still
    escaped: a renderer that only escapes untrusted input is one refactor away
    from not escaping at all, and the real Muninn's transcripts are exactly
    the untrusted content that makes this matter in production.
    """
    pages.mkdir(parents=True, exist_ok=True)
    history_dir = pages / "history"
    history_dir.mkdir(exist_ok=True)

    lines = [f"<p>{DISPLAY} — agent history.</p>", "<ul>"]
    for entry in HISTORY:
        lines.append(
            f"<li>{html.escape(entry['title'])} — {html.escape(entry['when'])}</li>"
        )
    lines.append("</ul>")
    _atomic_write_html(pages / "index.html", _page(DISPLAY, "\n".join(lines)))

    for entry in HISTORY:
        body = (f"<p>{html.escape(entry['title'])}</p>"
                f"<p>{html.escape(entry['when'])}</p>")
        _atomic_write_html(history_dir / f"{entry['id']}.html", _page(DISPLAY, body))


# ── Descriptor ────────────────────────────────────────────────────────────────

def publish(directory: Path, address: Path, pages: Path) -> Path:
    """Write this bird's descriptor atomically.

    Note what is *absent*: no ``port`` and no ``token_path``. This is the
    ``unix`` transport (SPEC.md §9a): ``address`` and ``pages_dir`` replace a
    port, and the socket file's own permissions are the entire credential —
    there is nothing for a token to add.
    """
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_version": MAX_API,
        "min_api": MIN_API,
        "max_api": MAX_API,
        "name": NAME,
        "display": DISPLAY,
        "pid": os.getpid(),
        "transport": "unix",
        "address": str(address),
        "pages_dir": str(pages),
        # Cross-checked by the host against the OS's record of when this process
        # began, so a recycled PID cannot pass as a live bird.
        "started": time.time(),
        "host_priority": HOST_PRIORITY,
        "endpoints": {"menu": MENU_OP},
    }
    target = directory / f"{NAME}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{NAME}.", dir=str(directory))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def withdraw(descriptor: Path, address: Path) -> None:
    """Remove the descriptor and the socket file so a stopped bird leaves
    nothing behind.

    Best-effort. A hard kill skips this, and the host copes: it checks the
    recorded PID before trusting the file, so a stale descriptor renders as "Not
    running" with a reason rather than as a phantom bird.
    """
    for target in (descriptor, address):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


# ── The listener ──────────────────────────────────────────────────────────────
#
# multiprocessing.connection over a Unix domain socket: one connection per
# call, a JSON ``{"op": ...}`` body in, a ``{"ok": ..., ...}`` reply out, then
# close. No keep-alive, no pipelining, and no Host/Origin checking of any
# kind — there is no HTTP here for either to apply to, which is the entire
# point of this transport over the one ``huginn_bird.py`` still uses.

def _encode(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _handle(conn: Any, pages: Path) -> None:
    try:
        raw = conn.recv_bytes(maxlength=MAX_REQUEST_BODY)
    except OSError:
        conn.close()
        return
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or request.get("op") != MENU_OP:
            reply = _encode({"ok": False, "error": "unknown op"})
        else:
            payload = build_menu()
            # Rendered inside the same request that returns the payload, so
            # every url it just emitted has a file waiting for it already —
            # see the note on write_pages above.
            write_pages(pages, payload)
            reply = _encode({"ok": True, "body": payload})
    except (UnicodeDecodeError, json.JSONDecodeError):
        reply = _encode({"ok": False, "error": "body is not JSON"})
    except Exception:
        reply = _encode({"ok": False, "error": "internal error"})
    try:
        conn.send_bytes(reply)
    except OSError:
        pass
    conn.close()


def _serve_forever(listener: Listener, pages: Path) -> None:
    while True:
        try:
            conn = listener.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(conn, pages), daemon=True).start()


def main() -> int:
    if sys.platform == "win32":
        print(
            "This example demonstrates the POSIX 'unix' transport only. "
            "See SPEC.md §9a for the Windows 'pipe' side of the contract.",
            file=sys.stderr,
        )
        return 1

    directory = state_dir()
    address = socket_path(directory)
    pages = pages_dir(directory)

    directory.mkdir(parents=True, exist_ok=True)
    try:
        address.unlink()
    except FileNotFoundError:
        pass
    listener = Listener(str(address), family="AF_UNIX")
    try:
        os.chmod(address, 0o600)
    except OSError:
        pass

    # After the bind, never before: a descriptor naming an address nothing is
    # listening on makes the host report a healthy bird as unreachable.
    descriptor = publish(directory, address, pages)

    atexit.register(withdraw, descriptor, address)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_a: sys.exit(0))

    print(f"{DISPLAY} listening on Unix domain socket {address}")
    print(f"  descriptor {descriptor}")
    print(f"  pages      {pages}")
    print("  no token: the socket's own file mode is the whole credential.")
    print("Ctrl-C to stop.")
    try:
        _serve_forever(listener, pages)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
