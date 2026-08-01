"""Appistry's shared local service and macOS menu bar implementation.

The launch/readiness page, stable hook proxy, help server, and menu state
helpers are imported by the Windows tray. macOS-only UI imports are guarded so
this module remains importable on Windows.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure this directory is on sys.path so registry and process are importable
# regardless of how launchd sets up the environment.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import html
import http.server
import json
import logging
import os
import socket
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

_IS_MACOS = sys.platform == "darwin"

if _IS_MACOS:
    import fcntl
    import objc
    from Foundation import NSObject
else:
    fcntl = None

    class NSObject:  # pragma: no cover - definition only supports safe import on Windows
        pass

    class _ObjCShim:
        @staticmethod
        def super(cls, instance):
            return super(cls, instance)

        @staticmethod
        def typedSelector(_signature):
            return lambda function: function

    objc = _ObjCShim()

log = logging.getLogger(__name__)

# ── Environment bootstrap ─────────────────────────────────────────────────────
# launchd doesn't source shell init files, so env vars set in ~/.zshenv are
# invisible to this process. Load them now so anything Appistry starts sees the
# same environment it would in a terminal. Since this process's os.environ is
# what gets copied into every app Appistry launches (see
# process._build_launch_env), _refresh_zshenv() is polled from _poll() below so
# edits to ~/.zshenv take effect without restarting Appistry.

_ZSHENV_PATH = Path.home() / ".zshenv"
_zshenv_mtime: float | None = None
_zshenv_managed_keys: set[str] = set()


def _source_zshenv() -> dict[str, str]:
    if not _IS_MACOS:
        return {}
    try:
        result = subprocess.run(
            ["zsh", "-c", "source ~/.zshenv 2>/dev/null; env -0"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return {}
    env = {}
    for item in result.stdout.split("\0"):
        if "=" in item:
            key, _, value = item.partition("=")
            env[key] = value
    return env


def _load_zshenv() -> None:
    global _zshenv_mtime
    if not _ZSHENV_PATH.exists():
        return
    for key, value in _source_zshenv().items():
        if key not in os.environ:
            os.environ[key] = value
            _zshenv_managed_keys.add(key)
    _zshenv_mtime = _ZSHENV_PATH.stat().st_mtime


def _refresh_zshenv() -> None:
    """Re-source ~/.zshenv if it changed since the last check.

    Only touches keys we previously sourced from zshenv (or new keys not
    already present) so we never clobber launchd-provided values we don't own.
    """
    global _zshenv_mtime
    if not _ZSHENV_PATH.exists():
        return
    mtime = _ZSHENV_PATH.stat().st_mtime
    if mtime == _zshenv_mtime:
        return
    _zshenv_mtime = mtime
    for key, value in _source_zshenv().items():
        if key in _zshenv_managed_keys or key not in os.environ:
            os.environ[key] = value
            _zshenv_managed_keys.add(key)


_load_zshenv()

if _IS_MACOS:
    import rumps
else:
    class _RumpsAppShim:
        pass

    class _RumpsShim:
        App = _RumpsAppShim

        @staticmethod
        def timer(_seconds):
            return lambda function: function

        @staticmethod
        def quit_application():
            raise RuntimeError("rumps is unavailable on Windows")

    rumps = _RumpsShim()

import cleanup
import hooks
import launch
import process
import registry

_BUNDLE_GRACE_SECONDS = 15  # ignore absences shorter than this (e.g. during rebuild)
_HELP_PORT_PATH = registry.APPISTRY_DIR / "menubar-http-port"
_LAUNCH_TIMEOUT_SECONDS = 45
_LAUNCH_POLL_MS = 750
_LAUNCH_ICON_TYPES = {
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_HOOK_MAX_BODY = 1_048_576
_HOOK_PROXY_TIMEOUT = 30
_SEARCH_LABEL = "Search running apps"
_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


# ── Single-instance guard ─────────────────────────────────────────────────────

_LOCK_PATH = HERE / ".menubar.lock"
_lock_fd = None  # kept open for the process lifetime; closing releases the flock


def _acquire_lock() -> bool:
    """Acquire an exclusive flock on the lock file.

    Returns True if this process is now the sole instance.
    Returns False if another instance already holds the lock.
    The kernel releases the lock automatically if this process dies for any reason,
    so stale locks are never an issue.
    """
    global _lock_fd
    if not _IS_MACOS:
        raise RuntimeError("The macOS menu lock is unavailable on this platform")
    raw_fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    _lock_fd = os.fdopen(raw_fd, "r+")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _lock_fd.close()
        _lock_fd = None
        return False
    _lock_fd.seek(0)
    _lock_fd.truncate()
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
    return True


def _release_lock():
    global _lock_fd
    if _lock_fd is not None:
        try:
            _lock_fd.close()
        except OSError:
            pass
        _lock_fd = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _osascript_notify(title: str, message: str) -> None:
    """Fire a macOS notification, safely escaping registry-controlled strings."""
    safe_title   = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{safe_message}" with title "{safe_title}"'],
        capture_output=True,
    )


def _app_url(port: int) -> str:
    """Return the canonical local browser URL for an app port."""
    return f"http://127.0.0.1:{port}"


def _port_is_valid(port: int) -> bool:
    return 1 <= int(port) <= 65535


def _probe_app_port(port: int, timeout: float = 0.75) -> bool:
    """Return True once the registered local HTTP endpoint responds."""
    if not _port_is_valid(port):
        return False
    try:
        urllib.request.urlopen(_app_url(port), timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _launch_status(app_id: str) -> dict:
    """Return launch readiness for a registered app."""
    entry = registry.get(app_id)
    if entry is None:
        return {
            "ready": False,
            "state": "missing",
            "message": "This app is no longer registered.",
        }
    payload = {
        "ready": False,
        "state": "starting",
        "name": entry.name,
        "port": entry.port,
        "url": _app_url(entry.port),
        "message": f"Waiting for {entry.name} on port {entry.port}...",
    }
    if not _port_is_valid(entry.port):
        payload["state"] = "invalid_port"
        payload["message"] = "The registered port is invalid."
        return payload
    if not process.is_running(app_id):
        payload["state"] = "not_running"
        payload["message"] = f"Waiting for {entry.name} to start..."
        return payload
    if _probe_app_port(entry.port):
        payload["ready"] = True
        payload["state"] = "ready"
        payload["message"] = f"{entry.name} is ready."
        return payload
    return payload


def _launch_url(app_id: str) -> str:
    encoded = urllib.parse.quote(app_id, safe="")
    return f"http://127.0.0.1:{_help_server_start()}/launch/{encoded}"


def _open_launch_page(app_id: str) -> None:
    webbrowser.open(_launch_url(app_id))


# ── Closure factories (avoid lambda capture bugs) ─────────────────────────────

def _open_app_ui(app_id: str) -> None:
    """Present a running app's UI, honoring the resolved launch mode.

    In shell mode Appistry owns a dedicated native window (opened from its own
    venv via ygg_shell, non-blocking so the tray loop is never held); in browser
    mode (the default) the historical readiness/launch page is opened. On any
    failure resolving the entry, fall back to the browser launch page.
    """
    if launch.is_shell_mode():
        entry = registry.get(app_id)
        if entry is not None:
            launch.open_dedicated_window(entry, block=False)
            return
    _open_launch_page(app_id)


def _make_open(app_id: str, port: int):
    def _open(_sender):
        _open_app_ui(app_id)
    return _open


def _app_matches_search(entry: "registry.AppEntry", query: str) -> bool:
    """Return whether a local search query matches an app's name or id."""
    normalized = (query or "").strip().casefold()
    if not normalized:
        return True
    return normalized in entry.name.casefold() or normalized in entry.id.casefold()


class _SearchFieldController(NSObject):
    """Bridge native AppKit search and menu events back to AppistryApp."""

    def initWithApp_(self, app):
        self = objc.super(_SearchFieldController, self).init()
        if self is None:
            return None
        self._app = app
        return self

    def search_(self, sender):
        self._app._search_changed(str(sender.stringValue()))

    @objc.typedSelector(b"B@:@@:")
    def control_textView_doCommandBySelector_(self, control, _text_view, command):
        command_name = str(command)
        if command_name == "insertNewline:":
            self._app._search_changed(str(control.stringValue()))
            self._app._open_first_search_match()
            return True
        if command_name == "cancelOperation:" and str(control.stringValue()):
            self._app._clear_search()
            return True
        return False

    def menuWillOpen_(self, _menu):
        self._app._menu_will_open()

    def menuDidClose_(self, _menu):
        self._app._menu_did_close()


def _make_search_menu_item(controller, query: str):
    """Build a native, accessible NSSearchField inside a rumps menu item."""
    from AppKit import NSMakeRect, NSSearchField, NSView

    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 256, 32))
    field = NSSearchField.alloc().initWithFrame_(NSMakeRect(8, 4, 240, 24))
    field.setPlaceholderString_(_SEARCH_LABEL)
    field.setAccessibilityLabel_(_SEARCH_LABEL)
    field.setSendsSearchStringImmediately_(True)
    field.setTarget_(controller)
    field.setAction_("search:")
    field.setDelegate_(controller)
    field.setStringValue_(query)
    container.addSubview_(field)

    item = rumps.MenuItem(_SEARCH_LABEL)
    item._menuitem.setView_(container)
    item._menuitem.setEnabled_(True)
    return item, field



def _make_stop(app_id: str, app: "AppistryApp"):
    def _stop(_sender):
        process.stop(app_id)
        app._build_menu()
    return _stop


def _make_restart(app_id: str, entry: "registry.AppEntry", app: "AppistryApp"):
    def _restart(_sender):
        # In shell mode the old dedicated window self-closed via its watchdog
        # when the server stopped, and the per-launch secret is one-shot, so a
        # fresh window must be opened after the server is back up. process.start
        # mints a new secret; open_dedicated_window reads it. In browser mode
        # keep the historical "open launch page, then restart" behavior.
        shell = launch.is_shell_mode()
        if not shell:
            _open_launch_page(app_id)
        process.stop(app_id)
        process.start(entry)
        if shell:
            launch.open_dedicated_window(entry, block=False)
        app._build_menu()
    return _restart


def _add_alternate(parent: "rumps.MenuItem", primary: "rumps.MenuItem", alternate: "rumps.MenuItem"):
    """Add primary to parent, then add alternate marked as an Option-key alternate."""
    from AppKit import NSEventModifierFlagOption
    parent.add(primary)
    alternate._menuitem.setAlternate_(True)
    alternate._menuitem.setKeyEquivalentModifierMask_(NSEventModifierFlagOption)
    parent.add(alternate)


def _make_browse(entry: "registry.AppEntry", app: "AppistryApp"):
    def _browse(_sender):
        _open_launch_page(entry.id)
        if not process.is_running(entry.id):
            process.start(entry)
            app._build_menu()
    return _browse


def _make_about(name: str, about_path: Path):
    def _about(_sender):
        import markdown, nh3, tempfile
        # about.md is registry-controlled content (any registered app can
        # supply one), so its rendered HTML must be sanitized before it's
        # written to disk and opened in a browser — otherwise a malicious
        # app could ship a <script> or javascript: link in its about.md.
        raw_body = markdown.markdown(about_path.read_text(encoding="utf-8"),
                                     extensions=["fenced_code"])
        body = nh3.clean(
            raw_body,
            tags={"h1", "h2", "p", "em", "strong", "code", "pre", "a",
                  "ul", "ol", "li", "blockquote", "br"},
            attributes={"a": {"href", "title"}},
            url_schemes={"https"},
            link_rel="noopener noreferrer",
        )
        safe_name = html.escape(name)
        doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>About {safe_name}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           max-width: 520px; margin: 60px auto; padding: 0 24px;
           color: #1d1d1f; line-height: 1.6; }}
    h1   {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 0.2em; }}
    h1 + p {{ margin-top: 0; color: #6e6e73; }}
    h2   {{ font-size: 0.95rem; font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.05em; color: #6e6e73; margin-top: 2em; }}
    p    {{ margin-top: 0.5em; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #1c1c1e; color: #f2f2f7; }}
      h1 + p, h2 {{ color: #98989d; }}
    }}
  </style>
</head>
<body>{body}</body>
</html>"""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write(doc)
        tmp.close()
        webbrowser.open(f"file://{tmp.name}")
    return _about


# ── Help server ──────────────────────────────────────────────────────────────

_HELP_IDLE_SECS = 600  # shut server down after 10 min of inactivity

_help_server: "socketserver.TCPServer | None" = None
_help_server_port: int = 0
_help_idle_timer: "threading.Timer | None" = None
_help_server_lock = threading.Lock()
_hook_server: "socketserver.TCPServer | None" = None
_hook_server_port: int = 0
_hook_server_lock = threading.Lock()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch_icon_path(app_id: str) -> "Path | None":
    entry = registry.get(app_id)
    if entry is None or not entry.icon:
        return None
    icon = Path(entry.icon)
    if icon.is_absolute() or icon.suffix.lower() not in _LAUNCH_ICON_TYPES:
        return None
    try:
        base = Path(entry.cwd).resolve()
        resolved = (base / icon).resolve()
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


def _render_launch_page(app_id: str) -> "str | None":
    entry = registry.get(app_id)
    if entry is None:
        return None

    app_name = html.escape(entry.name)
    port = html.escape(str(entry.port))
    app_id_json = json.dumps(app_id)
    icon_path = _launch_icon_path(app_id)
    if icon_path is not None:
        icon_url = f"/launch-icon/{urllib.parse.quote(app_id, safe='')}"
        icon_markup = f'<img class="app-icon" src="{icon_url}" alt="">'
    else:
        initial = html.escape((entry.name.strip() or entry.id or "?")[0].upper())
        icon_markup = f'<div class="app-icon fallback" aria-hidden="true">{initial}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Opening {app_name}</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f7f8;
      color: #172126;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      padding: 28px;
    }}
    main {{
      width: min(460px, 100%);
      display: grid;
      gap: 22px;
      justify-items: center;
      text-align: center;
    }}
    .app-icon {{
      width: 96px;
      height: 96px;
      border-radius: 22px;
      object-fit: cover;
      box-shadow: 0 18px 42px rgba(28, 45, 56, 0.18);
    }}
    .fallback {{
      display: grid;
      place-items: center;
      background: #1f6f68;
      color: white;
      font-size: 44px;
      font-weight: 700;
    }}
    h1 {{
      margin: 0;
      font-size: 30px;
      font-weight: 720;
      line-height: 1.15;
      letter-spacing: 0;
    }}
    .status {{
      display: grid;
      gap: 8px;
      justify-items: center;
      min-height: 56px;
    }}
    .status-line {{
      display: inline-grid;
      grid-template-columns: 12px minmax(0, 1fr);
      align-items: center;
      gap: 10px;
      max-width: 100%;
      color: #405158;
      font-size: 15px;
    }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: #2f8f83;
      animation: pulse 1.1s ease-in-out infinite;
    }}
    .detail {{
      margin: 0;
      color: #62727a;
      font-size: 13px;
    }}
    .actions {{
      display: flex;
      min-height: 38px;
      gap: 10px;
      justify-content: center;
      flex-wrap: wrap;
    }}
    button, a {{
      border: 1px solid #b8c6c9;
      border-radius: 7px;
      background: #fff;
      color: #172126;
      padding: 9px 14px;
      font: inherit;
      text-decoration: none;
      cursor: pointer;
    }}
    .hidden {{ display: none; }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 0.35; transform: scale(0.88); }}
      50% {{ opacity: 1; transform: scale(1); }}
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{ background: #101416; color: #eef3f4; }}
      .status-line {{ color: #c0cccf; }}
      .detail {{ color: #90a0a5; }}
      button, a {{
        background: #182023;
        color: #eef3f4;
        border-color: #3b494e;
      }}
    }}
  </style>
</head>
<body>
  <main>
    {icon_markup}
    <h1>Opening {app_name}</h1>
    <section class="status" aria-live="polite">
      <div class="status-line">
        <span id="dot" class="dot"></span>
        <span id="status-text">Starting {app_name}...</span>
      </div>
      <p id="detail" class="detail">Checking port {port}</p>
    </section>
    <div class="actions">
      <a id="open-anyway" class="hidden" href="#" rel="noopener">Open anyway</a>
      <button id="retry" class="hidden" type="button">Retry</button>
    </div>
  </main>
  <script>
    const appId = {app_id_json};
    const timeoutMs = {_LAUNCH_TIMEOUT_SECONDS * 1000};
    const pollMs = {_LAUNCH_POLL_MS};
    const startedAt = Date.now();
    let latestUrl = "";

    const statusEl = document.getElementById("status-text");
    const detailEl = document.getElementById("detail");
    const openAnyway = document.getElementById("open-anyway");
    const retry = document.getElementById("retry");
    const dot = document.getElementById("dot");

    function showTimeout() {{
      statusEl.textContent = "Still waiting for {app_name}";
      detailEl.textContent = latestUrl ? "The app has not responded yet." : "No launch URL is available.";
      dot.style.animation = "none";
      dot.style.background = "#b36b18";
      if (latestUrl) {{
        openAnyway.href = latestUrl;
        openAnyway.classList.remove("hidden");
      }}
      retry.classList.remove("hidden");
    }}

    async function poll() {{
      try {{
        const response = await fetch(`/api/launch/${{encodeURIComponent(appId)}}/ready`, {{cache: "no-store"}});
        const data = await response.json();
        if (data.url) latestUrl = data.url;
        if (data.message) statusEl.textContent = data.message;
        detailEl.textContent = data.port ? `Checking port ${{data.port}}` : "";
        if (data.ready && data.url) {{
          window.location.replace(data.url);
          return;
        }}
      }} catch (error) {{
        statusEl.textContent = "Checking Appistry...";
        detailEl.textContent = "The launcher is still waiting.";
      }}
      if (Date.now() - startedAt >= timeoutMs) {{
        showTimeout();
        return;
      }}
      window.setTimeout(poll, pollMs);
    }}

    retry.addEventListener("click", () => window.location.reload());
    poll();
  </script>
</body>
</html>"""


def _hook_proxy_target(app_id: str, target_path: str, query: str = "") -> tuple[int, str | None, str | None]:
    try:
        app_id = registry.validate_app_id(app_id)
    except ValueError:
        return 404, None, "Registered app not found."
    entry = registry.get(app_id)
    if entry is None:
        return 404, None, "Registered app not found."
    if not _port_is_valid(entry.port):
        return 502, None, "Registered app port is invalid."
    if not process.is_running(app_id):
        return 503, None, f"{entry.name} is not running."

    path = "/" + target_path.lstrip("/")
    return 200, urllib.parse.urlunsplit((
        "http",
        f"127.0.0.1:{entry.port}",
        path,
        query,
        "",
    )), None


def _hook_headers(headers) -> dict[str, str]:
    return {
        key: value for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }


def _hook_path_parts(path: str) -> tuple[str, str] | None:
    if not path.startswith("/hooks/"):
        return None
    rest = path[len("/hooks/"):]
    app_id, sep, target = rest.partition("/")
    if not app_id or not sep:
        return None
    return urllib.parse.unquote(app_id), "/" + target


def _relay_upstream_response(handler: http.server.BaseHTTPRequestHandler, response, data: bytes) -> None:
    handler.send_response(getattr(response, "status", getattr(response, "code", 502)))
    for key, value in response.headers.items():
        if key.lower() not in _HOP_BY_HOP_HEADERS:
            handler.send_header(key, value)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class _NoHookRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HOOK_OPENER = urllib.request.build_opener(_NoHookRedirect)


def _hook_urlopen(request: urllib.request.Request):
    return _HOOK_OPENER.open(request, timeout=_HOOK_PROXY_TIMEOUT)


def _hook_server_start() -> int:
    """Start the stable hook proxy. Returns 0 if the fixed port is unavailable."""
    global _hook_server, _hook_server_port
    with _hook_server_lock:
        if _hook_server is not None:
            return _hook_server_port
        port = hooks.hook_port()

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass  # silence access log

            def do_GET(self):
                self._proxy()

            def do_POST(self):
                self._proxy()

            def do_PUT(self):
                self._proxy()

            def do_PATCH(self):
                self._proxy()

            def do_DELETE(self):
                self._proxy()

            def _proxy(self):
                parsed = urllib.parse.urlparse(self.path)
                parts = _hook_path_parts(parsed.path)
                if parts is None:
                    self.send_error(404)
                    return
                app_id, target_path = parts
                status, target_url, message = _hook_proxy_target(app_id, target_path, parsed.query)
                if target_url is None:
                    self._json(status, {"error": message or "Hook target unavailable."})
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    self._json(400, {"error": "Invalid Content-Length."})
                    return
                if length > _HOOK_MAX_BODY:
                    self._json(413, {"error": "Hook request body is too large."})
                    return
                body = self.rfile.read(length) if length else None
                request = urllib.request.Request(
                    target_url,
                    data=body,
                    headers=_hook_headers(self.headers),
                    method=self.command,
                )
                try:
                    with _hook_urlopen(request) as response:
                        data = response.read()
                        _relay_upstream_response(self, response, data)
                except urllib.error.HTTPError as exc:
                    data = exc.read()
                    _relay_upstream_response(self, exc, data)
                except Exception:
                    # OAuth callbacks carry short-lived credentials in their query
                    # strings. Log only the validated registry slug, never the URL.
                    log.warning(
                        "Stable hook proxy failed for app_id=%s",
                        app_id,
                        exc_info=True,
                    )
                    self._json(502, {"error": "Hook target did not respond."})

            def _json(self, status: int, payload: dict):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class _ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            srv = _ThreadedServer(("127.0.0.1", port), _Handler)
        except OSError:
            log.warning("Stable hook proxy port %d is unavailable", port, exc_info=True)
            return 0
        _hook_server = srv
        _hook_server_port = port
        registry.APPISTRY_DIR.mkdir(parents=True, exist_ok=True)
        hooks.hook_port_path().write_text(str(port), encoding="utf-8")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return port


def _hook_server_shutdown() -> None:
    global _hook_server, _hook_server_port
    with _hook_server_lock:
        port = _hook_server_port
        if _hook_server is not None:
            _hook_server.shutdown()
            _hook_server = None
            _hook_server_port = 0
        try:
            if hooks.hook_port_path().read_text(encoding="utf-8").strip() == str(port):
                hooks.hook_port_path().unlink()
        except OSError:
            pass


def _help_server_start() -> int:
    """Ensure the help HTTP server is running. Returns its port."""
    global _help_server, _help_server_port
    with _help_server_lock:
        if _help_server is not None:
            _reset_idle_timer()
            return _help_server_port
        port = _free_port()

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass  # silence access log

            def do_GET(self):
                _reset_idle_timer()
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path in ("/", ""):
                    page = _render_help_page()
                    data = page.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path.startswith("/launch/"):
                    app_id = urllib.parse.unquote(path[len("/launch/"):])
                    page = _render_launch_page(app_id)
                    if page is None:
                        self.send_error(404)
                        return
                    data = page.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path.startswith("/launch-icon/"):
                    app_id = urllib.parse.unquote(path[len("/launch-icon/"):])
                    icon_path = _launch_icon_path(app_id)
                    if icon_path is None:
                        self.send_error(404)
                        return
                    data = icon_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", _LAUNCH_ICON_TYPES[icon_path.suffix.lower()])
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path.startswith("/api/launch/") and path.endswith("/ready"):
                    app_id = urllib.parse.unquote(path[len("/api/launch/"):-len("/ready")])
                    self._json(200, _launch_status(app_id))
                elif path == "/api/status":
                    self._json(200, {"service": "appistry", "ok": True})
                else:
                    self.send_error(404)

            def _json(self, status: int, payload: dict):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class _ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        registry.APPISTRY_DIR.mkdir(parents=True, exist_ok=True)
        _HELP_PORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        srv = _ThreadedServer(("127.0.0.1", port), _Handler)
        try:
            _HELP_PORT_PATH.write_text(str(port), encoding="utf-8")
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        except Exception:
            srv.server_close()
            try:
                _HELP_PORT_PATH.unlink()
            except OSError:
                pass
            raise
        _help_server = srv
        _help_server_port = port
        _reset_idle_timer()
        return port


def _reset_idle_timer():
    global _help_idle_timer
    if _help_idle_timer is not None:
        _help_idle_timer.cancel()
    _help_idle_timer = threading.Timer(_HELP_IDLE_SECS, _help_server_shutdown)
    _help_idle_timer.daemon = True
    _help_idle_timer.start()


def _help_server_shutdown():
    global _help_server, _help_server_port, _help_idle_timer
    with _help_server_lock:
        port = _help_server_port
        if _help_server is not None:
            _help_server.shutdown()
            _help_server.server_close()
            _help_server = None
            _help_server_port = 0
        try:
            if _HELP_PORT_PATH.read_text(encoding="utf-8").strip() == str(port):
                _HELP_PORT_PATH.unlink()
        except OSError:
            pass
        _help_idle_timer = None


def _render_help_page() -> str:
    import markdown as _md
    src  = HERE / "help.md"
    body = _md.markdown(src.read_text(), extensions=["fenced_code"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Appistry Help</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           max-width: 560px; margin: 60px auto; padding: 0 24px;
           color: #1d1d1f; line-height: 1.6; }}
    h1   {{ font-size: 1.6rem; font-weight: 700; }}
    h2   {{ font-size: 1rem; font-weight: 600; margin-top: 2em; }}
    code, pre {{ font-family: "SF Mono", Menlo, monospace; font-size: 0.88rem; }}
    pre  {{ background: #f5f5f7; border-radius: 8px; padding: 12px 16px; }}
    code {{ background: #e5e5ea; border-radius: 4px; padding: 1px 5px; }}
    pre code {{ background: none; padding: 0; }}
    blockquote {{ background: #fff8e1; border-left: 3px solid #f5a623;
                  border-radius: 0 6px 6px 0; margin: 1em 0; padding: 10px 14px; }}
    blockquote p {{ margin: 0; }}
    hr {{ border: none; border-top: 1px solid #d1d1d6; margin: 1.5em 0; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #1c1c1e; color: #f2f2f7; }}
      pre  {{ background: #2c2c2e; }}
      code {{ background: #3a3a3c; }}
      blockquote {{ background: #2c2c1e; }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


# ── App ───────────────────────────────────────────────────────────────────────

def _app_about_path(entry: "registry.AppEntry") -> "Path | None":
    yggdrasil_about = Path(entry.cwd) / ".yggdrasil" / "about.md"
    fallback_about = Path(entry.cwd) / "about.md"
    if yggdrasil_about.exists():
        return yggdrasil_about
    if fallback_about.exists():
        return fallback_about
    return None


def _menu_state():
    """Return current menu records and a signature used to avoid idle rebuilds."""
    records = []
    signature = []
    for entry in registry.load():
        running = process.is_running(entry.id)
        about_path = _app_about_path(entry)
        records.append((entry, running, about_path))
        signature.append((
            entry.id,
            entry.name,
            entry.cwd,
            entry.port,
            entry.github_url,
            entry.icon,
            running,
            str(about_path) if about_path else "",
        ))
    return records, tuple(signature)


class AppistryApp(rumps.App):
    def __init__(self):
        _hook_server_start()
        icon_path = str(HERE / "appistry_icon.png")
        super().__init__("", icon=icon_path, template=True, quit_button=None)
        self._search_query = ""
        self._search_field = None
        self._running_menu_items = []
        self._no_search_results = None
        self._menu_signature = None
        self._search_controller = _SearchFieldController.alloc().initWithApp_(self)
        self.menu._menu.setDelegate_(self._search_controller)
        # Seed with bundles that exist right now — bundles that were never built
        # (or are mid-rebuild) are excluded so they never trigger cleanup.
        self._known_bundles: set[str] = {
            e.id for e in registry.load()
            if (Path("/Applications") / f"{registry.bundle_name_for(e.name, e.id)}.app").is_dir()
        }
        self._bundle_missing_since: dict[str, float] = {}
        self._build_menu()

    def _build_menu(self, state=None):
        """Rebuild the menu from the current registry state, showing only running apps."""
        records, signature = state or _menu_state()
        items = []
        self._running_menu_items = []
        self._no_search_results = None
        self._search_field = None

        running_items = []
        for entry, running, about_path in records:
            if not running:
                continue

            item = rumps.MenuItem(entry.name)
            item.add(rumps.MenuItem("Open ↗", callback=_make_open(entry.id, entry.port)))
            stop    = rumps.MenuItem("Stop",    callback=_make_stop(entry.id, self))
            restart = rumps.MenuItem("Restart", callback=_make_restart(entry.id, entry, self))
            _add_alternate(item, stop, restart)

            if about_path is not None:
                item.add(None)  # separator before About
                about = rumps.MenuItem("About", callback=_make_about(entry.name, about_path))
                if entry.github_url:
                    github = rumps.MenuItem("GitHub ↗", callback=lambda _, u=entry.github_url: webbrowser.open(u))
                    _add_alternate(item, about, github)
                else:
                    item.add(about)

            running_items.append(item)
            self._running_menu_items.append((entry, item))

        if running_items:
            header = rumps.MenuItem("Running apps")
            header.set_callback(None)
            items.append(header)
            search_item, self._search_field = _make_search_menu_item(
                self._search_controller,
                self._search_query,
            )
            items.append(search_item)
            items.extend(running_items)
            self._no_search_results = rumps.MenuItem("No running apps match")
            self._no_search_results.set_callback(None)
            items.append(self._no_search_results)

        items.append(None)  # separator

        yggdrasil = next((entry for entry, _, _ in records if entry.id == "yggdrasil"), None)
        if yggdrasil:
            browse = rumps.MenuItem(
                "Browse Apps ↗",
                callback=_make_browse(yggdrasil, self),
            )
            items.append(browse)
            items.append(None)  # separator

        help_item = rumps.MenuItem("Help", callback=self._open_help)
        items.append(help_item)

        items.append(None)  # separator

        quit_all = rumps.MenuItem("Quit All", callback=self._quit_all)
        items.append(quit_all)

        items.append(None)  # separator

        quit_app = rumps.MenuItem("Quit Appistry", callback=self._quit_appistry)
        items.append(quit_app)

        self.menu.clear()
        self.menu.update(items)
        self._menu_signature = signature
        self._apply_search_filter()

    def _search_changed(self, query: str):
        self._search_query = query
        self._apply_search_filter()

    def _open_first_search_match(self):
        for entry, _item in self._running_menu_items:
            if _app_matches_search(entry, self._search_query):
                _open_launch_page(entry.id)
                return True
        return False

    def _apply_search_filter(self):
        visible = 0
        for entry, item in self._running_menu_items:
            matches = _app_matches_search(entry, self._search_query)
            item.hidden = not matches
            visible += int(matches)
        if self._no_search_results is not None:
            self._no_search_results.hidden = not (
                self._search_query.strip() and visible == 0
            )

    def _menu_will_open(self):
        if self._search_field is None:
            return
        window = self._search_field.window()
        if window is not None:
            window.makeFirstResponder_(self._search_field)

    def _menu_did_close(self):
        self._clear_search()

    def _clear_search(self):
        self._search_query = ""
        if self._search_field is not None:
            self._search_field.setStringValue_("")
        self._apply_search_filter()

    @rumps.timer(5)
    def _poll(self, _sender):
        """Refresh menu state, watch for deleted app bundles, and pick up env changes."""
        _refresh_zshenv()
        self._check_removed_bundles()
        state = _menu_state()
        if state[1] != self._menu_signature:
            self._build_menu(state)

    def _check_removed_bundles(self):
        now = time.monotonic()
        for entry in registry.load():
            bundle = Path("/Applications") / f"{registry.bundle_name_for(entry.name, entry.id)}.app"
            if bundle.is_dir():
                # Bundle present — welcome it into the known set, clear any timer
                self._known_bundles.add(entry.id)
                self._bundle_missing_since.pop(entry.id, None)
            elif entry.id in self._known_bundles:
                # Bundle was known but is now gone — start or check the timer
                missing_since = self._bundle_missing_since.setdefault(entry.id, now)
                if now - missing_since >= _BUNDLE_GRACE_SECONDS:
                    self._handle_removed(entry)

    def _handle_removed(self, entry: "registry.AppEntry"):
        self._known_bundles.discard(entry.id)
        self._bundle_missing_since.pop(entry.id, None)

        if process.is_running(entry.id):
            process.stop(entry.id)

        cwd = Path(entry.cwd)
        cleaned = cleanup.git_clean_project(cwd)

        registry.remove(entry.id)

        msg = (
            f"{entry.name} removed and project files cleaned up. "
            "Your data and changes are preserved."
            if cleaned else
            f"{entry.name} removed. Project files were not a clean git repo — left untouched."
        )
        _osascript_notify("Appistry", msg)

    def _open_help(self, _sender):
        port = _help_server_start()
        webbrowser.open(f"http://127.0.0.1:{port}/")

    def _quit_all(self, _sender):
        """Stop all running apps, then quit."""
        for entry in registry.load():
            if process.is_running(entry.id):
                process.stop(entry.id)
        _hook_server_shutdown()
        _help_server_shutdown()
        _release_lock()
        rumps.quit_application()

    def _quit_appistry(self, _sender):
        """Quit the menu bar app without stopping registered apps."""
        _hook_server_shutdown()
        _help_server_shutdown()
        _release_lock()
        rumps.quit_application()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__" and _IS_MACOS:
    import traceback, logging
    from logging.handlers import RotatingFileHandler
    log_path = Path.home() / ".appistry" / "menubar.log"
    handler = RotatingFileHandler(log_path, maxBytes=512_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)
    try:
        if not _acquire_lock():
            sys.exit(0)

        # Suppress Dock icon for non-framework Python builds where LSUIElement
        # in Info.plist alone isn't enough. Must be called before rumps creates
        # the run loop, but NSApplication.sharedApplication() must exist first.
        from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyProhibited
        )

        AppistryApp().run()
    except Exception:
        logging.critical(traceback.format_exc())
        raise

if __name__ == "__main__" and not _IS_MACOS:
    from windows_tray import main as _windows_main

    _windows_main()
