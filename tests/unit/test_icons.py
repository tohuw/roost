"""The tray icon, now that there is exactly one.

Icon *selection* was removed: the submenu, the CLI verb, the config file and its
validation surface all existed so the mark in the tray could be a different
mark. What remains worth pinning is that the right variant is chosen per
platform, and that a missing asset degrades instead of raising — every caller
already has a fallback, and refusing to start over a PNG would be worse.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import icons


class TestResolution:
    def test_the_default_is_the_raven(self):
        """A licence obligation as much as a default. See assets/CREDITS.md."""
        assert icons.DEFAULT_ICON == "raven"
        assert icons.resolve().name == "raven"

    def test_macos_gets_the_template_variant(self, monkeypatch):
        """template=True reads only the alpha channel, so it must be the
        monochrome silhouette; handing it colour achieves nothing."""
        monkeypatch.setattr(icons, "_IS_MACOS", True)
        choice = icons.resolve()
        assert choice.template is True
        assert choice.path.name == "raven-template.png"

    def test_windows_and_linux_get_the_colour_variant(self, monkeypatch):
        """pystray has no template concept and draws the bitmap literally, so
        the macOS file would be invisible against a taskbar of the same shade."""
        monkeypatch.setattr(icons, "_IS_MACOS", False)
        choice = icons.resolve()
        assert choice.template is False
        assert choice.path.name == "raven.png"

    def test_both_variants_ship(self):
        template, colour = icons._variant_paths(icons.DEFAULT_ICON)
        assert template.exists(), template
        assert colour.exists(), colour

    def test_a_missing_asset_degrades_rather_than_raising(self, monkeypatch, tmp_path):
        monkeypatch.setattr(icons, "ASSETS_DIR", tmp_path / "nothing-here")
        assert icons.resolve() is None

    def test_an_argument_is_accepted_and_ignored(self):
        """Call sites pass through from before selection was removed."""
        assert icons.resolve("anything at all") == icons.resolve()


class TestSelectionIsGone:
    """The feature is removed, not merely hidden behind a flag."""

    @pytest.mark.parametrize("name", [
        "set_icon", "clear_icon", "choices", "is_active",
        "builtin_names", "configured_icon", "config_path",
    ])
    def test_the_selection_api_is_absent(self, name):
        assert not hasattr(icons, name), f"icons.{name} survived the removal"

    def test_no_config_file_is_read(self):
        source = Path(icons.__file__).read_text(encoding="utf-8")
        for forbidden in ("tomllib", "roost.toml", "STATE_DIR"):
            assert forbidden not in source, forbidden
