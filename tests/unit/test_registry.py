"""Tests for registry.py — pure functions and I/O with a temp registry."""

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry
from registry import AppEntry, slugify, validate_app_id, bundle_name_for, _toml_str, _format_entry


# ── slugify ───────────────────────────────────────────────────────────────────

class TestSlugify:
    def test_basic(self):
        assert slugify("My App") == "my-app"

    def test_underscores_become_hyphens(self):
        assert slugify("my_app") == "my-app"

    def test_collapses_multiple_spaces(self):
        assert slugify("Hello   World") == "hello-world"

    def test_strips_special_chars(self):
        assert slugify("App (v2.0)!") == "app-v20"

    def test_leading_trailing_hyphens_stripped(self):
        assert slugify("  -my app- ") == "my-app"

    def test_already_lowercase(self):
        assert slugify("cortex") == "cortex"


# ── validate_app_id ───────────────────────────────────────────────────────────

class TestValidateAppId:
    def test_simple_slug_allowed(self):
        assert validate_app_id("widget") == "widget"

    def test_hyphenated_slug_allowed(self):
        assert validate_app_id("screenshot-sorter") == "screenshot-sorter"

    def test_digits_allowed(self):
        assert validate_app_id("app2") == "app2"

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError):
            validate_app_id("../../etc/passwd")

    def test_absolute_path_rejected(self):
        with pytest.raises(ValueError):
            validate_app_id("/etc/passwd")

    def test_shell_metacharacters_rejected(self):
        with pytest.raises(ValueError):
            validate_app_id('x";touch /tmp/pwn;"')

    def test_uppercase_rejected(self):
        with pytest.raises(ValueError):
            validate_app_id("MyApp")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            validate_app_id("")

    def test_none_rejected(self):
        with pytest.raises(ValueError):
            validate_app_id(None)

    def test_leading_hyphen_rejected(self):
        with pytest.raises(ValueError):
            validate_app_id("-app")

    def test_slash_in_middle_rejected(self):
        with pytest.raises(ValueError):
            validate_app_id("app/evil")


# ── bundle_name_for ───────────────────────────────────────────────────────────

class TestBundleNameFor:
    def test_simple_name_unchanged(self):
        assert bundle_name_for("Widget", "widget") == "Widget"

    def test_path_separators_stripped(self):
        name = bundle_name_for("../../etc/evil", "widget")
        assert "/" not in name
        assert ".." not in name

    def test_falls_back_to_id_when_name_sanitizes_empty(self):
        assert bundle_name_for("///", "widget") == "widget"

    def test_dots_collapsed(self):
        name = bundle_name_for("My..App", "myapp")
        assert ".." not in name

    def test_empty_name_rejects_unvalidated_fallback_id(self):
        # A sanitizes-to-nothing display name must not fall through to an
        # unvalidated id — e.g. a stale registry entry written before
        # validate_app_id() existed could carry a path-traversal id.
        with pytest.raises(ValueError):
            bundle_name_for("///", "../../tmp/evil")


# ── _toml_str ─────────────────────────────────────────────────────────────────

class TestTomlStr:
    def test_simple(self):
        assert _toml_str("hello") == '"hello"'

    def test_escapes_double_quote(self):
        assert _toml_str('say "hi"') == '"say \\"hi\\""'

    def test_escapes_backslash(self):
        assert _toml_str("C:\\path") == '"C:\\\\path"'

    def test_empty_string(self):
        assert _toml_str("") == '""'


# ── AppEntry round-trip ───────────────────────────────────────────────────────

class TestAppEntry:
    def _sample(self, **kwargs):
        defaults = dict(
            id="my-app",
            name="My App",
            cwd="/projects/my-app",
            command=".venv/bin/python server.py",
            port=8080,
            github_url="https://github.com/example/my-app",
        )
        defaults.update(kwargs)
        return AppEntry(**defaults)

    def test_from_dict_round_trip(self):
        e = self._sample()
        d = e.to_dict()
        e2 = AppEntry.from_dict(d)
        assert e2.id == e.id
        assert e2.name == e.name
        assert e2.port == e.port
        assert e2.github_url == e.github_url

    def test_from_dict_missing_github_url_defaults_empty(self):
        d = dict(id="x", name="X", cwd="/x", command="run", port=9000,
                 registered_at="2026-01-01T00:00:00")
        e = AppEntry.from_dict(d)
        assert e.github_url == ""

    def test_icon_optional(self):
        e = self._sample(icon=None)
        d = e.to_dict()
        assert "icon" not in d
        e = self._sample(icon="static/icon.png")
        d = e.to_dict()
        assert d["icon"] == "static/icon.png"

    def test_format_entry_contains_required_fields(self):
        e = self._sample()
        s = _format_entry(e)
        assert "[[apps]]" in s
        assert 'id' in s
        assert 'port         = 8080' in s
        assert "github_url" in s

    def test_format_entry_omits_icon_when_none(self):
        e = self._sample(icon=None)
        assert "icon" not in _format_entry(e)

    def test_format_entry_includes_icon_when_set(self):
        e = self._sample(icon="img/icon.png")
        assert "icon" in _format_entry(e)


# ── Registry I/O (uses a temp directory) ─────────────────────────────────────

@pytest.fixture()
def tmp_registry(tmp_path, monkeypatch):
    """Redirect APPISTRY_DIR and REGISTRY_PATH to a tmp directory."""
    monkeypatch.setattr(registry, "APPISTRY_DIR", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.toml")
    return tmp_path


def _entry(**kwargs):
    defaults = dict(
        id="app-a",
        name="App A",
        cwd="/projects/a",
        command=".venv/bin/python run.py",
        port=8001,
        github_url="https://github.com/ex/a",
        registered_at="2026-01-01T12:00:00",
    )
    defaults.update(kwargs)
    return AppEntry(**defaults)


class TestRegistryIO:
    def test_load_empty_when_no_file(self, tmp_registry):
        assert registry.load() == []

    def test_save_and_load_round_trip(self, tmp_registry):
        e = _entry()
        registry.save([e])
        loaded = registry.load()
        assert len(loaded) == 1
        assert loaded[0].id == "app-a"
        assert loaded[0].port == 8001

    def test_save_multiple(self, tmp_registry):
        entries = [_entry(id="a", name="A", port=8001),
                   _entry(id="b", name="B", port=8002)]
        registry.save(entries)
        loaded = registry.load()
        assert [e.id for e in loaded] == ["a", "b"]

    def test_get_found(self, tmp_registry):
        registry.save([_entry(id="target")])
        result = registry.get("target")
        assert result is not None
        assert result.id == "target"

    def test_get_not_found(self, tmp_registry):
        assert registry.get("missing") is None

    def test_upsert_insert(self, tmp_registry):
        e = _entry()
        registry.upsert(e)
        assert registry.get("app-a") is not None

    def test_upsert_update_preserves_registered_at(self, tmp_registry):
        original = _entry(registered_at="2026-01-01T00:00:00")
        registry.upsert(original)
        updated = _entry(port=9999, registered_at="2099-12-31T00:00:00")
        registry.upsert(updated)
        result = registry.get("app-a")
        assert result.port == 9999
        assert result.registered_at == "2026-01-01T00:00:00"  # original preserved

    def test_remove_existing(self, tmp_registry):
        registry.save([_entry()])
        assert registry.remove("app-a") is True
        assert registry.load() == []

    def test_remove_nonexistent(self, tmp_registry):
        assert registry.remove("ghost") is False

    def test_remove_leaves_others(self, tmp_registry):
        registry.save([_entry(id="a", name="A", port=8001),
                       _entry(id="b", name="B", port=8002)])
        registry.remove("a")
        remaining = registry.load()
        assert len(remaining) == 1
        assert remaining[0].id == "b"

    def test_github_url_survives_round_trip(self, tmp_registry):
        e = _entry(github_url="https://github.com/org/repo")
        registry.save([e])
        loaded = registry.load()[0]
        assert loaded.github_url == "https://github.com/org/repo"

    def test_empty_github_url_survives_round_trip(self, tmp_registry):
        e = _entry(github_url="")
        registry.save([e])
        loaded = registry.load()[0]
        assert loaded.github_url == ""


# ── Control characters must not brick the registry (F-7) ─────────────────────

class TestControlCharacterResilience:
    """A control character in a display name used to emit invalid TOML.

    Nothing then parsed the file, and because *every* entry point — tray
    startup, the tray poll loop, and every CLI verb including `unregister` —
    begins by loading the registry, the user was left with no in-app way to
    recover. Escaping is a data-integrity fix, not an escalation fix: escaped
    quotes already prevented forging a second `[[apps]]` block.
    """

    def test_newline_is_escaped(self):
        assert _toml_str("two\nlines") == '"two\\nlines"'

    def test_carriage_return_and_tab_are_escaped(self):
        assert _toml_str("a\rb\tc") == '"a\\rb\\tc"'

    def test_other_c0_controls_use_unicode_escapes(self):
        assert _toml_str("bell\x07here") == '"bell\\u0007here"'
        assert _toml_str("nul\x00here") == '"nul\\u0000here"'
        assert _toml_str("del\x7fhere") == '"del\\u007Fhere"'

    def test_backspace_and_formfeed_use_toml_shorthand(self):
        assert _toml_str("a\bb\fc") == '"a\\bb\\fc"'

    def test_newline_in_name_round_trips_through_save_and_load(self, tmp_registry):
        registry.save([_entry(name="Evil\nName")])

        loaded = registry.load()

        assert len(loaded) == 1, "registry became unparseable"
        assert loaded[0].name == "Evil\nName"

    def test_control_characters_in_every_string_field_round_trip(self, tmp_registry):
        registry.save([_entry(
            name="N\name",
            cwd="/tmp/c\twd",
            command="run\r.sh",
            github_url="https://github.com/o/r\x07",
            icon="ic\non.png",
        )])

        loaded = registry.load()[0]

        assert loaded.name == "N\name"
        assert loaded.cwd == "/tmp/c\twd"
        assert loaded.command == "run\r.sh"
        assert loaded.github_url == "https://github.com/o/r\x07"
        assert loaded.icon == "ic\non.png"

    def test_corrupt_registry_loads_as_empty(self, tmp_registry):
        registry.REGISTRY_PATH.write_text(
            '[[apps]]\nname = "unterminated\n', encoding="utf-8"
        )

        assert registry.load() == []

    def test_corrupt_registry_still_allows_recovery(self, tmp_registry):
        """`appistry unregister` must stay usable — it calls load() first."""
        registry.REGISTRY_PATH.write_text("this is not toml [[[", encoding="utf-8")

        assert registry.remove("anything") is False
        assert registry.get("anything") is None
        registry.save([_entry()])
        assert [e.id for e in registry.load()] == ["app-a"]

    def test_registry_with_invalid_utf8_loads_as_empty(self, tmp_registry):
        registry.REGISTRY_PATH.write_bytes(b'[[apps]]\nname = "\xff\xfe"\n')

        assert registry.load() == []

    def test_one_malformed_block_does_not_hide_valid_entries(self, tmp_registry):
        registry.REGISTRY_PATH.write_text(
            '[[apps]]\nid = "broken"\n\n'
            '[[apps]]\nid = "good"\nname = "Good"\ncwd = "/tmp/g"\n'
            'command = "run"\nport = 8100\n',
            encoding="utf-8",
        )

        assert [e.id for e in registry.load()] == ["good"]


# ── validate_name ─────────────────────────────────────────────────────────────

class TestValidateName:
    def test_ordinary_name_allowed(self):
        assert registry.validate_name("My App") == "My App"

    def test_non_ascii_name_allowed(self):
        assert registry.validate_name("Café ✨") == "Café ✨"

    @pytest.mark.parametrize("name", ["a\nb", "a\rb", "a\tb", "a\x00b", "a\x07b", "a\x7fb"])
    def test_control_characters_rejected(self, name):
        with pytest.raises(ValueError):
            registry.validate_name(name)

    @pytest.mark.parametrize("name", ["", "   ", None])
    def test_empty_name_rejected(self, name):
        with pytest.raises(ValueError):
            registry.validate_name(name)

    def test_absurdly_long_name_rejected(self):
        with pytest.raises(ValueError):
            registry.validate_name("x" * 201)
