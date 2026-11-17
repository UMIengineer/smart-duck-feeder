#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_static.sh — fetch all third-party assets for the duck feeder dashboard
# Run this ONCE on any machine that has internet access (your MacBook is fine).
# It produces a `static/` folder containing Chart.js + woff2 fonts, ready to
# sit alongside app_v2.py on the Pi for fully offline operation.
#
# Usage:   bash setup_static.sh
# Output:  ./static/js/chart.min.js
#          ./static/fonts/*.woff2
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

STATIC="static"
mkdir -p "$STATIC/js" "$STATIC/fonts"

# ── Chart.js (UMD build, jsDelivr) ──────────────────────────────────────────
echo "→ Chart.js"
curl -fsSL -o "$STATIC/js/chart.min.js" \
  "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"

# ── Fonts via google-webfonts-helper (zip per family, latin subset, woff2) ──
fetch_font() {
  local family="$1"
  local variants="$2"
  local tmp
  tmp="$(mktemp -d)"
  echo "→ $family ($variants)"
  curl -fsSL -o "$tmp/$family.zip" \
    "https://gwfh.mranftl.com/api/fonts/$family?download=zip&subsets=latin&variants=$variants&formats=woff2"
  unzip -o -q "$tmp/$family.zip" -d "$tmp"
  # gwfh names files like "<family>-v<N>-latin-<weight>.woff2".
  # Strip the version segment so the CSS can reference predictable paths.
  for f in "$tmp"/*.woff2; do
    [ -e "$f" ] || continue
    local cleaned
    cleaned=$(basename "$f" | sed -E 's/-v[0-9]+-/-/')
    mv "$f" "$STATIC/fonts/$cleaned"
  done
  rm -rf "$tmp"
}

fetch_font "instrument-serif" "regular,italic"
fetch_font "geist"            "regular,500"
fetch_font "jetbrains-mono"   "regular"

echo
echo "Done. Contents of $STATIC/:"
find "$STATIC" -type f | sort
