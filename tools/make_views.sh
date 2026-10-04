#!/usr/bin/env bash
# Board views for documents: 3D top and bottom + SVG of copper with silkscreen.
#
# Exists because document views tend to end up rendered from outdated copper while
# conclusions are drawn from them ("external connectors only on the edges", "part
# overhang"). Keeps the parameters in one place so views of different releases compare.
#
#   OUT=diagrams/views bash tools/make_views.sh [PREFIX]
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
OUT="${OUT:-$KAT_ROOT/diagrams/views}"
PRE="${1:-$BOARD_NAME}"
W=1920; H=1080
mkdir -p "$OUT"
echo "board:     $BOARD"
echo "directory: $OUT   prefix: $PRE   size: ${W}x${H}"

for side in top bottom; do
  case $side in top) suf=top;; bottom) suf=bot;; esac
  "$KICAD_CLI" pcb render --output "$OUT/$PRE-$suf.png" --side "$side" \
      --width $W --height $H --quality high --background opaque \
      --zoom 1.0 "$BOARD" >/dev/null
  echo "  3D $side -> $PRE-$suf.png"
done

# SVG: copper + silkscreen + outline, 1:1, each side separately
"$KICAD_CLI" pcb export svg --output "$OUT/$PRE-top.svg" \
    --layers 'F.Cu,F.Silkscreen,F.Mask,Edge.Cuts' --page-size-mode 2 --exclude-drawing-sheet \
    "$BOARD" >/dev/null
echo "  SVG top    -> $PRE-top.svg"
"$KICAD_CLI" pcb export svg --output "$OUT/$PRE-bot.svg" \
    --layers 'B.Cu,B.Silkscreen,B.Mask,Edge.Cuts' --page-size-mode 2 --exclude-drawing-sheet \
    --mirror "$BOARD" >/dev/null
echo "  SVG bottom -> $PRE-bot.svg"

# Fingerprint of the board the views were taken from: the gate uses it to tell "views are
# fresh" from "views predate the last copper edit". mtime does not work here: a zone refill
# touches the file without changing content, and views can be seconds older than an edit.
sha256sum "$BOARD" | awk '{print $1}' > "$OUT/.board.sha256"
echo "  board fingerprint -> .board.sha256"

python3 - "$OUT" "$PRE" <<'PY'
import os, sys
out, pre = sys.argv[1], sys.argv[2]
for f in sorted(os.listdir(out)):
    if f.startswith(pre):
        print('  %-22s %8.1f KB' % (f, os.path.getsize(os.path.join(out, f)) / 1024))
PY
