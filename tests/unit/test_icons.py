"""Selectable tray icon tests.

The properties worth pinning are the platform ones, because getting them wrong
produces an icon that is invisible or that renders as a flat blob: macOS wants
the monochrome template (it keeps only alpha and tints it), Windows wants the
colour PNG (it renders RGB literally), and a user-supplied colour file must never
be marked as a template.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import icons
import paths

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def config(monkeypatch, tmp_path):
    """Point the icon config at a temp dir so tests never touch ~/.appistry."""
    monkeypatch.setattr(paths, "APPISTRY_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def assets(monkeypatch, tmp_path):
    """A synthetic asset directory with both variants of two icons."""
    directory = tmp_path / "assets"
    directory.mkdir()
    for name in ("raven", "appistry"):
        for suffix in ("", "@2x"):
            (directory / f"{name}{suffix}.png").write_bytes(b"colour")
            (directory / f"{name}-template{suffix}.png").write_bytes(b"mono")
    monkeypatch.setattr(icons, "ASSETS_DIR", directory)
    return directory


# ── The shipped assets ────────────────────────────────────────────────────────

class TestShippedAssets:
    """The checked-in files must exist: nothing rasterizes them at runtime."""

    @pytest.mark.parametrize("name", [
        "raven.svg",
        "raven.png", "raven@2x.png",
        "raven-template.png", "raven-template@2x.png",
        "appistry.png", "appistry-template.png",
        "CREDITS.md",
    ])
    def test_asset_is_present(self, name):
        assert (REPO / "assets" / name).is_file(), name

    def test_the_raven_default_resolves_without_a_config(self):
        choice = icons.resolve_builtin(icons.DEFAULT_ICON)
        assert choice is not None
        assert choice.path.is_file()

    def test_attribution_is_shipped_with_the_art(self):
        """CC BY 3.0 requires credit; this is a licence obligation, not a nicety."""
        credits = (REPO / "assets" / "CREDITS.md").read_text(encoding="utf-8")
        assert "Lorc" in credits
        assert "game-icons.net" in credits
        assert "CC BY 3.0" in credits

    def test_the_svg_keeps_its_inline_attribution(self):
        svg = (REPO / "assets" / "raven.svg").read_text(encoding="utf-8")
        assert "Lorc" in svg
        assert "CC BY 3.0" in svg


# ── Built-in resolution per platform ──────────────────────────────────────────

class TestBuiltinResolution:
    def test_names_are_discovered_with_the_default_first(self, assets):
        assert icons.builtin_names()[0] == icons.DEFAULT_ICON
        assert set(icons.builtin_names()) == {"raven", "appistry"}

    def test_template_variants_are_not_listed_as_icons(self, assets):
        """"raven-template@2x.png" must reduce to "raven", not to a third icon."""
        assert "raven-template" not in icons.builtin_names()

    def test_macos_prefers_the_template_variant(self, assets, monkeypatch):
        monkeypatch.setattr(icons, "_IS_MACOS", True)
        choice = icons.resolve_builtin("raven")
        assert choice.path.name == "raven-template.png"
        assert choice.template is True

    def test_windows_prefers_the_colour_variant(self, assets, monkeypatch):
        """pystray has no template concept; a mono icon would be invisible."""
        monkeypatch.setattr(icons, "_IS_MACOS", False)
        choice = icons.resolve_builtin("raven")
        assert choice.path.name == "raven.png"
        assert choice.template is False

    def test_windows_never_marks_a_fallback_as_a_template(self, assets, monkeypatch):
        monkeypatch.setattr(icons, "_IS_MACOS", False)
        (assets / "raven.png").unlink()
        choice = icons.resolve_builtin("raven")
        assert choice.path.name == "raven-template.png"
        assert choice.template is False

    def test_macos_falls_back_to_colour_without_a_template(self, assets, monkeypatch):
        monkeypatch.setattr(icons, "_IS_MACOS", True)
        (assets / "raven-template.png").unlink()
        choice = icons.resolve_builtin("raven")
        assert choice.path.name == "raven.png"
        assert choice.template is False

    def test_a_missing_icon_resolves_to_none(self, assets):
        assert icons.resolve_builtin("absent") is None

    @pytest.mark.parametrize("name", [
        "", "../escape", "/abs", "Raven", "has space", "a" * 40, None,
    ])
    def test_unsafe_names_are_refused(self, assets, name):
        assert icons.resolve_builtin(name) is None

    def test_a_missing_assets_dir_offers_nothing(self, monkeypatch, tmp_path):
        """Naming an icon whose file is absent would put a dead entry in the menu."""
        monkeypatch.setattr(icons, "ASSETS_DIR", tmp_path / "absent")
        assert icons.builtin_names() == []
        assert icons.choices() == []

    def test_label_is_human_readable(self, assets):
        assert icons.resolve_builtin("raven").label == "Raven"


# ── User-supplied icons ───────────────────────────────────────────────────────

class TestUserIcon:
    def test_absolute_png_is_accepted(self, tmp_path):
        target = tmp_path / "mine.png"
        target.write_bytes(b"data")
        choice = icons.resolve_user_icon(str(target))
        assert choice is not None
        assert choice.builtin is False
        assert choice.label == "mine.png"

    def test_a_user_colour_icon_is_never_a_template(self, tmp_path):
        """Colour plus template=True renders a flat silhouette — reads as a bug."""
        target = tmp_path / "mine.png"
        target.write_bytes(b"data")
        assert icons.resolve_user_icon(str(target)).template is False

    def test_ico_is_accepted(self, tmp_path):
        target = tmp_path / "mine.ico"
        target.write_bytes(b"data")
        assert icons.resolve_user_icon(str(target)) is not None

    def test_svg_is_refused(self, tmp_path):
        """Neither rumps nor pystray can rasterize one."""
        target = tmp_path / "mine.svg"
        target.write_text("<svg/>", encoding="utf-8")
        assert icons.resolve_user_icon(str(target)) is None

    def test_relative_path_is_refused(self):
        assert icons.resolve_user_icon("assets/raven.png") is None

    def test_missing_file_is_refused(self, tmp_path):
        assert icons.resolve_user_icon(str(tmp_path / "absent.png")) is None

    def test_directory_is_refused(self, tmp_path):
        target = tmp_path / "dir.png"
        target.mkdir()
        assert icons.resolve_user_icon(str(target)) is None

    def test_oversized_file_is_refused(self, tmp_path):
        target = tmp_path / "huge.png"
        target.write_bytes(b"0" * (icons.MAX_ICON_BYTES + 1))
        assert icons.resolve_user_icon(str(target)) is None

    def test_tilde_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "mine.png"
        target.write_bytes(b"data")
        assert icons.resolve_user_icon("~/mine.png") is not None


# ── Config persistence ────────────────────────────────────────────────────────

class TestConfig:
    def test_no_config_means_no_setting(self, config):
        assert icons.configured_icon() == ""

    def test_round_trip(self, config):
        icons.set_icon("appistry")
        assert icons.configured_icon() == "appistry"

    def test_overwrite(self, config):
        icons.set_icon("appistry")
        icons.set_icon("raven")
        assert icons.configured_icon() == "raven"

    def test_clear_reverts_to_no_setting(self, config):
        icons.set_icon("appistry")
        icons.clear_icon()
        assert icons.configured_icon() == ""

    def test_a_user_path_round_trips(self, config, tmp_path):
        target = tmp_path / "mine.png"
        target.write_bytes(b"data")
        icons.set_icon(str(target))
        assert icons.configured_icon() == str(target)

    def test_windows_backslashes_survive(self, config):
        r"""A raw C:\ path must not become an invalid TOML escape."""
        icons.set_icon(r"C:\Users\alice\icon.png")
        assert icons.configured_icon() == r"C:\Users\alice\icon.png"

    def test_quotes_survive(self, config):
        icons.set_icon('/tmp/my "icon".png')
        assert icons.configured_icon() == '/tmp/my "icon".png'

    def test_a_control_character_does_not_break_the_file(self, config):
        """An unparseable config would also disable the verb that fixes it."""
        icons.set_icon("bad\nvalue\x00here")
        assert icons.configured_icon() == "bad\nvalue\x00here"

    def test_an_unparseable_config_degrades_to_defaults(self, config):
        icons.config_path().write_text("this is not [ toml", encoding="utf-8")
        assert icons.configured_icon() == ""
        assert icons.resolve() is not None

    def test_config_is_written_owner_only(self, config):
        import stat
        if sys.platform == "win32":
            pytest.skip("POSIX mode bits are not meaningful on Windows")
        icons.set_icon("raven")
        mode = stat.S_IMODE(icons.config_path().stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_unrelated_keys_are_preserved(self, config):
        icons.config_path().parent.mkdir(parents=True, exist_ok=True)
        icons.config_path().write_text('other = "keep me"\n', encoding="utf-8")
        icons.set_icon("raven")
        assert 'other = "keep me"' in icons.config_path().read_text(encoding="utf-8")


# ── Resolution and the submenu ────────────────────────────────────────────────

class TestResolve:
    def test_default_is_the_raven(self, config, assets):
        assert icons.resolve().name == "raven"

    def test_configured_builtin_wins(self, config, assets):
        icons.set_icon("appistry")
        assert icons.resolve().name == "appistry"

    def test_configured_user_icon_wins(self, config, tmp_path, assets):
        target = tmp_path / "mine.png"
        target.write_bytes(b"data")
        icons.set_icon(str(target))
        choice = icons.resolve()
        assert choice.builtin is False
        assert choice.path == target

    def test_a_deleted_user_icon_falls_back_to_the_default(self, config, tmp_path, assets):
        """Better the raven than a tray with no icon at all."""
        target = tmp_path / "mine.png"
        target.write_bytes(b"data")
        icons.set_icon(str(target))
        target.unlink()
        assert icons.resolve().name == "raven"

    def test_an_unknown_builtin_falls_back_to_the_default(self, config, assets):
        icons.set_icon("nonexistent")
        assert icons.resolve().name == "raven"

    def test_an_explicit_value_overrides_the_config(self, config, assets):
        icons.set_icon("raven")
        assert icons.resolve("appistry").name == "appistry"

    def test_no_assets_at_all_resolves_to_none(self, config, monkeypatch, tmp_path):
        monkeypatch.setattr(icons, "ASSETS_DIR", tmp_path / "absent")
        assert icons.resolve() is None


class TestChoices:
    def test_builtins_are_offered_with_the_default_first(self, config, assets):
        assert [choice.name for choice in icons.choices()] == ["raven", "appistry"]

    def test_a_configured_user_icon_joins_the_list(self, config, tmp_path, assets):
        target = tmp_path / "mine.png"
        target.write_bytes(b"data")
        icons.set_icon(str(target))
        labels = [choice.label for choice in icons.choices()]
        assert "mine.png" in labels

    def test_a_configured_builtin_is_not_duplicated(self, config, assets):
        icons.set_icon("appistry")
        names = [choice.name for choice in icons.choices()]
        assert names.count("appistry") == 1

    def test_the_active_choice_is_identifiable(self, config, assets):
        icons.set_icon("appistry")
        active = icons.resolve()
        marked = [
            choice.name for choice in icons.choices()
            if icons.is_active(choice, active)
        ]
        assert marked == ["appistry"]

    def test_is_active_handles_no_active_icon(self, assets):
        choice = icons.resolve_builtin("raven")
        assert icons.is_active(choice, None) is False
