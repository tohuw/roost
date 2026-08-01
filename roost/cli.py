"""The Roost CLI.

Roost is a status menu bar, not a launcher, so the CLI is small on purpose.
The ravens start themselves and publish their own descriptors; nothing here
registers, starts, stops, or opens anything on their behalf.

Verbs:

    install     Set up the venv, the tray's login startup, and the CLI
    uninstall   Remove login startup and CLI integration
    ui          Start the tray
    ravens      Show what the tray currently sees, and why
    icon        Show, set, or reset the tray icon
"""

from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

from roost import help_server
from roost import icons
from roost import paths
from roost import ravens
from roost import sanitize
from roost import windows_support

HERE = Path(__file__).resolve().parent
#: The repository root — the directory containing the ``roost`` package, and what
#: an install operates on (the venv, the requirements file, the shim).
REPO = HERE.parent

#: The launchd agent's label, and therefore also its plist filename. Reverse-DNS
#: under a domain this project actually owns rather than ``com.appistry.*``, which
#: belongs to the separate internal Appistry: two agents sharing a label means
#: ``launchctl load`` on one silently displaces the other.
LABEL = "com.tohuw.roost"

#: The console script this project installs. Not ``appistry`` — that name is the
#: internal tool's and is on the user's PATH already.
COMMAND = "roost"


# ── ravens ────────────────────────────────────────────────────────────────────

def cmd_ravens(args: argparse.Namespace) -> int:
    """List every raven the tray can see, with the reason for any it cannot use.

    This is the diagnostic that answers "why is my raven not in the menu?", so it
    prints the unavailable ones too — with the same reason string the menu shows.
    A raven that were simply omitted would be indistinguishable from one that was
    never installed.
    """
    directory = ravens.state_dir()
    found = ravens.discover(directory)
    print(f"Descriptor directory: {directory}")
    if not directory.is_dir():
        print("  (does not exist yet — no raven has published a descriptor)")
        return 0
    if not found:
        print("  (empty — no raven has published a descriptor)")
        return 0
    print()
    for raven in found:
        # The name and reason come from a descriptor, which is untrusted input.
        # sanitize before writing either to a terminal.
        name = sanitize.safe_for_log(raven.name)
        display = sanitize.safe_for_log(raven.display)
        if isinstance(raven, ravens.AvailableRaven):
            descriptor = raven.descriptor
            print(f"  ● {display} ({name})")
            print(f"      port     {descriptor.port}")
            print(f"      pid      {descriptor.pid}")
            print(f"      api      {descriptor.min_api}..{descriptor.max_api}")
            print(f"      priority {descriptor.host_priority}")
            token = "yes" if descriptor.token_path else "no"
            print(f"      token    {token}")
        else:
            print(f"  ○ {display} ({name})")
            print(f"      {sanitize.safe_for_log(raven.reason, 200)}")
    return 0


# ── icon ──────────────────────────────────────────────────────────────────────

def cmd_icon(args: argparse.Namespace) -> int:
    if args.icon_action == "list":
        active = icons.resolve()
        for choice in icons.choices():
            marker = "*" if icons.is_active(choice, active) else " "
            print(f" {marker} {choice.label}")
        return 0

    if args.icon_action == "reset":
        icons.clear_icon()
        print(f"Tray icon reset to {icons.DEFAULT_ICON}.")
        return 0

    # set
    choice = icons.resolve(args.value)
    if choice is None or (
        args.value
        and choice.name != args.value
        and str(choice.path) != args.value
    ):
        print(
            f"Error: {args.value!r} is not a built-in icon name or a usable "
            "PNG/ICO path.",
            file=sys.stderr,
        )
        return 1
    icons.set_icon(args.value)
    print(f"Tray icon set to {choice.label}. Restart the tray to apply it.")
    return 0


# ── install / uninstall ───────────────────────────────────────────────────────

def _install_windows(args: argparse.Namespace) -> int:
    if sys.version_info < (3, 10):
        print("Error: Roost for Windows requires Python 3.10 or newer.",
              file=sys.stderr)
        return 1

    venv_dir = REPO / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    requirements = REPO / "requirements.txt"

    running_from_target_venv = False
    try:
        Path(sys.executable).resolve().relative_to(venv_dir.resolve())
        running_from_target_venv = True
    except ValueError:
        pass

    reinstall_in_place = venv_dir.exists() and args.force and running_from_target_venv
    if venv_dir.exists() and args.force and not reinstall_in_place:
        import shutil

        windows_support.stop_tray()
        print("Removing existing virtual environment...")
        shutil.rmtree(venv_dir)

    if not venv_dir.exists():
        print("Creating virtual environment...")
        if subprocess.run([sys.executable, "-m", "venv", str(venv_dir)]).returncode:
            print("Error: virtual environment creation failed.", file=sys.stderr)
            return 1
    elif reinstall_in_place:
        windows_support.stop_tray()
        print("Refreshing the active Roost virtual environment in place...")
    else:
        print("Virtual environment already exists (use --force to recreate).")

    print("Installing dependencies...")
    pip_options = ["--upgrade", "--force-reinstall"] if reinstall_in_place else []
    if subprocess.run([
        str(venv_python), "-m", "pip", "install", *pip_options,
        "-r", str(requirements),
    ]).returncode:
        print("Error: dependency installation failed.", file=sys.stderr)
        return 1
    if subprocess.run([
        str(venv_python), "-m", "pip", "install", "--editable", str(REPO)
    ]).returncode:
        print("Error: Roost CLI installation failed.", file=sys.stderr)
        return 1

    paths.ensure_state_dir()
    startup, menu = windows_support.install_shortcuts(REPO)
    print(f"Startup shortcut: {startup}")
    print(f"Start Menu shortcut: {menu}")

    cli_dir = venv_dir / "Scripts"
    if windows_support.add_cli_dir_to_user_path(cli_dir):
        print(f"Added to user PATH: {cli_dir}")
        print("Open a new terminal before using the 'roost' command.")

    if not windows_support.start_tray(REPO):
        print(
            f"Error: the Roost tray failed to start; check "
            f"{paths.log_path()}.",
            file=sys.stderr,
        )
        return 1
    print("Roost installed and running.")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if windows_support.is_windows():
        return _install_windows(args)

    home = Path.home()
    venv_dir = REPO / ".venv"
    pip_bin = venv_dir / "bin" / "pip"
    reqs_file = REPO / "requirements.txt"
    plist_dir = home / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{LABEL}.plist"
    log_path = paths.ensure_state_dir() / paths.LOG_NAME

    if venv_dir.exists() and not args.force:
        print("Virtual environment already exists (use --force to recreate).")
    else:
        if venv_dir.exists() and args.force:
            import shutil

            print("Removing existing venv…")
            shutil.rmtree(venv_dir)
        print("Creating virtual environment…")
        if subprocess.run([sys.executable, "-m", "venv", str(venv_dir)]).returncode:
            print("Error: venv creation failed.", file=sys.stderr)
            return 1

    if reqs_file.exists():
        print("Installing dependencies…")
        if subprocess.run([str(pip_bin), "install", "-r", str(reqs_file)]).returncode:
            print("Error: pip install failed.", file=sys.stderr)
            return 1
    else:
        print(f"Warning: {reqs_file} not found; skipping pip install.", file=sys.stderr)

    # launchd runs Python directly rather than through an .app bundle: the tray
    # needs a proper Aqua session window-server context, which only a launchd
    # agent in the user's GUI session provides.
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps({
        "Label": LABEL,
        # ``-m roost.menubar`` rather than a path to the file: the tray is a
        # package module now, and running it by path would put the package's own
        # directory on sys.path instead of its parent. WorkingDirectory is what
        # makes the package importable — launchd starts an agent in ``/`` and
        # sources no shell profile, so there is no PYTHONPATH to rely on.
        "ProgramArguments": [
            str(venv_dir / "bin" / "python"),
            "-m",
            "roost.menubar",
        ],
        "WorkingDirectory": str(REPO),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }, fmt=plistlib.FMT_XML))
    print(f"Wrote plist: {plist_path}")

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    if subprocess.run(["launchctl", "load", str(plist_path)]).returncode:
        print("Warning: launchctl load failed.", file=sys.stderr)
    subprocess.run(["launchctl", "start", LABEL])

    shim = REPO / "bin" / COMMAND
    if shim.exists():
        for bin_dir in (Path("/usr/local/bin"), home / ".local" / "bin"):
            target = bin_dir / COMMAND
            try:
                bin_dir.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(shim)
                print(f"Symlink created: {target} -> {shim}")
                import os

                if str(bin_dir) not in os.environ.get("PATH", "").split(":"):
                    print(f"Note: add {bin_dir} to your PATH to use 'roost'.")
                break
            except (PermissionError, FileNotFoundError):
                continue
        else:
            shim_dir = REPO / "bin"
            print(
                f"Note: could not install the CLI symlink; add {shim_dir} to your PATH."
            )

    print("Roost installed and running.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if windows_support.is_windows():
        windows_support.stop_tray()
        windows_support.uninstall_shortcuts()
        cli_dir = REPO / ".venv" / "Scripts"
        if windows_support.remove_cli_dir_from_user_path(cli_dir):
            print(f"Removed from user PATH: {cli_dir}")
        print("Roost uninstalled. The ravens were not touched.")
        return 0

    home = Path.home()
    plist_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    shim = REPO / "bin" / COMMAND

    subprocess.run(["launchctl", "stop", LABEL], capture_output=True)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed plist: {plist_path}")
    else:
        print("Plist not found (already removed?).")

    for bin_dir in (Path("/usr/local/bin"), home / ".local" / "bin"):
        symlink = bin_dir / COMMAND
        if symlink.is_symlink():
            try:
                if symlink.resolve() == shim.resolve():
                    symlink.unlink()
                    print(f"Removed symlink: {symlink}")
                else:
                    print(f"Leaving {symlink} (points elsewhere).")
            except PermissionError:
                print(f"Note: could not remove {symlink} (no write permission).")

    print("Roost uninstalled. The ravens were not touched.")
    return 0


# ── ui ────────────────────────────────────────────────────────────────────────

def cmd_ui(args: argparse.Namespace) -> int:
    """Start the tray, or report that it is already running.

    "Already running" is answered by probing the tray's own help endpoint rather
    than by looking for a process: a stale port file or a crashed tray must not
    be mistaken for a live one, and a live one must not be started twice. The
    tray's host lock is the real guard — this is only so the CLI can say
    something useful instead of spawning a process that exits immediately.
    """
    if windows_support.is_windows():
        if windows_support.tray_is_running():
            print("The Roost tray is already running.")
            return 0
        if not windows_support.start_tray(REPO):
            print(
                f"Failed to start the Roost tray; check "
                f"{paths.log_path()}.",
                file=sys.stderr,
            )
            return 1
        print("Roost tray started.")
        return 0

    if help_server.active_port() is not None and _macos_tray_responding():
        print("The Roost menu bar is already running.")
        return 0

    if subprocess.run(["launchctl", "start", LABEL], capture_output=True).returncode:
        print(
            "Failed to start the menu bar via launchd. "
            "Run `roost install` first to register the launch agent.",
            file=sys.stderr,
        )
        return 1
    print("Roost menu bar started.")
    return 0


def _macos_tray_responding() -> bool:
    import json
    import urllib.error
    import urllib.request

    port = help_server.active_port()
    if port is None:
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=0.5
        ) as response:
            return json.loads(response.read(1024)) == {"service": "roost", "ok": True}
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False


# ── Parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="The shared status menu bar for the ravens.",
    )
    sub = parser.add_subparsers(dest="verb", metavar="COMMAND")

    p_install = sub.add_parser("install", help="First-time setup (venv + login startup)")
    p_install.add_argument("--force", action="store_true",
                           help="Re-create the venv even if it already exists")

    sub.add_parser("uninstall", help="Remove login startup and CLI integration")
    sub.add_parser("ui", help="Start the menu bar or system tray")
    sub.add_parser("ravens", help="Show what the tray sees, and why")

    p_icon = sub.add_parser("icon", help="Show, set, or reset the tray icon")
    icon_sub = p_icon.add_subparsers(dest="icon_action", metavar="ACTION")
    icon_sub.add_parser("list", help="List the selectable icons")
    p_icon_set = icon_sub.add_parser("set", help="Choose an icon by name or path")
    p_icon_set.add_argument("value", help="A built-in name, or an absolute PNG/ICO path")
    icon_sub.add_parser("reset", help="Revert to the default icon")

    return parser


COMMANDS = {
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "ui": cmd_ui,
    "ravens": cmd_ravens,
    "icon": cmd_icon,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.verb is None:
        parser.print_help()
        sys.exit(0)
    if args.verb == "icon" and getattr(args, "icon_action", None) is None:
        args.icon_action = "list"
    handler = COMMANDS.get(args.verb)
    if handler is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
