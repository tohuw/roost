#!/usr/bin/env python3
"""
ygg_shell.py — shared native-window host for Yggdrasil apps.

Renders a Yggdrasil app's existing loopback web UI in a dedicated, chromeless
native window instead of a browser tab. It uses the operating system's own
webview engine via pywebview:

  - macOS   → WKWebView (WebKit)
  - Windows → WebView2 (Edge/Chromium, evergreen)
  - Linux   → WebKitGTK

No browser engine is bundled — the OS patches the web engine, not us. This is
the durable, secure shape: one shared host, driven entirely by CLI arguments,
that any app can point at without changing its server or front-end.

The app itself is unchanged: it keeps serving the same `http://127.0.0.1:PORT`
UI. This host only decides *how that URL is presented*.

Usage:
    python ygg_shell.py --url http://127.0.0.1:8000 --title "My App" \
        [--icon path/to/icon.png] [--wait-timeout 20] [--width 1280] [--height 860]

Launch secret (optional, opt-in hardening):
    If the environment variable YGG_LAUNCH_SECRET is set, this host appends it
    to the URL as a fragment (`#ygg_launch=<secret>`). Fragments are never sent
    to the server and never appear in server access logs; the app's front-end
    reads it synchronously at load and presents it when bootstrapping its
    session. This makes the dedicated window the only client that can obtain a
    session token, without weakening the plain-browser path (where the secret
    is simply absent and the server stays permissive).

Exit codes:
    0  window closed normally
    2  the target URL never became ready within --wait-timeout
    3  no OS webview backend is available (caller should fall back to browser)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request


def _wait_until_ready(url: str, timeout: float) -> bool:
    """Poll the loopback URL until it responds, or timeout elapses.

    Mirrors the readiness probe the browser launchers already use, so the
    native window never opens against a server that is still starting up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                # Any HTTP response means the server is accepting connections.
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError:
            # 4xx still means the server is up and answering.
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.3)
    return False


def _url_with_secret(url: str, in_query: bool = False) -> str:
    """Append the launch secret to the URL when one is present.

    Two carriage modes, chosen per app by how it consumes the secret:

    - Fragment (default): appended as ``#ygg_launch=...``. Fragments are
      client-only — not transmitted to the server and never in access logs —
      so this suits apps whose own front-end JS reads the fragment and presents
      the secret via a header.

    - Query (``in_query=True``): appended as ``?ygg_launch=...``. Needed for
      apps whose server must read the secret off the document request itself —
      e.g. a prebuilt SPA that cannot be modified to read the fragment, where
      the server pre-authenticates the document response.

    When no secret is set (the default browser path), the URL is unchanged.
    """
    secret = os.environ.get("YGG_LAUNCH_SECRET", "").strip()
    if not secret:
        return url
    if in_query:
        sep = "&" if "?" in url.split("#", 1)[0] else "?"
        return f"{url}{sep}ygg_launch={secret}"
    sep = "&" if "#" in url else "#"
    return f"{url}{sep}ygg_launch={secret}"


def _apply_macos_click_fix() -> None:
    """Make the webview accept the click that also focuses the window (macOS).

    pywebview's WKWebView subclass does not override ``acceptsFirstMouse:``, so
    it inherits the AppKit default of NO. That means when the dedicated window
    is not the key window (e.g. the user is focused on their terminal or
    editor), the first click on it is consumed just to activate the window and
    never reaches the button underneath — so buttons appear dead until a
    second click. Returning YES lets that first click act on the control
    directly, which is the expected behavior for a single-purpose app window.

    Best-effort: any failure here is non-fatal — the window still opens.
    """
    if sys.platform != "darwin":
        return
    try:
        import objc  # type: ignore
        import webview.platforms.cocoa as cocoa  # type: ignore

        host = cocoa.BrowserView.WebKitHost
        if host.instancesRespondToSelector_("acceptsFirstMouse:"):
            return

        def acceptsFirstMouse_(self, event):  # noqa: N802 - ObjC selector name
            return True

        objc.classAddMethods(
            host,
            [objc.selector(acceptsFirstMouse_, selector=b"acceptsFirstMouse:", signature=b"B24@0:8@16")],
        )
    except Exception:
        pass


def _apply_macos_no_activate() -> None:
    """Open the window in the background without stealing foreground focus.

    Sets the process to the "accessory" activation policy so showing the
    window does not pull focus away from whatever the user is working in. The
    window still appears and is fully interactive; it just does not steal the
    foreground. Best-effort and macOS-only.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit  # type: ignore

        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def _is_server_up(url: str) -> bool:
    """Single connectivity probe against the loopback URL."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status < 500
    except urllib.error.HTTPError:
        return True  # answering, even if 4xx
    except (urllib.error.URLError, ConnectionError, OSError):
        return False


def _start_server_watchdog(window, url: str, grace_failures: int = 3, interval: float = 1.0):
    """Close the window when its server goes away.

    The app's server is a separate process that this host does not own —
    Appistry (or the user) can stop or restart it, or it can crash. When that
    happens the window would otherwise linger, pointing at a dead port. This
    watchdog polls the server and, after `grace_failures` consecutive failed
    probes (a short grace period so a quick restart blip does not close a
    healthy window), destroys the window so no dead window is left behind.

    Runs on a daemon thread; all failures are swallowed so the watchdog can
    never crash the UI.
    """
    import threading

    def _watch():
        misses = 0
        while True:
            time.sleep(interval)
            if _is_server_up(url):
                misses = 0
                continue
            misses += 1
            if misses >= grace_failures:
                try:
                    window.destroy()
                except Exception:
                    pass
                return

    t = threading.Thread(target=_watch, name="ygg-shell-watchdog", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ygg_shell",
        description="Render a Yggdrasil app's loopback UI in a native window.",
    )
    parser.add_argument("--url", required=True, help="Loopback URL to display, e.g. http://127.0.0.1:8000")
    parser.add_argument("--title", default="Yggdrasil App", help="Window title")
    parser.add_argument("--icon", default=None, help="Path to a window/app icon (optional)")
    parser.add_argument("--wait-timeout", type=float, default=20.0, help="Seconds to wait for the server to become ready")
    parser.add_argument("--width", type=int, default=1280, help="Initial window width")
    parser.add_argument("--height", type=int, default=860, help="Initial window height")
    parser.add_argument(
        "--no-activate",
        action="store_true",
        help="Open the window without stealing focus from the foreground app.",
    )
    parser.add_argument(
        "--secret-in-query",
        action="store_true",
        help=(
            "Carry the launch secret as a ?ygg_launch= query param instead of a "
            "URL fragment. Use for apps whose server reads the secret off the "
            "document request (e.g. a prebuilt SPA that cannot read the fragment)."
        ),
    )
    args = parser.parse_args(argv)

    # Import pywebview lazily so a missing/broken backend becomes a clean
    # fallback signal (exit 3) rather than an import crash at module load.
    try:
        import webview  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"ygg_shell: no webview backend available ({exc})", file=sys.stderr)
        return 3

    # Confirm a concrete OS backend actually resolves before we wait on the
    # server; if not, tell the caller to fall back to the browser immediately.
    try:
        webview.initialize()
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"ygg_shell: webview backend failed to initialize ({exc})", file=sys.stderr)
        return 3

    # Let the window accept the click that also focuses it, so buttons work on
    # the first click even when the window is not frontmost.
    _apply_macos_click_fix()
    if args.no_activate:
        _apply_macos_no_activate()

    if not _wait_until_ready(args.url, args.wait_timeout):
        print(
            f"ygg_shell: {args.url} did not become ready within "
            f"{args.wait_timeout:g}s",
            file=sys.stderr,
        )
        return 2

    target = _url_with_secret(args.url, in_query=args.secret_in_query)

    create_kwargs = {
        "title": args.title,
        "url": target,
        "width": args.width,
        "height": args.height,
        "min_size": (720, 560),
        # The app's own CSP/headers still apply; this is just a native frame.
        # text_select=True also avoids pywebview injecting a `user-select:none`
        # inline <style>, which a strict app CSP (no 'unsafe-inline' in
        # style-src) would otherwise block with a style-src-elem violation.
        "text_select": True,
    }

    window = webview.create_window(**create_kwargs)

    # Bind the window's lifetime to the server's: once the UI loop is running,
    # start a watchdog that closes the window if the server goes away (Appistry
    # stop/restart or a crash), so no dead window lingers.
    def _on_start():
        _start_server_watchdog(window, args.url)

    start_kwargs: dict[str, object] = {}
    # An icon is only honored by some backends; pass it when supported and
    # the file exists, but never fail the launch over a missing icon.
    if args.icon and os.path.isfile(args.icon):
        start_kwargs["icon"] = args.icon

    try:
        webview.start(_on_start, **start_kwargs)
    except TypeError:
        # Older/newer signatures may not accept `icon`; retry without it
        # rather than deny the user a window.
        webview.start(_on_start)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
