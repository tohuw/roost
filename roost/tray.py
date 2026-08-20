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

Nothing here interprets a bird's data. Labels come from the bird, already
sanitised by :mod:`menu_spec`; ids are opaque strings this module only carries
back to :func:`host.activate`. The host contributes the ordering (which comes
from the birds' own declared priority), the wording of its own rows, and
nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from roost import host
from roost import menu_spec
from roost import launcher
from roost import sanitize


class RowKind(Enum):
    """What a row is for. A tray switches on this and nothing else."""

    #: A bird's name, introducing its rows. Never clickable.
    BIRD = "bird"
    #: Why a bird cannot be used. Never clickable, always visible.
    REASON = "reason"
    #: A section title inside a bird's contribution. Never clickable.
    SECTION = "section"
    #: A row the bird published. Clickable when the bird said so.
    ITEM = "item"
    #: A visual divider.
    SEPARATOR = "separator"
    #: One of Roost's own rows (Help, Quit, the icon submenu's parent).
    HOST = "host"


#: Markers prefixed to an item's label for each style a bird may request. The
#: bird names an intent; the host chooses the presentation. A bird cannot
#: supply a marker of its own, because that would be styling by another name.
STYLE_MARKERS = {
    "attention": "● ",
    "muted": "· ",
    "normal": "",
}

#: Shown in place of a bird's rows when no bird has published a descriptor at
#: all. Distinct from a bird that is present but unreachable: that one gets its
#: own name and its own reason.
NO_BIRDS_LABEL = "No birds are running"

HELP_LABEL = "Help"
START_LABEL = "Start"
QUIT_LABEL = "Quit Roost"


@dataclass(frozen=True)
class Row:
    """One line of the rendered menu."""

    kind: RowKind
    label: str = ""
    #: Set on :attr:`RowKind.HOST` rows so a tray can bind the right callback
    #: without matching on display text.
    action: str = ""
    #: The bird this row belongs to, for :attr:`RowKind.ITEM` rows.
    bird: str = ""
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


def bird_label(menu: "menu_spec.BirdMenu") -> str:
    """Return the header text for one bird, including its badge count.

    The badge is the bird's own number — how many things it says want
    attention. Roost does not compute it and does not know what it counts.
    """
    display = menu.display or menu.name or "Unknown bird"
    if menu.available and menu.spec.badge:
        return f"{display} ({menu.spec.badge})"
    return display


def bird_rows(menu: "menu_spec.BirdMenu") -> list[Row]:
    """Return the rows for one bird: its name, then its content or its reason."""
    rows = [Row(RowKind.BIRD, label=bird_label(menu))]

    if not menu.available:
        # An unreachable, stale, or malformed bird renders as a disabled row
        # with a visible reason. Omitting it would be worse than showing it
        # broken: a bird that vanishes from the menu looks like one that was
        # never installed, and the user has nothing to act on.
        rows.append(Row(RowKind.REASON, label=menu.reason))
        # ...and a reason on its own was still a dead end. A bird that says
        # how to start it gets a row that does, because "its process is gone"
        # with nothing to click is the state a status menu is least useful in.
        if menu.launch is not None and launcher.supported_here(menu.launch):
            rows.append(
                Row(RowKind.HOST, label=f"{START_LABEL} {menu.display}",
                    action=f"start:{menu.name}", enabled=True)
            )
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
                    bird=menu.name,
                    item=item,
                    enabled=item.clickable,
                )
            )
    return rows


def build_rows(model: "host.MenuModel") -> list[Row]:
    """Return every row of the whole menu, in the order it should be drawn.

    The birds come first, in the order the model already holds — which is the
    order the birds' own ``host_priority`` produced. Roost's own rows follow.
    """
    rows: list[Row] = []

    if model.menus:
        for index, menu in enumerate(model.menus):
            if index:
                rows.append(Row(RowKind.SEPARATOR))
            rows.extend(bird_rows(menu))
    else:
        rows.append(Row(RowKind.REASON, label=NO_BIRDS_LABEL))

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
        (row.kind.value, row.label, row.action, row.bird, row.enabled,
         row.checked,
         tuple((child.label, child.action, child.checked) for child in row.children))
        for row in rows
    )
