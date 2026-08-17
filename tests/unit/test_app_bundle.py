from __future__ import annotations

import plistlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from roost import app_bundle


def test_install_builds_managed_user_application() -> None:
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(app_bundle.sys, "platform", "darwin"), \
            patch.object(app_bundle.Path, "home", return_value=Path(tmp)):
        bundle = app_bundle.install()
        assert bundle == Path(tmp) / "Applications" / "Roost.app"
        with (bundle / "Contents" / "Info.plist").open("rb") as stream:
            assert plistlib.load(stream)["CFBundleIdentifier"] == app_bundle.BUNDLE_ID
        launcher = bundle / "Contents" / "MacOS" / "Roost"
        assert launcher.stat().st_mode & 0o111
        assert "launchctl kickstart" in launcher.read_text()
        assert app_bundle.uninstall()
        assert not bundle.exists()


def test_install_refuses_unrelated_application() -> None:
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(app_bundle.sys, "platform", "darwin"), \
            patch.object(app_bundle.Path, "home", return_value=Path(tmp)):
        info = Path(tmp) / "Applications" / "Roost.app" / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))
        try:
            app_bundle.install()
        except RuntimeError as exc:
            assert "unrelated application" in str(exc)
        else:
            raise AssertionError("unrelated application was overwritten")
