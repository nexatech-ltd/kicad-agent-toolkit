#!/usr/bin/env bash
# Import a Freerouting session (SES) into a trial copy of the board and judge it with DRC.
# Usage: tools/after_ses.sh   (env: BOARD/BRD, KAT_WORK, ONLY_NETS_FILE)
set -e
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
SRC="$(dirname "$BOARD")"
STEM="${BOARD%.kicad_pcb}"
T="$KAT_WORK/trial"
rm -rf "$T" && mkdir -p "$T"
cp "$BOARD" "$STEM.kicad_pro" "$T/"
cp "$STEM.kicad_dru" "$T/" 2>/dev/null || true
cp "$SRC"/*.kicad_sch "$T/" 2>/dev/null || true
cp -r "$SRC/lib" "$T/" 2>/dev/null || true
# ONLY_NETS_FILE, if set, must match the value used when the DSN was exported.
BRD="$T/$BOARD_NAME.kicad_pcb" BOARD="$T/$BOARD_NAME.kicad_pcb" ${PYTHON:-python3} "$KAT_TOOLS/ses_import.py"
# --severity-all, not --severity-error: a severity filter once hid 20 hole_to_hole items,
# 19 of them below the fab minimum. Read the whole report; let KiCad do the classification.
"$KICAD_CLI" pcb drc --refill-zones --save-board \
  --severity-all --format json -o "$T/drc.json" "$T/$BOARD_NAME.kicad_pcb" 2>&1 | tail -1
${PYTHON:-python3} "$KAT_TOOLS/accept.py" "$T/drc.json"
