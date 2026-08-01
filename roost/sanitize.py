"""Text sanitising for strings that arrive from a raven.

A raven descriptor and a raven's menu payload are **untrusted input**: they are
files and HTTP responses written by another process, and Roost renders them
into a desktop menu and into its own log file. Two attack shapes matter:

- **Terminal / log forgery.** An ANSI escape or a bare control character in a
  label rewrites whatever reads the log — including a developer's terminal — and
  can hide or fabricate lines around it.
- **Menu spoofing.** Unicode bidirectional overrides reorder a label after the
  fact, so a rendered menu item can read as something other than the bytes that
  produced it (``Quit`` that is really ``tiuQ``, a plausible-looking action id
  hidden behind a benign label).

So nothing from a raven reaches a menu or a log without passing through here.
The functions are deliberately allow-shaped: strip escapes, remove every
character with no business in a one-line label, collapse whitespace, and cap the
length.
"""

from __future__ import annotations

import re

# CSI/OSC and the short two-character escapes. Matched before the control-class
# strip below so the whole sequence disappears rather than leaving its printable
# tail ("[31m") behind as text.
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;:<=>?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC ... BEL or ST
    r"|[@-Z\\-_])"                       # two-character escapes
)

# C0 (minus the whitespace handled separately), DEL, and C1. C1 is included
# because a lone 0x9b is an alternate CSI introducer on some terminals.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Explicit bidi controls (LRE/RLE/PDF/LRO/RLO and the isolates) plus the
# invisible formatting characters most often used to disguise text.
_SPOOF_RE = re.compile(
    "["
    "​-‏"  # zero-width space/joiners, LRM/RLM
    "‪-‮"  # LRE, RLE, PDF, LRO, RLO
    "⁠-⁤"  # word joiner and invisible operators
    "⁦-⁩"  # LRI, RLI, FSI, PDI
    "﻿"         # BOM / zero-width no-break space
    "]"
)

_WHITESPACE_RE = re.compile(r"[\s   -     　]+")

DEFAULT_LABEL_LIMIT = 120
DEFAULT_LOG_LIMIT = 80

# Truncation marker. A single character so a limit is still a hard byte-ish cap.
_ELLIPSIS = "…"


def contains_unsafe_text(value: str) -> bool:
    """Return True if ``value`` holds an escape, control, or spoofing character.

    Used where the right answer is to *reject* rather than repair — a descriptor
    field, where a control character means the file is malformed and the raven
    should be reported unavailable with a reason instead of silently cleaned up.
    """
    if not isinstance(value, str):
        return True
    return bool(
        "\x1b" in value
        or _CONTROL_RE.search(value)
        or _SPOOF_RE.search(value)
    )


def sanitize_label(value: object, limit: int = DEFAULT_LABEL_LIMIT) -> str:
    """Return ``value`` reduced to a single safe line, or ``""`` if nothing survives.

    Non-strings collapse to ``""`` rather than being coerced: a menu label that
    arrived as a dict or a list is a protocol error, and rendering ``repr()`` of
    it would put attacker-chosen punctuation on screen.
    """
    if not isinstance(value, str):
        return ""
    cleaned = _ANSI_RE.sub("", value)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _SPOOF_RE.sub("", cleaned)
    # Any remaining escape byte was not part of a recognised sequence.
    cleaned = cleaned.replace("\x1b", "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[: max(limit - 1, 0)].rstrip() + _ELLIPSIS
    return cleaned


def safe_for_log(value: object, limit: int = DEFAULT_LOG_LIMIT) -> str:
    """Return a form of ``value`` that is safe to write into a log line.

    Same cleaning as :func:`sanitize_label` but shorter, and an empty result is
    rendered as a visible placeholder so a log line never silently loses its
    subject.
    """
    cleaned = sanitize_label(value, limit)
    return cleaned or "<empty>"
