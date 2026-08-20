"""The tray icon: the raven, resolved per platform.

There is no icon *selection*. There was, and it earned its removal: a submenu, a
CLI verb, a config file and a validation surface, all so the mark in the tray
could be a different mark. The mark is Roost's identity — a tray item that looks
like whatever the user last picked is harder to find, not easier.

Two files back the icon, because the platforms consume a tray icon
incompatibly:

- macOS ``template=True`` reads **only the alpha channel** and discards RGB, so
  the shell can tint the icon for light mode, dark mode, and menu highlight. A
  template icon must therefore be a monochrome silhouette; handing it colour
  achieves nothing because the colour is thrown away.
- pystray on Windows has **no template concept**. It renders the RGBA bitmap
  literally, so the monochrome-on-transparent file that is correct on macOS is
  invisible against a taskbar of the same shade. Windows gets the full-colour
  variant.

Icons are rasterized ahead of time by ``tools/build-icons.sh`` and checked in.
They are not generated at install time, because ``sips`` is macOS-only and Pillow
cannot read SVG — there is no cross-platform way to rasterize on demand.

**The artwork is a licence obligation, not decoration.** *Raven* by Lorc,
game-icons.net, CC BY 3.0 — see ``assets/CREDITS.md``. Whatever else changes
here, the attribution ships with the artwork.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_IS_MACOS = sys.platform == "darwin"

HERE = Path(__file__).resolve().parent
ASSETS_DIR = HERE / "assets"

#: The one icon. Named rather than inlined because it is also the asset
#: filename stem, and both variants derive from it.
DEFAULT_ICON = "raven"


@dataclass(frozen=True)
class IconChoice:
    """A resolved tray icon: which file to load and how to render it."""

    name: str
    path: Path
    template: bool


def _variant_paths(name: str) -> tuple[Path, Path]:
    """Return ``(template, colour)`` asset paths for a built-in icon name."""
    return (
        ASSETS_DIR / f"{name}-template.png",
        ASSETS_DIR / f"{name}.png",
    )


def resolve(value: str | None = None) -> IconChoice | None:
    """The tray icon, or None if the asset is missing.

    ``value`` is accepted and ignored so the three call sites — both trays and
    the Windows shortcut builder — did not all have to change when selection
    was removed. None rather than a raise: a missing asset costs the tray its
    icon, and every caller already degrades (a title on macOS, a skipped
    shortcut icon on Windows). Refusing to start over a PNG would be worse.
    """
    template_path, colour_path = _variant_paths(DEFAULT_ICON)
    # macOS wants the alpha-only silhouette it can tint; Windows renders the
    # bitmap literally and needs the colour one.
    path = template_path if _IS_MACOS else colour_path
    if not path.exists():
        log.warning("Tray icon asset is missing: %s", path)
        return None
    return IconChoice(name=DEFAULT_ICON, path=path, template=_IS_MACOS)
