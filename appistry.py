"""
appistry.py

Full CLI for Appistry — local app registry and macOS/Windows tray manager.

Commands:
  register    Add or update an app in the registry
  unregister  Remove an app (stops it first if running)
  list        Show all registered apps and their state
  start       Start an app by id
  stop        Stop an app by id
  open        Open an app's browser URL
  launch      Start an app if needed and open it
  hook-url    Print a stable Appistry proxy URL for an app-local path
  install     Set up the venv, native tray, login startup, and CLI
  uninstall   Remove native tray startup and CLI integration
"""

from __future__ import annotations

import argparse
import plistlib
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import hooks
import launch
import process
import registry
import windows_support
from registry import AppEntry, slugify


_APPLICATIONS_DIR = Path("/Applications")


def _app_bundle_path(entry: AppEntry) -> Path:
    """Return the /Applications/{name}.app path for entry, guaranteed to stay
    under /Applications regardless of what entry.name/entry.id contain."""
    safe_name = registry.bundle_name_for(entry.name, entry.id)
    bundle = (_APPLICATIONS_DIR / f"{safe_name}.app").resolve()
    bundle.relative_to(_APPLICATIONS_DIR.resolve())  # raises ValueError if escaped
    return bundle


def _registered_launcher_path(entry: AppEntry) -> Path:
    if windows_support.is_windows():
        return windows_support.registered_shortcut_path(entry)
    return _app_bundle_path(entry)


def _remove_registered_launcher(entry: AppEntry) -> None:
    if windows_support.is_windows():
        windows_support.remove_registered_shortcut(entry)
        return
    app_bundle = _app_bundle_path(entry)
    if app_bundle.exists():
        import shutil

        shutil.rmtree(app_bundle)


# ── Command handlers ──────────────────────────────────────────────────────────

def _entry_for_cli(app_id: str) -> AppEntry | None:
    """Validate a CLI app id before using it at any registry or path boundary."""
    try:
        safe_id = registry.validate_app_id(app_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None

    entry = registry.get(safe_id)
    if entry is None:
        print(f"Error: '{safe_id}' not found in registry.", file=sys.stderr)
    return entry

def _get_github_url(cwd: str) -> str | None:
    """Return the normalised GitHub HTTPS URL for the repo at cwd, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        if url.startswith("git@github.com:"):
            url = "https://github.com/" + url[len("git@github.com:"):]
        if not url.startswith("https://github.com/"):
            return None
        return url.removesuffix(".git")
    except Exception:
        return None


def cmd_register(args: argparse.Namespace) -> int:
    # Determine id so we can check for an existing entry
    app_id = args.id or (slugify(args.name) if args.name else None)
    if app_id is not None:
        try:
            app_id = registry.validate_app_id(app_id)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    existing = registry.get(app_id) if app_id else None

    if existing is None:
        # New registration — all core fields required
        missing = []
        if not args.name:    missing.append("--name")
        if not args.cwd:     missing.append("--cwd")
        if not args.command: missing.append("--command")
        if args.port is None: missing.append("--port")
        if missing:
            print(f"Error: new registration requires: {', '.join(missing)}", file=sys.stderr)
            return 1

    # Merge: provided args override existing values; omitted args keep existing values
    name    = args.name    or existing.name
    cwd     = str(Path(args.cwd).resolve()) if args.cwd else existing.cwd
    command = args.command or existing.command
    port    = args.port    if args.port is not None else existing.port
    icon    = args.icon    if args.icon is not None else (existing.icon if existing else None)

    # Require a GitHub remote — no remote, no registration
    github_url = _get_github_url(cwd)
    if not github_url:
        print(
            f"Error: no GitHub remote found for {cwd}.\n"
            "Appistry requires a GitHub remote (origin) to register an app.",
            file=sys.stderr,
        )
        return 1

    # Interactive icon prompt when no icon and running in a terminal
    if icon is None and sys.stdin.isatty():
        icon = _prompt_icon()

    if app_id is None:
        app_id = slugify(name)

    entry = AppEntry(id=app_id, name=name, cwd=cwd, command=command, port=port,
                     github_url=github_url, icon=icon)
    registry.upsert(entry)
    print(f"Registered: {app_id}")

    launcher_path = _build_registered_app(entry)
    if launcher_path:
        print(f"Launcher: {launcher_path}")

    return 0


def cmd_unregister(args: argparse.Namespace) -> int:
    entry = _entry_for_cli(args.id)
    if entry is None:
        return 1
    if process.is_running(entry.id):
        print(f"Stopping {entry.id}…")
        process.stop(entry.id)
    registry.remove(entry.id)

    launcher_path = _registered_launcher_path(entry)
    if windows_support.is_windows() or launcher_path.exists():
        _remove_registered_launcher(entry)
        print(f"Removed launcher: {launcher_path}")

    print(f"Unregistered: {entry.id}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    entries = registry.load()
    if not entries:
        print("No apps registered.")
        return 0
    # Column widths
    id_w   = max(len(e.id)   for e in entries)
    name_w = max(len(e.name) for e in entries)
    print(f"  {'ID':<{id_w}}  {'NAME':<{name_w}}  PORT")
    print(f"  {'-'*id_w}  {'-'*name_w}  ----")
    for e in entries:
        dot = "●" if process.is_running(e.id) else "○"
        print(f"{dot} {e.id:<{id_w}}  {e.name:<{name_w}}  {e.port}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    entry = _entry_for_cli(args.id)
    if entry is None:
        return 1
    if process.is_running(entry.id):
        print(f"'{entry.id}' is already running.", file=sys.stderr)
        return 2
    ok = process.start(entry)
    if ok:
        print(f"Started: {entry.id}")
        return 0
    else:
        print(f"Error: '{entry.id}' failed to start (check ~/.appistry/{entry.id}.log).",
              file=sys.stderr)
        return 1


def cmd_run(args: argparse.Namespace) -> int:
    """Run an app's server in the foreground, replacing this process (execv).

    Used by the generated /Applications/{Name}.app launcher: because the server
    replaces this process in-place, it inherits the .app bundle's LaunchServices
    identity, so macOS file-access (TCC) prompts are attributed to the app
    instead of the raw interpreter. Does not return on success.
    """
    entry = _entry_for_cli(args.id)
    if entry is None:
        return 1
    if process.is_running(entry.id):
        # Already running from another launch — just surface it, don't double-run.
        print(f"'{entry.id}' is already running.", file=sys.stderr)
        return 2
    return process.run_foreground(entry)


def cmd_stop(args: argparse.Namespace) -> int:
    entry = _entry_for_cli(args.id)
    if entry is None:
        return 1
    if not process.is_running(entry.id):
        print(f"'{entry.id}' is not running.", file=sys.stderr)
        return 2
    process.stop(entry.id)
    print(f"Stopped: {entry.id}")
    return 0


def _menubar_launch_url(app_id: str) -> str | None:
    port_file = registry.APPISTRY_DIR / "menubar-http-port"
    try:
        port = int(port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    encoded = urllib.parse.quote(app_id, safe="")
    ready_url = f"http://127.0.0.1:{port}/api/launch/{encoded}/ready"
    try:
        urllib.request.urlopen(ready_url, timeout=0.5)
    except (OSError, urllib.error.URLError):
        return None
    return f"http://127.0.0.1:{port}/launch/{encoded}"


def cmd_open(args: argparse.Namespace) -> int:
    entry = _entry_for_cli(args.id)
    if entry is None:
        return 1
    if not process.is_running(entry.id):
        print(f"Warning: '{entry.id}' does not appear to be running.", file=sys.stderr)
        return 2
    url = _menubar_launch_url(entry.id) or f"http://localhost:{entry.port}"
    webbrowser.open(url)
    print(f"Opened: {url}")
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    """Open an app's readiness page and start the app when necessary."""
    entry = _entry_for_cli(args.id)
    if entry is None:
        return 1
    launch_url = _menubar_launch_url(entry.id)
    if launch_url:
        webbrowser.open(launch_url)
    if process.is_running(entry.id):
        url = launch_url or f"http://localhost:{entry.port}"
        if launch_url is None:
            webbrowser.open(url)
        print(f"Opened: {url}")
        return 0
    if not process.start(entry):
        print(
            f"Error: '{entry.id}' failed to start (check ~/.appistry/{entry.id}.log).",
            file=sys.stderr,
        )
        return 1
    if launch_url is None:
        webbrowser.open(f"http://localhost:{entry.port}")
    print(f"Started and opened: {entry.id}")
    return 0


def cmd_window(args: argparse.Namespace) -> int:
    """Open an app's UI in an Appistry-owned dedicated native window.

    Appistry owns the window so apps don't each need their own Python/pywebview:
    it starts the app if needed (mirroring `launch`), reads the per-launch secret
    minted by `process.start`, and opens `ygg_shell.py` from Appistry's own
    interpreter. On no webview backend the shell exits 3 and we fall back to the
    browser (handled inside `launch.open_dedicated_window`).

    Secret carriage defaults to a URL fragment (for apps whose own front-end JS
    reads it). Use `--secret-mode query` for apps whose server reads the secret
    off the document request (e.g. a prebuilt SPA).
    """
    entry = _entry_for_cli(args.id)
    if entry is None:
        return 1
    if not process.is_running(entry.id):
        if not process.start(entry):
            print(
                f"Error: '{entry.id}' failed to start (check ~/.appistry/{entry.id}.log).",
                file=sys.stderr,
            )
            return 1
    code = launch.open_dedicated_window(entry, secret_mode=args.secret_mode, block=True)
    if code == 3:
        # No webview backend — the browser fallback already fired.
        print(f"No native window backend available; opened browser for {entry.id}.")
        return 0
    return 0 if code == 0 else 1


def cmd_hook_url(args: argparse.Namespace) -> int:
    entry = _entry_for_cli(args.id)
    if entry is None:
        return 1
    try:
        url = hooks.hook_url(entry.id, args.path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(url)
    return 0


def _prompt_icon() -> str | None:
    """Interactively prompt for an icon file. Returns path string or None if skipped."""
    print()
    print("Icon file (optional) — accepted: PNG, ICNS, JPEG, TIFF, PDF; SVG not supported.")
    print("Path relative to app's cwd, or absolute. Leave blank to skip.")
    raw = input("  Icon path: ").strip()
    return raw if raw else None


def _icon_to_icns(src: Path, out: Path) -> bool:
    """Convert src image to ICNS at out path. Returns True on success."""
    if src.suffix.lower() == ".icns":
        import shutil as _shutil
        _shutil.copy2(src, out)
        return True
    import tempfile
    # sips + iconutil — both ship with macOS
    specs = [
        (16,   "icon_16x16.png"),
        (32,   "icon_16x16@2x.png"),
        (32,   "icon_32x32.png"),
        (64,   "icon_32x32@2x.png"),
        (128,  "icon_128x128.png"),
        (256,  "icon_128x128@2x.png"),
        (256,  "icon_256x256.png"),
        (512,  "icon_256x256@2x.png"),
        (512,  "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for sz, fname in specs:
            r = subprocess.run(
                ["sips", "-z", str(sz), str(sz), str(src), "--out", str(iconset / fname)],
                capture_output=True,
            )
            if r.returncode != 0:
                return False
        r = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
            capture_output=True,
        )
        return r.returncode == 0


def _build_registered_app(entry: AppEntry) -> Path | None:
    """Build the platform-native launcher for a registered app."""
    if windows_support.is_windows():
        return windows_support.build_registered_shortcut(
            entry,
            Path(__file__).resolve().parent,
        )
    app_bundle = _app_bundle_path(entry)
    safe_name = registry.bundle_name_for(entry.name, entry.id)
    macos_dir   = app_bundle / "Contents" / "MacOS"
    resources_dir = app_bundle / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(exist_ok=True)

    bundle_id = f"com.appistry.app.{entry.id}"

    # Resolve icon first so we know whether to add CFBundleIconFile
    has_icon = False
    if entry.icon:
        icon_src = (Path(entry.cwd) / entry.icon
                    if not Path(entry.icon).is_absolute()
                    else Path(entry.icon))
        if icon_src.exists():
            icns_out = resources_dir / f"{safe_name}.icns"
            if _icon_to_icns(icon_src, icns_out):
                has_icon = True

    plist_data: dict = {
        "CFBundleName": entry.name,
        "CFBundleDisplayName": entry.name,
        "CFBundleIdentifier": bundle_id,
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleExecutable": safe_name,
        "NSHighResolutionCapable": True,
    }
    if has_icon:
        plist_data["CFBundleIconFile"] = safe_name
    (app_bundle / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(plist_data, fmt=plistlib.FMT_XML)
    )

    appistry_bin = Path(__file__).resolve().parent / "appistry"
    # The launcher must *become* the server process (exec), not spawn-and-exit.
    # macOS attributes file-access (TCC) prompts to the responsible process's
    # bundle identity; only a process still running inside this .app carries that
    # identity. `appistry run` records the PID and execs the server in place, so
    # the prompt reads as this app (e.g. "My App") instead of the raw interpreter
    # ("Python"). The browser is opened from a short-lived background subshell
    # before the exec replaces this process. If the server is already running,
    # `appistry run` exits non-zero (code 2) and we just open the browser.
    open_bg = (
        f'( sleep 2; open {shlex.quote(f"http://localhost:{entry.port}")} ) &\n'
        if entry.port else ""
    )
    launcher = macos_dir / safe_name
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        f"{open_bg}exec {shlex.quote(str(appistry_bin))} run {shlex.quote(entry.id)}\n"
    )
    launcher.chmod(0o755)
    return app_bundle


def _appistry_launcher_script(menubar_target: Path) -> str:
    """Return the Appistry.app launcher script contents.

    menubar_target (derived from the install directory, which is
    environment/user-controlled) is shell-quoted so metacharacters in the
    install path can't inject additional shell commands.
    """
    return (
        "#!/usr/bin/env bash\n"
        f"if pgrep -qf {shlex.quote(str(menubar_target))}; then\n"
        "    osascript -e 'display notification "
        '"Appistry is already running - look for the icon in your menu bar." '
        "with title \"Appistry\"'\n"
        "else\n"
        "    launchctl start com.appistry.menubar\n"
        "fi\n"
    )


def _build_app_bundle(appistry_dir: Path) -> Path:
    """Build a minimal Appistry.app bundle in /Applications so macOS
    attributes permissions and notifications to 'Appistry' rather than 'python'."""
    app_bundle    = Path("/Applications/Appistry.app")
    macos_dir     = app_bundle / "Contents" / "MacOS"
    resources_dir = app_bundle / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(exist_ok=True)

    # Convert the menu bar PNG to ICNS for the Finder/Dock icon
    has_icon = False
    icon_src = appistry_dir / "appistry_icon.png"
    if icon_src.exists():
        icns_out = resources_dir / "Appistry.icns"
        if _icon_to_icns(icon_src, icns_out):
            has_icon = True

    plist_data: dict = {
        "CFBundleName": "Appistry",
        "CFBundleDisplayName": "Appistry",
        "CFBundleIdentifier": "com.appistry.menubar",
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleExecutable": "Appistry",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    if has_icon:
        plist_data["CFBundleIconFile"] = "Appistry"
    (app_bundle / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(plist_data, fmt=plistlib.FMT_XML)
    )

    # Launcher — delegates to launchd so the menubar process always runs in
    # the proper Aqua session context, regardless of how the .app is invoked.
    launcher = macos_dir / "Appistry"
    launcher.write_text(_appistry_launcher_script(appistry_dir / "menubar.py"))
    launcher.chmod(0o755)
    return app_bundle


def _cmd_install_windows(args: argparse.Namespace) -> int:
    if sys.version_info < (3, 10):
        print("Error: Appistry for Windows requires Python 3.10 or newer.", file=sys.stderr)
        return 1

    appistry_dir = Path(__file__).resolve().parent
    venv_dir = appistry_dir / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    requirements = appistry_dir / "requirements.txt"
    running_from_target_venv = False
    try:
        Path(sys.executable).resolve().relative_to(venv_dir.resolve())
        running_from_target_venv = True
    except ValueError:
        running_from_target_venv = False

    reinstall_in_place = venv_dir.exists() and args.force and running_from_target_venv
    if venv_dir.exists() and args.force and not reinstall_in_place:
        import shutil

        windows_support.stop_tray()
        print("Removing existing virtual environment...")
        shutil.rmtree(venv_dir)

    if not venv_dir.exists():
        print("Creating virtual environment...")
        result = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)])
        if result.returncode != 0:
            print("Error: virtual environment creation failed.", file=sys.stderr)
            return 1
    else:
        if reinstall_in_place:
            windows_support.stop_tray()
            print("Refreshing the active Appistry virtual environment in place...")
        else:
            print("Virtual environment already exists (use --force to recreate).")

    print("Installing dependencies...")
    pip_options = ["--upgrade", "--force-reinstall"] if reinstall_in_place else []
    result = subprocess.run(
        [
            str(venv_python), "-m", "pip", "install", *pip_options,
            "-r", str(requirements),
        ]
    )
    if result.returncode != 0:
        print("Error: dependency installation failed.", file=sys.stderr)
        return 1
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--editable", str(appistry_dir)]
    )
    if result.returncode != 0:
        print("Error: Appistry CLI installation failed.", file=sys.stderr)
        return 1

    registry.APPISTRY_DIR.mkdir(parents=True, exist_ok=True)
    startup, menu = windows_support.install_appistry_shortcuts(appistry_dir)
    print(f"Startup shortcut: {startup}")
    print(f"Start Menu shortcut: {menu}")

    for entry in registry.load():
        launcher = windows_support.build_registered_shortcut(entry, appistry_dir)
        print(f"App launcher: {launcher}")

    cli_dir = venv_dir / "Scripts"
    if windows_support.add_cli_dir_to_user_path(cli_dir):
        print(f"Added to user PATH: {cli_dir}")
        print("Open a new terminal before using the 'appistry' command.")

    if not windows_support.start_tray(appistry_dir):
        print(
            f"Error: Appistry tray failed to start; check {registry.APPISTRY_DIR / 'menubar.log'}.",
            file=sys.stderr,
        )
        return 1
    print("Appistry installed and running.")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if windows_support.is_windows():
        return _cmd_install_windows(args)
    home = Path.home()
    appistry_dir = Path(__file__).resolve().parent
    venv_dir     = appistry_dir / ".venv"
    pip_bin      = venv_dir / "bin" / "pip"
    reqs_file    = appistry_dir / "requirements.txt"

    plist_dir    = home / "Library" / "LaunchAgents"
    plist_path   = plist_dir / "com.appistry.menubar.plist"
    log_dir      = home / ".appistry"
    log_path     = log_dir / "menubar.log"
    label        = "com.appistry.menubar"

    # 1. Create venv
    if venv_dir.exists() and not args.force:
        print("Virtual environment already exists (use --force to recreate).")
    else:
        if venv_dir.exists() and args.force:
            import shutil
            print("Removing existing venv…")
            shutil.rmtree(venv_dir)
        print("Creating virtual environment…")
        result = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)])
        if result.returncode != 0:
            print("Error: venv creation failed.", file=sys.stderr)
            return 1

    # 2. Install dependencies
    if reqs_file.exists():
        print("Installing dependencies…")
        result = subprocess.run([str(pip_bin), "install", "-r", str(reqs_file)])
        if result.returncode != 0:
            print("Error: pip install failed.", file=sys.stderr)
            return 1
    else:
        print(f"Warning: {reqs_file} not found; skipping pip install.", file=sys.stderr)

    # 3. Build the .app bundle so macOS shows "Appistry" not "python"
    print("Building Appistry.app bundle…")
    _build_app_bundle(appistry_dir)

    # 4. Write launchd plist pointing directly at Python + menubar.py — the
    #    bundle launcher is only for Finder double-clicks; launchd must run
    #    Python directly so it gets a proper Aqua session window-server context.
    plist_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_data: dict = {
        "Label": label,
        "ProgramArguments": [
            str(venv_dir / "bin" / "python"),
            str(appistry_dir / "menubar.py"),
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    plist_path.write_bytes(plistlib.dumps(plist_data, fmt=plistlib.FMT_XML))
    print(f"Wrote plist: {plist_path}")

    # 5. Load the plist (unload first for idempotency)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(plist_path)])
    if result.returncode != 0:
        print("Warning: launchctl load failed.", file=sys.stderr)

    # 6. Start immediately
    subprocess.run(["launchctl", "start", label])

    # 7. Install CLI symlink — try /usr/local/bin, fall back to ~/.local/bin
    appistry_shim = appistry_dir / "appistry"
    if appistry_shim.exists():
        candidates = [Path("/usr/local/bin"), home / ".local" / "bin"]
        for bin_dir in candidates:
            symlink_target = bin_dir / "appistry"
            try:
                bin_dir.mkdir(parents=True, exist_ok=True)
                if symlink_target.exists() or symlink_target.is_symlink():
                    symlink_target.unlink()
                symlink_target.symlink_to(appistry_shim)
                print(f"Symlink created: {symlink_target} -> {appistry_shim}")
                import os
                path_dirs = os.environ.get("PATH", "").split(":")
                if str(bin_dir) not in path_dirs:
                    print(f"Note: add {bin_dir} to your PATH to use the 'appistry' command.")
                    print(f'      e.g. echo \'export PATH="{bin_dir}:$PATH"\' >> ~/.zshrc')
                break
            except (PermissionError, FileNotFoundError):
                continue
        else:
            print(f"Note: could not install CLI symlink.")
            print(f"      Add {appistry_dir} to your PATH manually.")

    print("Appistry installed and running.")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Bring all registry entries up to the current Appistry spec."""
    entries = registry.load()
    if not entries:
        print("No apps registered.")
        return 0

    ok = skipped = failed = 0
    for entry in entries:
        changes = []

        # Backfill github_url if missing
        if not entry.github_url:
            url = _get_github_url(entry.cwd)
            if url:
                entry.github_url = url
                changes.append(f"github_url → {url}")
            else:
                print(
                    f"  ✗ {entry.id}: no GitHub remote found for {entry.cwd} — "
                    "fix the remote and re-run migrate, or unregister this app.",
                    file=sys.stderr,
                )
                failed += 1
                continue

        if changes:
            registry.upsert(entry)
            print(f"  ✓ {entry.id}: {'; '.join(changes)}")
            ok += 1
        else:
            print(f"  – {entry.id}: already up to date")
            skipped += 1

    print(f"\n{ok} updated, {skipped} already current, {failed} failed.")
    return 0 if failed == 0 else 1


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Rebuild platform-native launchers for all registered apps."""
    if windows_support.is_windows():
        appistry_dir = Path(__file__).resolve().parent
        for entry in registry.load():
            windows_support.remove_registered_shortcut(entry)
            result = windows_support.build_registered_shortcut(entry, appistry_dir)
            print(f"Rebuilt: {result}")
        windows_support.install_appistry_shortcuts(appistry_dir)
        print("Rebuilt: Appistry Start Menu and startup shortcuts")
        return 0
    import shutil
    appistry_dir = Path(__file__).resolve().parent
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework"
        "/Frameworks/LaunchServices.framework/Support/lsregister"
    )
    for entry in registry.load():
        bundle = _app_bundle_path(entry)
        if bundle.exists():
            shutil.rmtree(bundle)
        result = _build_registered_app(entry)
        if result:
            print(f"Rebuilt: {result}")
            if lsregister.exists():
                subprocess.run([str(lsregister), "-f", str(result)], capture_output=True)
        else:
            print(f"Failed: {entry.name}", file=sys.stderr)

    # Rebuild Appistry.app itself
    _build_app_bundle(appistry_dir)
    print(f"Rebuilt: /Applications/Appistry.app")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Start the platform-native tray UI."""
    appistry_dir = Path(__file__).resolve().parent
    if windows_support.is_windows():
        if windows_support.control_server_running():
            print("Appistry tray is already running.")
            return 0
        if not windows_support.start_tray(appistry_dir):
            print(
                f"Failed to start Appistry tray; check {registry.APPISTRY_DIR / 'menubar.log'}.",
                file=sys.stderr,
            )
            return 1
        print("Appistry tray started.")
        return 0
    menubar_py   = appistry_dir / "menubar.py"
    label        = "com.appistry.menubar"

    if subprocess.run(["pgrep", "-qf", str(menubar_py)]).returncode == 0:
        print("Appistry menubar is already running.")
        return 0

    result = subprocess.run(["launchctl", "start", label], capture_output=True)
    if result.returncode != 0:
        print(
            "Failed to start menubar via launchd. "
            "Run `appistry install` first to register the launch agent.",
            file=sys.stderr,
        )
        return 1

    print("Appistry menubar started.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if windows_support.is_windows():
        appistry_dir = Path(__file__).resolve().parent
        for entry in registry.load():
            if process.is_running(entry.id):
                print(f"Stopping {entry.id}...")
                process.stop(entry.id)
        windows_support.stop_tray()
        windows_support.uninstall_shortcuts()
        cli_dir = appistry_dir / ".venv" / "Scripts"
        if windows_support.remove_cli_dir_from_user_path(cli_dir):
            print(f"Removed from user PATH: {cli_dir}")
        print("Appistry uninstalled. Registry and project data were preserved.")
        return 0
    home        = Path.home()
    plist_path  = home / "Library" / "LaunchAgents" / "com.appistry.menubar.plist"
    label       = "com.appistry.menubar"
    symlink     = Path("/usr/local/bin/appistry")
    appistry_shim = Path(__file__).resolve().parent / "appistry"

    # Stop all running apps
    for entry in registry.load():
        if process.is_running(entry.id):
            print(f"Stopping {entry.id}…")
            process.stop(entry.id)

    # Stop and unload plist
    subprocess.run(["launchctl", "stop",   label], capture_output=True)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed plist: {plist_path}")
    else:
        print("Plist not found (already removed?).")

    # Remove Appistry.app from /Applications
    app_bundle = Path("/Applications/Appistry.app")
    if app_bundle.exists():
        import shutil
        shutil.rmtree(app_bundle)
        print(f"Removed app bundle: {app_bundle}")

    # Remove symlink from whichever bin dir it was installed to
    for bin_dir in [Path("/usr/local/bin"), home / ".local" / "bin"]:
        symlink = bin_dir / "appistry"
        if symlink.is_symlink():
            try:
                if symlink.resolve() == appistry_shim.resolve():
                    symlink.unlink()
                    print(f"Removed symlink: {symlink}")
                else:
                    print(f"Leaving {symlink} (points elsewhere).")
            except PermissionError:
                print(f"Note: could not remove {symlink} (no write permission).")

    print("Appistry uninstalled.")
    return 0


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="appistry",
        description="Local app registry and macOS/Windows tray manager.",
    )
    sub = parser.add_subparsers(dest="verb", metavar="COMMAND")

    # register
    p_reg = sub.add_parser("register", help="Add or update an app in the registry")
    p_reg.add_argument("--name",    default=None, help="Display name (required for new registrations)")
    p_reg.add_argument("--cwd",     default=None, help="Absolute path to project root (required for new registrations)")
    p_reg.add_argument("--command", default=None, help="Shell command to start the server (required for new registrations)")
    p_reg.add_argument("--port",    default=None, type=int, help="Port the server listens on (required for new registrations)")
    p_reg.add_argument("--icon",    default=None, help="Icon path relative to cwd (optional)")
    p_reg.add_argument("--id",      default=None, help="Explicit id slug (derived from name if omitted); required for re-registrations")

    # unregister
    p_unreg = sub.add_parser("unregister", help="Remove an app from the registry")
    p_unreg.add_argument("id", help="App id")

    # list
    sub.add_parser("list", help="Show all registered apps")

    # start
    p_start = sub.add_parser("start", help="Start an app")
    p_start.add_argument("id", help="App id")

    # run (foreground exec — used by the .app bundle launcher for TCC attribution)
    p_run = sub.add_parser(
        "run", help="Run an app's server in the foreground (replaces this process)")
    p_run.add_argument("id", help="App id")

    # stop
    p_stop = sub.add_parser("stop", help="Stop an app")
    p_stop.add_argument("id", help="App id")

    # open
    p_open = sub.add_parser("open", help="Open an app in the browser")
    p_open.add_argument("id", help="App id")

    # launch (used by native per-app launchers; starts the app when needed)
    p_launch = sub.add_parser("launch", help="Start an app if needed and open it")
    p_launch.add_argument("id", help="App id")

    # window (open an app in an Appistry-owned dedicated native window)
    p_window = sub.add_parser(
        "window", help="Open an app in a dedicated native window (starts it if needed)")
    p_window.add_argument("id", help="App id")
    p_window.add_argument(
        "--secret-mode",
        choices=["fragment", "query"],
        default="fragment",
        help=(
            "How to carry the launch secret to the app: 'fragment' (default, "
            "read by the app's front-end) or 'query' (read by the app's server)."
        ),
    )

    # hook-url
    p_hook = sub.add_parser("hook-url", help="Print a stable proxy URL for an app-local hook path")
    p_hook.add_argument("id", help="App id")
    p_hook.add_argument("path", help="App-local path to proxy, e.g. /api/oauth/callback")

    # migrate
    sub.add_parser("migrate", help="Bring all registry entries up to the current Appistry spec")

    # rebuild
    sub.add_parser("rebuild", help="Rebuild all platform-native app launchers")

    # install
    p_install = sub.add_parser("install", help="First-time setup (venv + login startup)")
    p_install.add_argument("--force", action="store_true",
                           help="Re-create venv even if it already exists")

    # uninstall
    sub.add_parser("uninstall", help="Remove login startup and CLI integration")

    # ui
    sub.add_parser("ui", help="Start the menu bar or system tray UI")

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    "register":   cmd_register,
    "unregister": cmd_unregister,
    "list":       cmd_list,
    "start":      cmd_start,
    "run":        cmd_run,
    "stop":       cmd_stop,
    "open":       cmd_open,
    "launch":     cmd_launch,
    "window":     cmd_window,
    "hook-url":   cmd_hook_url,
    "migrate":    cmd_migrate,
    "rebuild":    cmd_rebuild,
    "install":    cmd_install,
    "uninstall":  cmd_uninstall,
    "ui":         cmd_ui,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.verb is None:
        parser.print_help()
        sys.exit(0)
    handler = COMMANDS.get(args.verb)
    if handler is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
