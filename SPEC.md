# Appistry Integration Spec

**Version:** 1.0
**Audience:** Developers of apps that want to register with Appistry

---

## What Appistry Is

Appistry is a local app registry and native macOS/Windows tray manager for developer tools that
run as local web servers. It provides:

- A persistent registry at `~/.appistry/registry.toml`
- A CLI (`appistry`) for registering, starting, stopping, and opening apps
- A macOS menu bar or Windows system tray app that surfaces registered apps and manages their processes
- A branded launch page that opens immediately and redirects when the app is ready
- A stable loopback hook proxy for browser-return flows that need a fixed URL
- Auto-start via launchd on macOS or the current user's Startup folder on Windows

Apps integrate with Appistry by calling the CLI. Appistry has no knowledge of any
specific app — it is told what to do via registration.

---

## Registry Format

Appistry maintains `~/.appistry/registry.toml`. Implementors never write this file
directly — use the CLI. It is documented here for transparency only.

```toml
[[apps]]
id           = "notekeeper"
name         = "Notekeeper"
cwd          = "/Users/alice/Projects/Notekeeper"
command      = ".venv/bin/python ui/server.py"
port         = 8000
github_url   = "https://github.com/example/notekeeper"
icon         = "ui/static/notekeeper-logo.png"   # relative to cwd; optional
registered_at = "2026-03-27T11:00:00"
```

Fields:

| Field           | Required | Description                                                  |
|-----------------|----------|--------------------------------------------------------------|
| `id`            | yes      | Lowercase slug, auto-derived from `name` if omitted          |
| `name`          | yes      | Display name shown in the desktop tray menu                  |
| `cwd`           | yes      | Absolute path to the project root; commands run from here    |
| `command`       | yes      | Shell command to start the server, relative to `cwd`         |
| `port`          | yes      | Port the server listens on                                   |
| `github_url`    | yes      | HTTPS GitHub URL, resolved from the repo's `origin` remote  |
| `icon`          | no       | Path to a browser-renderable icon, relative to `cwd`         |
| `registered_at` | auto     | ISO 8601 timestamp set by `appistry register`                |

The icon should be PNG, JPEG, GIF, WebP, or ICO for the browser launch page. PNG
is preferred because it converts cleanly into macOS app-bundle and Windows shortcut icons.

---

## CLI Contract

All commands exit `0` on success. Non-zero exits are described per command.

```
appistry register   --name NAME --cwd PATH --command CMD --port PORT [--icon PATH] [--id ID]
appistry unregister ID
appistry list
appistry start      ID
appistry stop       ID
appistry open       ID
appistry launch     ID
appistry hook-url   ID /app/local/path
appistry migrate
appistry install
appistry uninstall
appistry ui
```

### `register`

Adds an entry to the registry. Idempotent: re-registering with the same `id` updates
the existing entry rather than erroring. Prints `Registered: {id}` on success.

**Requires a GitHub remote.** Registration fails if the project at `--cwd` has no
`origin` remote pointing to GitHub. This ensures every registered app has a discoverable
source and enables the GitHub ↗ menu action.

Exit codes:
- `0` registered or updated
- `1` invalid arguments or no GitHub remote found

### `unregister`

Removes an app from the registry. If the app is running, stops it first.

Exit codes:
- `0` removed
- `1` id not found

### `list`

Prints a human-readable table of all registered apps and their current state
(`running` / `stopped`). Suitable for terminal display; not machine-parseable.
For scripting, inspect `~/.appistry/registry.toml` directly.

### `start` / `stop` / `open` / `launch`

`start` — spawns the app's command as a background subprocess, writes the PID to
`~/.appistry/pids/{id}.pid`.

`stop` — on macOS, sends SIGTERM to the PID, waits up to 5 seconds, then SIGKILL
if needed. On Windows, terminates the recorded process tree, waits up to 5 seconds,
then kills any survivors. Removes the PID file on both platforms.

`open` — opens the Appistry launch page when the menu bar control server is
available, otherwise opens `http://localhost:{port}` directly. Does not start the
server if it is not already running.

`launch` — opens the readiness page and starts the app when it is stopped. Native
per-app launchers use this command.

`hook-url` — prints a stable local Appistry proxy URL for an app-local path. For
example, `appistry hook-url demo-app /api/oauth/callback` prints:

```text
http://127.0.0.1:47658/hooks/demo-app/api/oauth/callback
```

The app must already be registered. The target path must be local to the app; absolute
URLs are rejected.

Exit codes for start/stop/open/launch/hook-url:
- `0` success
- `1` id not found
- `2` already in the requested state (start when running, stop when stopped)

### `migrate`

Brings all registry entries up to the current Appistry spec. Currently backfills
`github_url` for apps registered before that field was introduced. Safe to re-run;
entries that are already current are skipped. Exits `1` if any entry could not be
migrated (e.g. its repo no longer has a GitHub remote).

### `install`

Performs first-time setup. On macOS it:

1. Creates a virtualenv at `<appistry_dir>/.venv` if absent
2. Installs Python dependencies (`rumps` and any others)
3. Writes `~/Library/LaunchAgents/com.appistry.menubar.plist`
4. Calls `launchctl load` on the plist
5. Starts the menu bar app immediately (so the user sees it without logging out)

Prints `Appistry installed and running.` on success. Safe to re-run; existing
installs are detected and skipped unless `--force` is passed.

On Windows it creates `<appistry_dir>\.venv`, installs the pinned runtime and
the `appistry.exe` console entry point, adds the venv's `Scripts` directory to
the current user's `PATH`, creates Appistry login/Start Menu shortcuts, rebuilds
registered-app shortcuts, and starts the system tray immediately. Windows support
requires Python 3.10 or newer.

### `uninstall`

Stops the tray app and all Appistry-managed child processes, removes platform login
startup and CLI integration, and removes Windows Start Menu shortcuts when applicable.
It does not touch the registry or project data.

### `ui`

Starts the native tray UI via launchd on macOS or detached `pythonw.exe` on Windows.
If the process is already running, prints a message and exits `0`.

---

## Launch Readiness Page

When a user opens a registered app from the Appistry menu, Appistry opens a local
launch page immediately instead of waiting silently for the app server. The page shows
the app name, icon or initials, and readiness state. It polls Appistry, not the app
directly; Appistry rereads the registry, probes the current `127.0.0.1:{port}` HTTP
endpoint, and redirects the browser once the endpoint responds.

This page is owned by Appistry. Apps do not implement their own splash page. Apps do
have to provide reliable launch metadata:

- Call `register(port=actual_port)` on startup after choosing the real port.
- Keep `APP_ICON` pointed at a browser-renderable relative path when an icon exists.
- Bind the web server to loopback and respond on the registered port once ready.
- If the process was launched by Appistry, do not open a second raw browser tab.

Appistry sets these environment variables for spawned app processes:

| Variable             | Value                         | Purpose                                |
|----------------------|-------------------------------|----------------------------------------|
| `APPISTRY_LAUNCHED`  | `1`                           | Suppress app-owned browser auto-open   |
| `APPISTRY_APP_ID`    | The registry id for the app   | Optional diagnostics and logging       |

Apps that can also be launched directly from a terminal may still open their own
browser tab when `APPISTRY_LAUNCHED` is absent.

The menu bar control server writes its current port to
`~/.appistry/menubar-http-port`. Consumers such as `appistry open` and any
catalog app may use this to prefer `http://127.0.0.1:{control_port}/launch/{app_id}`
and fall back to the raw app URL if the control server is unavailable.

## Stable Hook Proxy

Some local apps need a fixed browser-return URL even though their own web server port
changes at startup. OAuth integrations are the common case: the provider requires a
registered redirect URI, but the app chooses a free local port and re-registers that
port with Appistry on each launch.

Appistry starts a stable loopback proxy with the menu bar app. By default it listens on
`127.0.0.1:47658`; set `APPISTRY_HOOK_PORT` before launching Appistry to use a different
fixed port. The active hook port is also written to `~/.appistry/stable-hook-port`.

Hook URLs have this shape:

```text
http://127.0.0.1:{hook_port}/hooks/{app_id}/{app-local-path}
```

The proxy strips `/hooks/{app_id}` and forwards the request to the app's current
registered loopback port, preserving the method, query string, body, and ordinary
headers. Redirect responses are relayed back to the browser instead of followed
server-side. For example:

```text
http://127.0.0.1:47658/hooks/demo-app/api/oauth/callback?code=abc
```

forwards to:

```text
http://127.0.0.1:{current_app_port}/api/oauth/callback?code=abc
```

Security boundary:

- The proxy only targets apps present in the Appistry registry.
- The upstream host is always `127.0.0.1`; callers cannot provide an arbitrary URL.
- Invalid registered ports, stopped apps, and unreachable app servers return errors.
- Request bodies are capped at 1 MB.

This is for browser-return flows on the user's own machine. It is not a public webhook
ingress, because external services cannot reach a user's loopback address.

---

## launchd Auto-Start

`appistry install` writes this plist:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>com.appistry.menubar</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/.local/share/appistry/.venv/bin/python</string>
    <string>/Users/USERNAME/.local/share/appistry/menubar.py</string>
  </array>
  <key>RunAtLoad</key>         <true/>
  <key>KeepAlive</key>         <false/>
  <key>StandardOutPath</key>   <string>/Users/USERNAME/.appistry/menubar.log</string>
  <key>StandardErrorPath</key> <string>/Users/USERNAME/.appistry/menubar.log</string>
</dict>
</plist>
```

`USERNAME` is substituted with `os.environ["USER"]` at install time.

## Windows Startup and Native Launchers

`appistry install` creates two Appistry shortcuts through the Windows Shell COM
API, without invoking PowerShell or interpolating shell commands:

- `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Appistry.lnk`
  starts `windows_tray.py` through the venv's `pythonw.exe` after sign-in.
- `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Appistry\Appistry.lnk`
  lets the user start the tray manually from the Start Menu.

Every registered app also receives a contained, filename-sanitized `.lnk` in the
same `Programs\Appistry` folder. Its target is the venv's `pythonw.exe`; its
arguments call `appistry.py launch <validated-id>`. App icons are converted to
bounded ICO files under `~/.appistry/shortcut-icons/`. Relative icon paths must
remain inside the registered app's `cwd`.

The Windows tray uses a named mutex for single-instance behavior, refreshes the
current user's Environment registry values every five seconds, and owns the same
loopback help/readiness and stable-hook servers as the macOS menu bar app.

---

## Integration Guide for Apps

### Overview

The integration has three steps:

1. **Detect** — is `appistry` available?
2. **Offer to install** — if not, prompt the user (once) and clone + install
3. **Register** — tell Appistry about this app

All three steps should be gracefully skippable. If the user declines installation,
or if Appistry is unavailable for any reason, the app continues to work normally
(just without desktop tray integration).

### Locating the Binary

Appistry is considered available if either:

- `appistry` is on `PATH` (i.e., `shutil.which("appistry")` is not `None`), or
- the platform-specific explicit binary exists: `<APPISTRY_PATH>/appistry` on
  macOS or `<APPISTRY_PATH>\.venv\Scripts\appistry.exe` on Windows.
  `APPISTRY_PATH` defaults to `~/.local/share/appistry` and can be overridden via
  the `APPISTRY_HOME` environment variable.

Always check both. The second path covers the window between cloning and running
`appistry install`.

### Reference Implementation

Copy [`appistry_integration.py`](appistry_integration.py) into your project and
adapt only its constants block. The checked-in module is the authoritative,
security-hardened implementation; it refuses to replace unrelated existing
directories and treats installation/registration failures as soft skips.

```python
"""
appistry_integration.py

Drop this file into your project and call `setup_appistry()` from your
first-run or setup flow. Adjust the APP_* constants for your app.
"""

import os
import sys
from pathlib import Path

# ── Configure these for your app ──────────────────────────────────────────────

APP_NAME    = "My App"
APP_COMMAND = (
    ".venv\\Scripts\\python.exe ui/server.py"
    if sys.platform == "win32"
    else ".venv/bin/python ui/server.py"
)
APP_PORT    = 8000
APP_ICON    = "ui/static/icon.png"   # browser-renderable path relative to APP_CWD
APP_CWD     = Path(__file__).resolve().parent  # project root

APPISTRY_REPO    = "https://github.com/tohuw/appistry"
APPISTRY_PATH    = Path(os.environ["APPISTRY_HOME"]) if os.environ.get("APPISTRY_HOME") \
                   else Path.home() / ".local" / "share" / "appistry"
APPISTRY_BINARY = (
    APPISTRY_PATH / ".venv" / "Scripts" / "appistry.exe"
    if sys.platform == "win32"
    else APPISTRY_PATH / "appistry"
)
```

The module's `_install_appistry()` runs `appistry.py install` with the current
Python interpreter on Windows because the venv console entry point does not exist
until that command completes. Subsequent calls use `appistry.exe` directly.

### When to Call `setup_appistry()`

Call it from your app's first-run or explicit setup command — not silently on
every launch. The prompt should only appear when the user is already in a
setup-oriented context.

```python
# Example: in a `notekeeper setup` command
def cmd_setup(projects, args):
    from appistry_integration import setup_appistry
    print("[Appistry]")
    setup_appistry()
    print()
    # ... rest of setup
```

### Silent Re-Registration on Launch

After first-time setup, call `register()` directly (without `offer_install()`) on
each launch so the registry stays current if the project moves or config changes.
This is silent and fast — Appistry processes the call in milliseconds.

```python
# At the bottom of your server startup, before blocking on uvicorn:
from appistry_integration import register
register()
```

### Graceful Degradation

Your app must work fully without Appistry. Never gate core functionality on
Appistry being present. The integration is additive — a nicer launcher — not load-
bearing. All `appistry_integration` functions return a bool; check it only if you
want to log; never raise on failure.

---

## App About Page

Each app may include an `about.md` at its project root. When present, Appistry
surfaces an **About** item in the app's submenu that opens the page in the
default browser as rendered HTML.

`about.md` is optional. Apps without one simply have no About item.

### Format

```markdown
# App Name

_One sentence. What this app is._

## What it does

Two to four sentences describing the app's purpose and key behaviour from the
user's point of view. No marketing language. No feature lists.

## Data & Privacy

What data this app collects, stores, or transmits — and where it goes. Be
specific. If the app stores nothing and makes no network requests, say so
explicitly: **This app stores no data and makes no network requests.**
```

### Rules

- **Lead with the sentence.** The first paragraph under the `h1` is the
  one-line summary. Write it as a plain declarative sentence, not a tagline.
- **What it does is behaviour, not features.** Describe what happens when
  the user uses the app — not a bulleted capability list.
- **Data & Privacy is mandatory.** Omitting it reads as evasion. If there is
  nothing to disclose, say so in one sentence.
- **No third-level headings or deeper.** The page is a quick read, not a
  manual. If you need more structure, you need fewer words.
- **No screenshots, no badges, no links** (except to a privacy policy if one
  exists, placed at the end of Data & Privacy).

### Example

```markdown
# Notekeeper

_A local writing assistant that turns rough notes into structured prose._

## What it does

Notekeeper watches a folder of Markdown files and offers inline suggestions as you
write. Accepting a suggestion rewrites the paragraph in place. All processing
runs locally; nothing leaves your machine.

## Data & Privacy

Notekeeper reads files from the folder you configure and writes back to those same
files when you accept a suggestion. It stores a small preferences file at
`~/.notekeeper/config.toml`. No data is transmitted anywhere.
```

---

## Desktop Tray Behaviour

The desktop tray app reads the registry at startup and polls process state every
5 seconds. Menu structure on macOS:

```
[icon]
  ● Notekeeper
      Open ↗
      Stop          ← Option: Restart
      ──────────
      About         ← Option: GitHub ↗
  ● Widget
      Open ↗
      Stop
  ──────────────────
  Browse Apps ↗
  ──────────────────
  Help
  ──────────────────
  Quit All
  Quit Appistry
```

- Only **running** apps appear in the menu.
- **Stop** becomes **Restart** when the Option key is held.
- **About** appears only when the app's `cwd` contains an `about.md`. Holding Option
  replaces it with **GitHub ↗**, which opens the app's GitHub repository.
- **Browse Apps ↗** appears only when a catalog app is registered, and opens it. If
  that app is not running, it is started first; the browser opens only after it
  re-registers with its current port. The catalog app is identified by a registry id
  that is currently hard-coded in `menubar.py` / `windows_tray.py`.
- On Windows, Stop and Restart are separate submenu actions, and search opens a
  keyboard-accessible native search window because Windows tray menus do not host
  an inline search control.
- **Quit All** stops all running apps and exits the tray app. It relaunches on the
  next login, not immediately.
- **Quit Appistry** closes the tray icon only; registered apps keep running.

---

## Notes for Implementors

- **Port conflicts are your problem.** Appistry does not assign ports — you declare
  yours. Ensure your app's port is unique across all registered apps.
- **The command runs from `cwd`.** Use relative paths in `command` (for example,
  `.venv/bin/python` on macOS or `.venv\\Scripts\\python.exe` on Windows) so the
  registration is portable if the project moves.
- **Icons should be 32×32 or 64×64 PNG.** Larger images are accepted but will be
  scaled down by the native tray or launcher.
- **`appistry register` is idempotent.** Call it freely; it updates rather than
  duplicates.
