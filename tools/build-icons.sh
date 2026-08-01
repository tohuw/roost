#!/usr/bin/env bash
#
# Rasterize assets/*.svg into the PNG variants the tray needs.
#
# Two variants per icon, because the two platforms consume a tray icon
# differently and a single file cannot satisfy both:
#
#   *-template.png / *-template@2x.png
#       Black on transparent. macOS `template=True` uses only the alpha channel
#       and discards RGB, so the shell tints these to match the menu bar in light
#       mode, dark mode, and while the menu is highlighted.
#
#   *.png / *@2x.png
#       Full colour. pystray on Windows has no template concept — it renders the
#       RGBA bitmap literally — so a white-on-transparent icon would be invisible
#       on a light taskbar and a black one invisible on a dark one. The source
#       art's own colour is legible on both.
#
# Run this only when the SVG source changes; the PNGs are checked in so neither
# an install nor a Windows machine needs an SVG rasterizer. `sips` is macOS-only
# and Pillow cannot read SVG, so there is no cross-platform way to do this at
# install time.
#
# Usage: tools/build-icons.sh

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "build-icons.sh needs macOS: it uses sips, which is the only SVG" >&2
    echo "rasterizer this project depends on. The generated PNGs are checked" >&2
    echo "in precisely so this step never runs on Windows." >&2
    exit 1
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The assets ship inside the package.
assets="$here/roost/assets"

# A status item is ~18pt tall; @2x is the Retina rendition. Both sizes are
# generated from the vector source rather than by scaling the 1x bitmap, which
# would visibly soften the @2x edges.
declare -a sizes=("18:" "36:@2x")

shopt -s nullglob
for svg in "$assets"/*.svg; do
    name="$(basename "$svg" .svg)"

    # The monochrome source: the same paths with every fill forced to black, so
    # the alpha channel macOS keeps carries the full silhouette. Recolouring the
    # vector is exact; post-processing the bitmap's channels would not be.
    mono="$(mktemp -t "roost-${name}-mono").svg"
    trap 'rm -f "$mono"' EXIT
    sed -E 's/(fill|stroke)="#[0-9A-Fa-f]{3,8}"/\1="#000000"/g' "$svg" > "$mono"

    for spec in "${sizes[@]}"; do
        size="${spec%%:*}"
        suffix="${spec##*:}"
        sips -s format png -z "$size" "$size" "$svg" \
            --out "$assets/${name}${suffix}.png" >/dev/null
        sips -s format png -z "$size" "$size" "$mono" \
            --out "$assets/${name}-template${suffix}.png" >/dev/null
        echo "  ${name}${suffix}.png  ${name}-template${suffix}.png  (${size}px)"
    done

    rm -f "$mono"
    trap - EXIT
done

echo "Done. Commit the regenerated PNGs alongside the SVG change."
