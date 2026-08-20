"""Tests for the platform-neutral menu shape both trays render.

This module is where the decision about what the menu contains lives, so it is
where the rules are pinned: an unavailable bird is shown with its reason rather
than dropped, the ordering is the birds' own, nothing here interprets a bird's
data, and no bird's name or id is special-cased.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import host
from roost import menu_spec
from roost import birds
from roost import tray
from roost.tray import RowKind


def _descriptor(name="huginn", **overrides):
    values = dict(
        name=name, display=name.title(), api_version=1, min_api=1, max_api=1,
        pid=1, port=47100, token_path=None, token_header="", endpoints={},
        host_priority=0, started=None, path=Path(f"/tmp/{name}.json"),
    )
    values.update(overrides)
    return birds.BirdDescriptor(**values)


def _menu(name="huginn", *labels, badge=0, title="", reason="", section="Sessions"):
    if reason:
        return menu_spec.BirdMenu(name=name, display=name.title(), reason=reason)
    items = tuple(
        menu_spec.MenuItem(label=label, action_id=f"act:{label}") for label in labels
    )
    spec = menu_spec.MenuSpec(
        title=title,
        badge=badge,
        sections=(menu_spec.MenuSection(id="s", title=section, items=items),) if items else (),
    )
    return menu_spec.BirdMenu(
        name=name, display=title or name.title(), spec=spec,
        descriptor=_descriptor(name),
    )


def _labels(rows, *kinds):
    wanted = set(kinds) or set(RowKind)
    return [row.label for row in rows if row.kind in wanted]


# ── Availability ──────────────────────────────────────────────────────────────

class TestUnavailableBirds:
    def test_an_unavailable_bird_is_shown_with_its_reason(self):
        rows = tray.bird_rows(_menu("muninn", reason="Is not answering."))
        assert [(row.kind, row.label) for row in rows] == [
            (RowKind.BIRD, "Muninn"),
            (RowKind.REASON, "Is not answering."),
        ]

    def test_an_unavailable_bird_is_never_clickable(self):
        rows = tray.bird_rows(_menu("muninn", reason="Is not answering."))
        assert all(row.enabled is False for row in rows)

    def test_an_unavailable_bird_is_not_dropped_from_the_model(self):
        """A bird that vanished would look like one that was never installed."""
        model = host.MenuModel((
            _menu("huginn", "Row"),
            _menu("muninn", reason="Not running."),
        ))
        rows = tray.build_rows(model)
        assert "Muninn" in _labels(rows, RowKind.BIRD)
        assert "Not running." in _labels(rows, RowKind.REASON)

    def test_one_broken_bird_does_not_disable_the_other(self):
        model = host.MenuModel((
            _menu("huginn", "Approve"),
            _menu("muninn", reason="Not running."),
        ))
        rows = tray.build_rows(model)
        clickable = [row for row in rows if row.kind is RowKind.ITEM and row.enabled]
        assert [row.bird for row in clickable] == ["huginn"]

    def test_a_bird_that_is_up_but_silent_says_so(self):
        """Distinct from unreachable: it answered, it just has nothing to report."""
        rows = tray.bird_rows(_menu("huginn"))
        assert [(row.kind, row.label) for row in rows] == [
            (RowKind.BIRD, "Huginn"),
            (RowKind.REASON, "Nothing to report."),
        ]

    def test_no_birds_at_all_is_its_own_message(self):
        rows = tray.build_rows(host.MenuModel())
        assert tray.NO_BIRDS_LABEL in _labels(rows, RowKind.REASON)
        assert not [row for row in rows if row.kind is RowKind.BIRD]


# ── Ordering and neutrality ───────────────────────────────────────────────────

class TestNeutrality:
    def test_the_order_is_the_models_order(self):
        """Ordering comes from the birds' declared priority, never from here."""
        model = host.MenuModel((_menu("muninn", "M"), _menu("huginn", "H")))
        assert _labels(tray.build_rows(model), RowKind.BIRD) == ["Muninn", "Huginn"]

    def test_no_bird_name_is_special_cased(self):
        """Regression: both trays used to hardcode one participant's id."""
        source = Path(tray.__file__).read_text(encoding="utf-8")
        for name in ("huginn", "muninn", "Huginn", "Muninn"):
            assert name not in source, name

    def test_an_unknown_bird_renders_exactly_like_a_known_one(self):
        known = tray.bird_rows(_menu("huginn", "Row"))
        unknown = tray.bird_rows(_menu("corvid-nine", "Row"))
        assert [row.kind for row in known] == [row.kind for row in unknown]

    def test_an_action_id_is_carried_not_interpreted(self):
        item = menu_spec.MenuItem(label="Approve", action_id="focus:abc123")
        spec = menu_spec.MenuSpec(
            sections=(menu_spec.MenuSection(items=(item,)),)
        )
        menu = menu_spec.BirdMenu(name="huginn", display="Huginn", spec=spec,
                                   descriptor=_descriptor())
        row = next(r for r in tray.bird_rows(menu) if r.kind is RowKind.ITEM)
        assert row.item is item
        assert row.item.action_id == "focus:abc123"


# ── Labels ────────────────────────────────────────────────────────────────────

class TestLabels:
    def test_a_detail_is_appended(self):
        item = menu_spec.MenuItem(label="Approve", detail="claude", action_id="a")
        assert tray.item_label(item) == "Approve — claude"

    def test_a_label_without_a_detail_stands_alone(self):
        assert tray.item_label(menu_spec.MenuItem(label="Approve", action_id="a")) == "Approve"

    @pytest.mark.parametrize("style,marker", [
        ("attention", "● "), ("muted", "· "), ("normal", ""),
    ])
    def test_each_style_gets_its_own_marker(self, style, marker):
        item = menu_spec.MenuItem(label="Row", action_id="a", style=style)
        assert tray.item_label(item) == f"{marker}Row"

    def test_an_unknown_style_gets_no_marker(self):
        """menu_spec normalises styles, so this is belt and braces."""
        item = menu_spec.MenuItem(label="Row", action_id="a", style="fancy")
        assert tray.item_label(item) == "Row"

    def test_a_badge_appears_beside_the_bird_name(self):
        assert tray.bird_label(_menu("huginn", "Row", badge=3)) == "Huginn (3)"

    def test_a_zero_badge_is_not_shown(self):
        assert tray.bird_label(_menu("huginn", "Row", badge=0)) == "Huginn"

    def test_an_unavailable_birds_badge_is_not_shown(self):
        menu = menu_spec.BirdMenu(
            name="huginn", display="Huginn", reason="Gone.",
            spec=menu_spec.MenuSpec(badge=9),
        )
        assert tray.bird_label(menu) == "Huginn"

    def test_a_nameless_bird_still_gets_a_label(self):
        menu = menu_spec.BirdMenu(name="", display="", reason="Bad descriptor.")
        assert tray.bird_label(menu) == "Unknown bird"

    def test_a_bird_may_retitle_its_own_section(self):
        assert tray.bird_label(_menu("huginn", "Row", title="Thought")) == "Thought"


# ── Structure ─────────────────────────────────────────────────────────────────

class TestStructure:
    def test_a_section_title_is_rendered_above_its_items(self):
        rows = tray.bird_rows(_menu("huginn", "One", "Two", section="Needs attention"))
        kinds = [row.kind for row in rows]
        assert kinds == [RowKind.BIRD, RowKind.SECTION, RowKind.ITEM, RowKind.ITEM]
        assert rows[1].label == "Needs attention"

    def test_an_untitled_section_contributes_no_title_row(self):
        rows = tray.bird_rows(_menu("huginn", "One", section=""))
        assert not [row for row in rows if row.kind is RowKind.SECTION]

    def test_a_published_separator_becomes_a_separator_row(self):
        spec = menu_spec.MenuSpec(sections=(menu_spec.MenuSection(items=(
            menu_spec.MenuItem(label="One", action_id="a"),
            menu_spec.MenuItem(separator=True),
            menu_spec.MenuItem(label="Two", action_id="b"),
        )),))
        menu = menu_spec.BirdMenu(name="huginn", display="Huginn", spec=spec,
                                   descriptor=_descriptor())
        kinds = [row.kind for row in tray.bird_rows(menu)]
        assert kinds == [RowKind.BIRD, RowKind.ITEM, RowKind.SEPARATOR, RowKind.ITEM]

    def test_birds_are_separated_from_each_other(self):
        model = host.MenuModel((_menu("huginn", "H"), _menu("muninn", "M")))
        rows = tray.build_rows(model)
        first_muninn = next(
            index for index, row in enumerate(rows)
            if row.kind is RowKind.BIRD and row.label == "Muninn"
        )
        assert rows[first_muninn - 1].kind is RowKind.SEPARATOR

    def test_the_hosts_own_rows_are_always_present(self):
        actions = [
            row.action for row in tray.build_rows(host.MenuModel())
            if row.kind is RowKind.HOST
        ]
        assert "help" in actions
        assert "quit" in actions

    def test_the_hosts_own_rows_come_last(self):
        model = host.MenuModel((_menu("huginn", "Row"),))
        rows = tray.build_rows(model)
        last_bird = max(
            index for index, row in enumerate(rows)
            if row.kind in (RowKind.BIRD, RowKind.ITEM, RowKind.REASON)
        )
        host_indexes = [
            index for index, row in enumerate(rows) if row.kind is RowKind.HOST
        ]
        assert min(host_indexes) > last_bird

    def test_quit_does_not_offer_to_stop_anything(self):
        """The birds are daemons the tray does not own, so there is no Quit All."""
        labels = [
            row.label for row in tray.build_rows(host.MenuModel())
            if row.kind is RowKind.HOST
        ]
        assert "Quit All" not in labels
        assert tray.QUIT_LABEL in labels


class TestSignature:
    def test_an_unchanged_menu_has_an_unchanged_signature(self):
        model = host.MenuModel((_menu("huginn", "Row"),))
        assert tray.signature(tray.build_rows(model)) == tray.signature(
            tray.build_rows(model)
        )

    def test_a_changed_label_changes_the_signature(self):
        first = tray.signature(tray.build_rows(host.MenuModel((_menu("huginn", "A"),))))
        second = tray.signature(tray.build_rows(host.MenuModel((_menu("huginn", "B"),))))
        assert first != second

    def test_a_changed_badge_changes_the_signature(self):
        first = tray.signature(
            tray.build_rows(host.MenuModel((_menu("huginn", "A", badge=1),)))
        )
        second = tray.signature(
            tray.build_rows(host.MenuModel((_menu("huginn", "A", badge=2),)))
        )
        assert first != second

    def test_a_changed_reason_changes_the_signature(self):
        first = tray.signature(
            tray.build_rows(host.MenuModel((_menu("m", reason="One."),)))
        )
        second = tray.signature(
            tray.build_rows(host.MenuModel((_menu("m", reason="Two."),)))
        )
        assert first != second

    def test_becoming_unavailable_changes_the_signature(self):
        up = tray.signature(tray.build_rows(host.MenuModel((_menu("huginn", "A"),))))
        down = tray.signature(
            tray.build_rows(host.MenuModel((_menu("huginn", reason="Gone."),)))
        )
        assert up != down

    def test_the_signature_is_hashable(self):
        assert isinstance(
            hash(tray.signature(tray.build_rows(host.MenuModel()))), int
        )
