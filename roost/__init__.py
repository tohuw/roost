"""Roost — one shared status menu bar for the ravens.

Everything lives inside this package rather than as top-level modules, and that
is a coexistence requirement rather than a matter of taste. This project was
forked from a separate, unrelated internal tool called Appistry, which is
still in daily use and installs top-level ``appistry``, ``menubar``,
``windows_support``, and ``windows_tray`` modules of its own. Two distributions
shipping same-named top-level modules silently overwrite each other in any
environment that holds both, so this one claims exactly one name — ``roost`` —
and nothing else.

See ``COEXISTENCE`` in README.md for the full list of what this project owns and
what it never touches. ``tests/unit/test_coexistence.py`` pins the guarantee.

Importing this package is deliberately free of side effects: the tray toolkits
(``rumps`` on macOS, ``pystray`` on Windows) are only imported by the modules
that render with them.
"""

from __future__ import annotations

#: The user-visible product name, used in menus, notifications, and log lines.
NAME = "Roost"

#: The lowercase identity used for filesystem paths, the console script, thread
#: names, and the ``/api/status`` service field.
SLUG = "roost"
