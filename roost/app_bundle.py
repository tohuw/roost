"""Managed macOS Finder entry for Roost's own menu-bar process.

This launches Roost only.  It does not know about, start, or supervise birds.
"""
from __future__ import annotations

import os
import plistlib
import sys
import tempfile
from pathlib import Path

NAME = "Roost"
BUNDLE_ID = "com.tohuw.roost"


def bundle_path() -> Path:
    return Path.home() / "Applications" / f"{NAME}.app"


def _managed(bundle: Path) -> bool:
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as stream:
            return plistlib.load(stream).get("CFBundleIdentifier") == BUNDLE_ID
    except (OSError, plistlib.InvalidFileException):
        return False


def _write(path: Path, contents: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def install() -> Path | None:
    if sys.platform != "darwin":
        return None
    bundle = bundle_path()
    if bundle.is_symlink() or (bundle.exists() and not _managed(bundle)):
        raise RuntimeError(f"refusing to overwrite unrelated application: {bundle}")
    macos_dir = bundle / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleExecutable": NAME,
        "LSUIElement": True,
    }
    _write(bundle / "Contents" / "Info.plist", plistlib.dumps(info), 0o644)
    # ``roost install`` has already registered this process in the user's Aqua
    # launchd domain. Activating that agent is the same route used at login and
    # avoids a Finder-launched duplicate winning the host lock while the intended
    # supervisor remains stopped. This controls Roost only; it never names or
    # starts a bird.
    launcher = (
        "#!/bin/sh\n"
        "exec /bin/launchctl kickstart -k \"gui/$(/usr/bin/id -u)/com.tohuw.roost\"\n"
    )
    _write(macos_dir / NAME, launcher.encode(), 0o755)
    return bundle


def uninstall() -> bool:
    bundle = bundle_path()
    if not _managed(bundle):
        return False
    import shutil
    shutil.rmtree(bundle)
    return True
