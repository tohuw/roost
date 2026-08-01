# Appistry

Native macOS menu bar and Windows system tray launcher for local apps. Appistry
keeps your apps running, surfaces them in the desktop shell, and gives each one a
stable local URL — so a tool that is "just a script serving `127.0.0.1:8000`"
behaves like a real desktop app.

Appistry is app-agnostic. It has no knowledge of any specific app; apps tell it
what to do by calling its CLI. It is used as the shared menubar for
[Huginn](https://github.com/tohuw/huginn) and
[Muninn](https://github.com/tohuw/muninn), and works with anything else that can
run a local web server.

## What it does

- Runs registered apps as background processes and shows them in the menu bar or system tray
- Searches running apps by name or app ID (native menu search on macOS, native search window on Windows)
- **Open** any running app through a branded wait page that redirects when ready
- Optionally presents an app's loopback UI in a dedicated, chromeless native window instead of a browser tab
- Provides stable local hook URLs for browser-return flows such as OAuth callbacks
- **Stop / Restart** apps from the desktop menu (Option-key alternate on macOS, explicit actions on Windows)
- Auto-starts after login using launchd on macOS or the current user's Startup folder on Windows
- Picks up environment changes automatically (`~/.zshenv` on macOS, user Environment settings on Windows)

Everything binds to `127.0.0.1`. Appistry makes no outbound network requests of
its own.

## Installation

Requires Python 3.10 or newer.

```bash
git clone https://github.com/tohuw/appistry.git
cd appistry
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python appistry.py install
```

On Windows (PowerShell):

```powershell
git clone https://github.com/tohuw/appistry.git
Set-Location appistry
py -3 appistry.py install
```

The Windows installer creates its virtual environment, installs the `appistry.exe`
CLI, adds that CLI directory to the current user's `PATH`, creates Start Menu
launchers for registered apps, and starts the system tray immediately.

Apps can also install Appistry on the user's behalf from their own setup flow —
see [`appistry_integration.py`](appistry_integration.py) and [SPEC.md](SPEC.md).

## CLI

```
appistry register --id <id> --name "Name" --cwd /path --command "cmd" --port PORT
appistry start <id>
appistry stop <id>
appistry open <id>
appistry launch <id>
appistry hook-url <id> /app/local/path
appistry list
appistry unregister <id>
```

Apps are registered in `~/.appistry/registry.toml`. See [SPEC.md](SPEC.md) for the
full integration spec.

## Removing an app

On macOS, drag the app's `.app` bundle from `/Applications` to the Trash. On
Windows, remove its shortcut from the Start Menu's `Appistry` folder. Appistry
detects the removal, stops the process, and cleans up its registry entry
automatically.

Project cleanup deletes only tracked files that match `HEAD` in the repository
rooted at the app's `cwd`. Modified, untracked, and gitignored files are always
kept, and if `cwd` is not a repository root nothing is deleted at all. Your data
and modified project files are never touched.

## Contributing

Issues and pull requests are welcome. Run the unit tests before opening a PR:

```bash
python -m pytest tests/unit -q
```

See [SECURITY.md](SECURITY.md) for how to report a vulnerability privately.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
