"""The platform-neutral shape of the rendered menu, shared by both trays.

macOS (rumps) and Windows (pystray) build menus with completely different APIs,
but they must show the *same menu*. The previous design in this repository let
each tray assemble its own structure from raw state, and the two drifted — right
down to each one separately hardcoding a special case for one particular
participant's id.

So the decision about what the menu contains is made exactly once, here, and
produces a flat list of :class:`Row` values. Each tray does nothing but turn rows
into its own widgets. A change to the menu's content is a change to this file and
lands on both platforms at once; a change to how a row looks on one platform
cannot silently change what the other platform shows.

Nothing here interprets a raven's data. Labels come from the raven, already
sanitised by :mod:`menu_spec`; ids are opaque strings this module only carries
back to :func:`host.activate`. The host contributes the ordering (which comes
from the ravens' own declared priority), the wording of its own rows, and
nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from roost import host
from roost import menu_spec
from roost import sanitize


class RowKind(Enum):
    """What a row is for. A tray switches on this and nothing else."""

    #: A raven's name, introducing its rows. Never clickable.
    RAVEN = "raven"
    #: Why a raven cannot be used. Never clickable, always visible.
    REASON = "reason"
    #: A section title inside a raven's contribution. Never clickable.
    SECTION = "section"
    #: A row the raven published. Clickable when the raven said so.
    ITEM = "item"
    #: A visual divider.
    SEPARATOR = "separator"
    #: One of Roost's own rows (Help, Quit, the icon submenu's parent).
    HOST = "host"


#: Markers prefixed to an item's label for each style a raven may request. The
#: raven names an intent; the host chooses the presentation. A raven cannot
#: supply a marker of its own, because that would be styling by another name.
STYLE_MARKERS = {
    "attention": "● ",
    "muted": "· ",
    "normal": "",
}

#: Shown in place of a raven's rows when no raven has published a descriptor at
#: all. Distinct from a raven that is present but unreachable: that one gets its
#: own name and its own reason.
NO_RAVENS_LABEL = "No ravens are running"

HELP_LABEL = "Help"
QUIT_LABEL = "Quit Roost"


@dataclass(frozen=True)
class Row:
    """One line of the rendered menu."""

    kind: RowKind
    label: str = ""
    #: Set on :attr:`RowKind.HOST` rows so a tray can bind the right callback
    #: without matching on display text.
    action: str = ""
    #: The raven this row belongs to, for :attr:`RowKind.ITEM` rows.
    raven: str = ""
    #: The published item, for :attr:`RowKind.ITEM` rows.
    item: "menu_spec.MenuItem | None" = None
    enabled: bool = False
    #: Nested rows. Only the icon submenu uses these today.
    children: tuple["Row", ...] = ()
    #: True for the icon submenu entry currently in use, so a tray can mark it.
    #: Part of the row rather than looked up at draw time, so the value that is
    #: drawn is the same one :func:`signature` compared.
    checked: bool = False


def item_label(item: "menu_spec.MenuItem") -> str:
    """Return the display text for a published item.

    The label and detail were sanitised when the menu was parsed; joining them
    here is the only presentation decision, and it is made once for both
    platforms so a row cannot read differently on macOS than on Windows.
    """
    marker = STYLE_MARKERS.get(item.style, "")
    if item.detail:
        return f"{marker}{item.label} — {item.detail}"
    return f"{marker}{item.label}"


def raven_label(menu: "menu_spec.RavenMenu") -> str:
    """Return the header text for one raven, including its badge count.

    The badge is the raven's own number — how many things it says want
    attention. Roost does not compute it and does not know what it counts.
    """
    display = menu.display or menu.name or "Unknown raven"
    if menu.available and menu.spec.badge:
        return f"{display} ({menu.spec.badge})"
    return display


def raven_rows(menu: "menu_spec.RavenMenu") -> list[Row]:
    """Return the rows for one raven: its name, then its content or its reason."""
    rows = [Row(RowKind.RAVEN, label=raven_label(menu))]

    if not menu.available:
        # An unreachable, stale, or malformed raven renders as a disabled row
        # with a visible reason. Omitting it would be worse than showing it
        # broken: a raven that vanishes from the menu looks like one that was
        # never installed, and the user has nothing to act on.
        rows.append(Row(RowKind.REASON, label=menu.reason))
        return rows

    if menu.spec.is_empty:
        rows.append(Row(RowKind.REASON, label="Nothing to report."))
        return rows

    for section in menu.spec.sections:
        if section.title:
            rows.append(Row(RowKind.SECTION, label=section.title))
        for item in section.items:
            if item.separator:
                rows.append(Row(RowKind.SEPARATOR))
                continue
            rows.append(
                Row(
                    RowKind.ITEM,
                    label=item_label(item),
                    raven=menu.name,
                    item=item,
                    enabled=item.clickable,
                )
            )
    return rows


def build_rows(model: "host.MenuModel") -> list[Row]:
    """Return every row of the whole menu, in the order it should be drawn.

    The ravens come first, in the order the model already holds — which is the
    order the ravens' own ``host_priority`` produced. Roost's own rows follow.
    """
    rows: list[Row] = []

    if model.menus:
        for index, menu in enumerate(model.menus):
            if index:
                rows.append(Row(RowKind.SEPARATOR))
            rows.extend(raven_rows(menu))
    else:
        rows.append(Row(RowKind.REASON, label=NO_RAVENS_LABEL))

    rows.append(Row(RowKind.SEPARATOR))
    rows.append(Row(RowKind.HOST, label=HELP_LABEL, action="help", enabled=True))
    rows.append(Row(RowKind.SEPARATOR))
    rows.append(Row(RowKind.HOST, label=QUIT_LABEL, action="quit", enabled=True))
    return rows


def signature(rows: list[Row]) -> tuple:
    """A hashable summary of the rendered menu, used to skip idle rebuilds.

    Built from the rows rather than from the model so that anything affecting
    what is on screen — including the icon list — forces a rebuild. Summarising
    the model instead would let a change reach the model but never the menu.
    """
    return tuple(
        (row.kind.value, row.label, row.action, row.raven, row.enabled,
         row.checked,
         tuple((child.label, child.action, child.checked) for child in row.children))
        for row in rows
    )
