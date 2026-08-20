"""Menu-as-data parsing tests.

The contract under test is that Roost renders a bird's menu without
interpreting it, and that it cannot be made to render something dangerous or
unbounded. So: labels are sanitised, action ids are forwarded opaquely, URLs stay
bird-local, and every dimension of the payload is bounded.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import menu_spec
from roost import birds
from roost import sanitize


def _section(*items, **overrides):
    section = {"id": "main", "title": "Sessions", "items": list(items)}
    section.update(overrides)
    return section


def _payload(*sections, **overrides):
    payload = {"api_version": 1, "title": "Huginn", "sections": list(sections)}
    payload.update(overrides)
    return payload


class TestParseItem:
    def test_action_item(self):
        item = menu_spec.parse_item({"id": "focus:abc", "label": "Approve: deploy"})
        assert item.label == "Approve: deploy"
        assert item.action_id == "focus:abc"
        assert item.clickable is True

    def test_link_item(self):
        item = menu_spec.parse_item({"label": "Open Console", "url": "/#t=x"})
        assert item.url == ""  # a fragment is refused
        item = menu_spec.parse_item({"label": "Open Console", "url": "/console"})
        assert item.url == "/console"
        assert item.clickable is True

    def test_separator(self):
        item = menu_spec.parse_item({"separator": True})
        assert item.separator is True
        assert item.clickable is False

    def test_detail_and_style_are_carried(self):
        item = menu_spec.parse_item({
            "id": "a", "label": "Row", "detail": "claude", "style": "attention",
        })
        assert item.detail == "claude"
        assert item.style == "attention"

    def test_unknown_style_falls_back_to_normal(self):
        """A bird cannot supply arbitrary presentation."""
        item = menu_spec.parse_item({"id": "a", "label": "Row", "style": "<script>"})
        assert item.style == "normal"

    def test_unknown_fields_are_dropped_not_rejected(self):
        """A newer bird in range must be able to add a field, per huginn #38."""
        item = menu_spec.parse_item({
            "id": "a", "label": "Row", "future_field": {"nested": [1, 2]},
        })
        assert item is not None
        assert item.label == "Row"

    def test_item_with_no_action_is_inert_and_disabled(self):
        """A live-looking row that does nothing is worse than an honest one."""
        item = menu_spec.parse_item({"label": "Just text"})
        assert item.enabled is False
        assert item.clickable is False

    def test_explicitly_disabled_item_stays_disabled(self):
        item = menu_spec.parse_item({"id": "a", "label": "Row", "enabled": False})
        assert item.enabled is False

    def test_enabled_must_be_a_real_bool(self):
        """"false" is truthy; treating it as truth would enable a disabled row."""
        item = menu_spec.parse_item({"id": "a", "label": "Row", "enabled": "false"})
        assert item.enabled is True  # non-bool falls back to the default
        item = menu_spec.parse_item({"id": "a", "label": "Row", "enabled": 0})
        assert item.enabled is True

    @pytest.mark.parametrize("raw", [None, "string", 42, [], object()])
    def test_non_dict_items_are_dropped(self, raw):
        assert menu_spec.parse_item(raw) is None

    def test_item_with_no_label_is_dropped(self):
        assert menu_spec.parse_item({"id": "a"}) is None
        assert menu_spec.parse_item({"id": "a", "label": ""}) is None

    def test_item_whose_label_is_all_control_characters_is_dropped(self):
        assert menu_spec.parse_item({"id": "a", "label": "\x1b[2J\x00"}) is None


class TestLabelSanitising:
    @pytest.mark.parametrize("hostile", [
        "Quit\x1b[2J\x1b[H All",
        "Quit\r\nQuit All",
        "Quit\x00All",
        "Qu‮it",
        "Quit\x07",
    ])
    def test_hostile_labels_are_cleaned(self, hostile):
        item = menu_spec.parse_item({"id": "a", "label": hostile})
        assert item is not None
        assert not sanitize.contains_unsafe_text(item.label)
        assert "\n" not in item.label and "\r" not in item.label

    def test_hostile_detail_is_cleaned(self):
        item = menu_spec.parse_item({"id": "a", "label": "Row", "detail": "\x1b[31mx"})
        assert item.detail == "x"

    def test_hostile_section_title_is_cleaned(self):
        spec = menu_spec.parse_menu(_payload(
            _section({"id": "a", "label": "Row"}, title="\x1b[31mSessions")
        ))
        assert spec.sections[0].title == "Sessions"

    def test_hostile_menu_title_is_cleaned(self):
        spec = menu_spec.parse_menu(_payload(
            _section({"id": "a", "label": "Row"}), title="Hu\x00ginn",
        ))
        assert spec.title == "Huginn"

    def test_labels_are_length_capped(self):
        item = menu_spec.parse_item({"id": "a", "label": "z" * 5000})
        assert len(item.label) <= sanitize.DEFAULT_LABEL_LIMIT

    def test_detail_has_its_own_shorter_cap(self):
        item = menu_spec.parse_item({"id": "a", "label": "Row", "detail": "z" * 5000})
        assert len(item.detail) <= menu_spec.MAX_DETAIL_LENGTH


class TestActionIdForwarding:
    def test_action_id_is_opaque_to_roost(self):
        """Roost does not parse the id; it round-trips whatever the bird sent."""
        weird = "session/abc-123:focus?x=1&y=2"
        item = menu_spec.parse_item({"id": weird, "label": "Row"})
        assert item.action_id == weird

    @pytest.mark.parametrize("bad", [
        "a\r\nX-Evil: 1",
        "a\x00b",
        "\x1b[31ma",
        "z" * (menu_spec.MAX_ACTION_ID_LENGTH + 1),
        "",
        None,
        42,
        [],
    ])
    def test_unsafe_action_ids_make_the_item_inert(self, bad):
        """The row stays visible but disabled, so the breakage is not hidden."""
        item = menu_spec.parse_item({"id": bad, "label": "Row"})
        assert item is not None
        assert item.action_id == ""
        assert item.enabled is False


class TestUrlValidation:
    @pytest.mark.parametrize("bad", [
        "http://evil.example/",
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example/",
        "javascript:alert(1)",
        "relative/path",
        "/a/../../etc/passwd",
        "/path#frag",
        "/path\x00",
        "z" * (menu_spec.MAX_URL_LENGTH + 1),
        "",
        None,
        42,
    ])
    def test_non_local_urls_are_refused(self, bad):
        item = menu_spec.parse_item({"label": "Row", "url": bad})
        assert item.url == ""
        assert item.enabled is False

    @pytest.mark.parametrize("good", ["/", "/console", "/api/x?y=1", "/a/b/c"])
    def test_local_urls_are_accepted(self, good):
        item = menu_spec.parse_item({"label": "Row", "url": good})
        assert item.url == good


class TestParseSection:
    def test_section_requires_items(self):
        section, _ = menu_spec.parse_section({"id": "a", "title": "T"}, 100)
        assert section is None

    def test_section_items_must_be_a_list(self):
        section, _ = menu_spec.parse_section({"items": {"a": 1}}, 100)
        assert section is None

    def test_section_of_only_dropped_items_is_dropped(self):
        section, _ = menu_spec.parse_section({"items": [{"label": ""}, None]}, 100)
        assert section is None

    def test_leading_and_duplicate_separators_are_collapsed(self):
        section, _ = menu_spec.parse_section({"items": [
            {"separator": True},
            {"separator": True},
            {"id": "a", "label": "A"},
            {"separator": True},
            {"separator": True},
            {"id": "b", "label": "B"},
        ]}, 100)
        kinds = [item.separator for item in section.items]
        assert kinds == [False, True, False]

    def test_trailing_separators_are_removed(self):
        section, _ = menu_spec.parse_section({"items": [
            {"id": "a", "label": "A"}, {"separator": True},
        ]}, 100)
        assert [item.separator for item in section.items] == [False]

    def test_items_per_section_are_capped(self):
        raw = [{"id": f"a{i}", "label": f"Row {i}"} for i in range(500)]
        section, _ = menu_spec.parse_section({"items": raw}, 10_000)
        assert len(section.items) <= menu_spec.MAX_ITEMS_PER_SECTION

    def test_budget_is_consumed_and_returned(self):
        raw = [{"id": f"a{i}", "label": f"Row {i}"} for i in range(5)]
        section, remaining = menu_spec.parse_section({"items": raw}, 100)
        assert len(section.items) == 5
        assert remaining == 95

    def test_exhausted_budget_yields_nothing(self):
        section, remaining = menu_spec.parse_section(
            {"items": [{"id": "a", "label": "A"}]}, 0
        )
        assert section is None
        assert remaining == 0


class TestParseMenu:
    def test_full_payload(self):
        spec = menu_spec.parse_menu(_payload(
            _section({"id": "focus:1", "label": "Approve"}),
            badge=3,
        ))
        assert spec.title == "Huginn"
        assert spec.badge == 3
        assert len(spec.sections) == 1
        assert spec.is_empty is False

    @pytest.mark.parametrize("raw", [None, "text", 42, [], object()])
    def test_non_object_payload_yields_an_empty_spec(self, raw):
        spec = menu_spec.parse_menu(raw)
        assert spec.is_empty is True
        assert spec.sections == ()

    def test_missing_sections_keeps_the_title(self):
        spec = menu_spec.parse_menu({"title": "Muninn"})
        assert spec.title == "Muninn"
        assert spec.is_empty is True

    def test_sections_must_be_a_list(self):
        spec = menu_spec.parse_menu({"title": "M", "sections": {"a": 1}})
        assert spec.sections == ()

    def test_sections_are_capped(self):
        sections = [
            _section({"id": f"a{i}", "label": "Row"}, id=f"s{i}")
            for i in range(100)
        ]
        spec = menu_spec.parse_menu(_payload(*sections))
        assert len(spec.sections) <= menu_spec.MAX_SECTIONS

    def test_total_items_are_capped_across_sections(self):
        """An oversized payload must not be fully built and then discarded."""
        sections = [
            _section(
                *[{"id": f"a{i}-{j}", "label": f"Row {j}"} for j in range(50)],
                id=f"s{i}",
            )
            for i in range(12)
        ]
        spec = menu_spec.parse_menu(_payload(*sections))
        total = sum(len(section.items) for section in spec.sections)
        assert total <= menu_spec.MAX_TOTAL_ITEMS

    @pytest.mark.parametrize("badge,expected", [
        (5, 5), (0, 0), (-1, 0), (100_000, 0), ("3", 0), (True, 0), (None, 0), (1.5, 0),
    ])
    def test_badge_bounds(self, badge, expected):
        spec = menu_spec.parse_menu(_payload(
            _section({"id": "a", "label": "Row"}), badge=badge,
        ))
        assert spec.badge == expected

    def test_deeply_nested_payload_does_not_recurse(self):
        """Nesting is not part of the schema, so depth cannot be weaponised."""
        nested = {"items": [{"id": "a", "label": "A"}]}
        for _ in range(2000):
            nested = {"items": [nested]}
        spec = menu_spec.parse_menu({"sections": [nested]})
        assert spec.sections == ()


class TestBirdMenu:
    def test_unavailable_carries_a_reason(self):
        bird = birds.UnavailableBird("muninn", "Muninn", "Not running.", None)
        menu = menu_spec.unavailable(bird)
        assert menu.available is False
        assert menu.reason == "Not running."
        assert menu.display == "Muninn"

    def test_unavailable_reason_is_sanitised(self):
        bird = birds.UnavailableBird("m", "\x1b[31mM", "\x1b[2Jgone", None)
        menu = menu_spec.unavailable(bird)
        assert not sanitize.contains_unsafe_text(menu.reason)
        assert not sanitize.contains_unsafe_text(menu.display)

    def test_unavailable_always_has_some_reason(self):
        bird = birds.UnavailableBird("m", "M", "", None)
        assert menu_spec.unavailable(bird).reason == "Unavailable."

    def test_unavailable_falls_back_to_a_name(self):
        bird = birds.UnavailableBird("muninn", "\x00", "why", None)
        assert menu_spec.unavailable(bird).display == "muninn"

    def test_signature_changes_when_a_label_changes(self):
        first = menu_spec.BirdMenu("h", "H", menu_spec.parse_menu(
            _payload(_section({"id": "a", "label": "One"}))))
        second = menu_spec.BirdMenu("h", "H", menu_spec.parse_menu(
            _payload(_section({"id": "a", "label": "Two"}))))
        assert first.signature() != second.signature()

    def test_signature_is_stable_for_identical_input(self):
        payload = _payload(_section({"id": "a", "label": "One"}))
        first = menu_spec.BirdMenu("h", "H", menu_spec.parse_menu(payload))
        second = menu_spec.BirdMenu("h", "H", menu_spec.parse_menu(payload))
        assert first.signature() == second.signature()

    def test_signature_changes_when_a_reason_appears(self):
        ok = menu_spec.BirdMenu("h", "H", menu_spec.parse_menu(
            _payload(_section({"id": "a", "label": "One"}))))
        broken = menu_spec.BirdMenu("h", "H", reason="Not running.")
        assert ok.signature() != broken.signature()

    def test_signature_is_hashable(self):
        menu = menu_spec.BirdMenu("h", "H", menu_spec.parse_menu(
            _payload(_section({"id": "a", "label": "One"}))))
        assert hash(menu.signature())
