"""Menu-as-data: the declarative menu spec Appistry fetches and renders.

Appistry renders a raven's menu **without interpreting it**. It draws labels and
forwards action ids back to the raven that published them. It does not know what
``focus-session`` means, it does not special-case any raven's id, and it never
decides what a raven's menu should contain. That rule is what lets a companion
change its own menu with no change here.

The wire shape, fetched from the descriptor's ``menu`` endpoint:

.. code-block:: json

    {
      "api_version": 1,
      "title": "Huginn",
      "badge": 2,
      "sections": [
        {
          "id": "attention",
          "title": "Needs attention",
          "items": [
            {"id": "focus:abc123", "label": "Approve: deploy", "detail": "claude",
             "enabled": true, "style": "attention"},
            {"separator": true},
            {"id": "open-console", "label": "Open Console", "url": "/"}
          ]
        }
      ]
    }

Every field is optional except a section's ``items`` and an item's ``label``.
Unknown fields are dropped rather than rejected: a newer raven inside the
compatible version range must be able to add a field without disabling itself
here — the same failure mode huginn issue #38 describes, one layer up.

An item carries either an ``id`` (Appistry POSTs it back to the raven's ``action``
endpoint) or a ``url`` (Appistry opens ``http://127.0.0.1:{port}{url}`` in the
browser). An item with neither is inert and renders disabled: a menu entry that
looks clickable and does nothing is worse than one that admits it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import ravens
import sanitize

log = logging.getLogger(__name__)

#: Bounds on a rendered menu. These are not politeness limits — a raven that
#: returns ten thousand items would hang the menu build inside the AppKit run
#: loop, which reads to the user as a frozen desktop.
MAX_SECTIONS = 12
MAX_ITEMS_PER_SECTION = 50
MAX_TOTAL_ITEMS = 200
MAX_DETAIL_LENGTH = 80
MAX_ACTION_ID_LENGTH = 128
MAX_URL_LENGTH = 512

#: Styles a raven may request. Appistry maps these to its own presentation; a
#: raven cannot supply arbitrary styling, because that would be interpretation of
#: raven data by another name.
STYLES = ("normal", "attention", "muted")


@dataclass(frozen=True)
class MenuItem:
    """One rendered row. Either an action, a link, a separator, or inert."""

    label: str = ""
    action_id: str = ""
    url: str = ""
    detail: str = ""
    enabled: bool = True
    style: str = "normal"
    separator: bool = False

    @property
    def clickable(self) -> bool:
        return self.enabled and bool(self.action_id or self.url) and not self.separator


@dataclass(frozen=True)
class MenuSection:
    """A titled group of rows contributed by one raven."""

    id: str = ""
    title: str = ""
    items: tuple[MenuItem, ...] = ()


@dataclass(frozen=True)
class MenuSpec:
    """A whole raven's menu contribution, already sanitised and bounded."""

    title: str = ""
    badge: int = 0
    sections: tuple[MenuSection, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(section.items for section in self.sections)


def _coerce_bool(raw: object, default: bool) -> bool:
    """Return a real bool, treating anything non-bool as the default.

    Deliberately not truthiness: ``"false"`` is a non-empty string and would
    enable an item the raven meant to disable.
    """
    return raw if isinstance(raw, bool) else default


def _coerce_style(raw: object) -> str:
    return raw if isinstance(raw, str) and raw in STYLES else "normal"


def _coerce_badge(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if 0 <= raw <= 9999 else 0


def _parse_action_id(raw: object) -> str:
    """Return an action id Appistry is willing to send back to the raven.

    The id is opaque to Appistry — it means whatever the raven says it means —
    but it is still put on the wire, so it must not carry control characters or
    exceed a sane length. A rejected id makes the item inert rather than
    dropping the row, so the user sees that something is there and broken.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    if len(raw) > MAX_ACTION_ID_LENGTH:
        return ""
    if sanitize.contains_unsafe_text(raw) or any(ch in raw for ch in "\r\n"):
        return ""
    return raw


def _parse_url(raw: object) -> str:
    """Return a raven-local URL path, or "" if it is not one.

    Same reasoning as descriptor endpoints: the value is joined onto the raven's
    own loopback origin, so a scheme or an authority would send the user's
    browser somewhere the raven does not control. Query strings are permitted
    here (unlike in the descriptor) because a menu link legitimately carries
    parameters; a fragment is not, since Appistry appends nothing after it.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    if len(raw) > MAX_URL_LENGTH:
        return ""
    if sanitize.contains_unsafe_text(raw):
        return ""
    if not raw.startswith("/") or raw[1:2] in ("/", "\\"):
        return ""
    path = raw.split("?", 1)[0]
    if ".." in path.split("/"):
        return ""
    if "#" in raw:
        return ""
    return raw


def parse_item(raw: object) -> MenuItem | None:
    """Parse one item. Returns None for a row that cannot be rendered at all."""
    if not isinstance(raw, dict):
        return None

    if _coerce_bool(raw.get("separator"), False):
        return MenuItem(separator=True)

    label = sanitize.sanitize_label(raw.get("label"))
    if not label:
        # A row with no legible label is not a row. Dropping it is right here:
        # unlike a broken action, there is nothing to show the user.
        return None

    action_id = _parse_action_id(raw.get("id"))
    url = _parse_url(raw.get("url"))
    requested_enabled = _coerce_bool(raw.get("enabled"), True)

    return MenuItem(
        label=label,
        action_id=action_id,
        url=url,
        detail=sanitize.sanitize_label(raw.get("detail"), MAX_DETAIL_LENGTH),
        # An item with neither an action nor a URL is inert; render it disabled
        # rather than as a live row that silently does nothing when clicked.
        enabled=requested_enabled and bool(action_id or url),
        style=_coerce_style(raw.get("style")),
    )


def parse_section(raw: object, budget: int) -> tuple[MenuSection | None, int]:
    """Parse one section, consuming at most ``budget`` items.

    Returns the section and the remaining budget. The budget is threaded through
    rather than checked afterwards so a hostile payload cannot make Appistry
    build a huge structure and only then discard it.
    """
    if not isinstance(raw, dict) or budget <= 0:
        return None, budget

    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        return None, budget

    items: list[MenuItem] = []
    for raw_item in raw_items[:MAX_ITEMS_PER_SECTION]:
        if budget <= 0:
            break
        item = parse_item(raw_item)
        if item is None:
            continue
        # A separator with nothing before it, or two in a row, is noise the
        # raven should not be able to use to pad a menu into unusability.
        if item.separator and (not items or items[-1].separator):
            continue
        items.append(item)
        budget -= 1

    while items and items[-1].separator:
        items.pop()
        budget += 1

    if not items:
        return None, budget

    section_id = _parse_action_id(raw.get("id"))
    return (
        MenuSection(
            id=section_id,
            title=sanitize.sanitize_label(raw.get("title")),
            items=tuple(items),
        ),
        budget,
    )


def parse_menu(payload: object) -> MenuSpec:
    """Parse a whole menu payload into a bounded, sanitised :class:`MenuSpec`.

    Never raises for content reasons. A payload that is entirely unusable yields
    an empty spec, which the caller renders as a raven that is up but has nothing
    to say — distinct from a raven that could not be reached at all.
    """
    if not isinstance(payload, dict):
        log.debug("Raven menu payload was %s, not an object", type(payload).__name__)
        return MenuSpec()

    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        return MenuSpec(title=sanitize.sanitize_label(payload.get("title")))

    sections: list[MenuSection] = []
    budget = MAX_TOTAL_ITEMS
    for raw_section in raw_sections[:MAX_SECTIONS]:
        section, budget = parse_section(raw_section, budget)
        if section is not None:
            sections.append(section)

    return MenuSpec(
        title=sanitize.sanitize_label(payload.get("title")),
        badge=_coerce_badge(payload.get("badge")),
        sections=tuple(sections),
    )


# ── Rendered result ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RavenMenu:
    """What one raven contributes to the menu, available or not.

    ``reason`` is set exactly when the raven could not be rendered. The two
    fields are never both meaningful: a raven either contributed a spec or it
    contributed a reason, and the UI branches on ``reason`` alone.
    """

    name: str
    display: str
    spec: MenuSpec = field(default_factory=MenuSpec)
    reason: str = ""
    descriptor: "ravens.RavenDescriptor | None" = None

    @property
    def available(self) -> bool:
        return not self.reason

    def signature(self) -> tuple:
        """A hashable summary used to skip idle menu rebuilds.

        Includes everything the rendered menu shows. Omitting a field here means
        a change to it never reaches the screen, which is a subtler bug than
        rebuilding too often.
        """
        return (
            self.name,
            self.display,
            self.reason,
            self.spec.title,
            self.spec.badge,
            tuple(
                (
                    section.id,
                    section.title,
                    tuple(
                        (
                            item.label, item.action_id, item.url, item.detail,
                            item.enabled, item.style, item.separator,
                        )
                        for item in section.items
                    ),
                )
                for section in self.spec.sections
            ),
        )


def unavailable(raven: "ravens.UnavailableRaven") -> RavenMenu:
    """Build the disabled-with-a-reason menu for a raven that cannot be used."""
    return RavenMenu(
        name=raven.name,
        display=sanitize.sanitize_label(raven.display) or raven.name or "Unknown raven",
        reason=sanitize.sanitize_label(raven.reason) or "Unavailable.",
    )
