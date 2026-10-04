#!/usr/bin/env bash
# Release of the fabrication kit.
#
# Exists because a kit assembled from ad-hoc calls once shipped DNP parts to assembly: the
# "do not place" mark lived only in the value text, and the export had no --exclude-dnp.
# The script makes the release reproducible and keeps the flags in one place.
#
#   bash tools/make_fab.sh                 # into $FAB_ROOT/<today>
#   OUT=fab/2026-01-01-v2 bash tools/make_fab.sh
#
# Environment: BOARD/BRD (board; may be a snapshot so a running campaign is not disturbed),
# SCH (root schematic), PRO (project file with the rules), FAB_ROOT, OUT,
# LAYERS (Gerber layer list; default: all copper layers of the board + mask/paste/silk/outline).
set -e
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
FAB_ROOT="${FAB_ROOT:-$KAT_ROOT/fab}"
OUT="${OUT:-$FAB_ROOT/$(date +%Y-%m-%d)}"
mkdir -p "$OUT/gerbers"
BRDF="$BOARD"
_paths() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import paths; print(getattr(paths, sys.argv[2]))' "$KAT_TOOLS" "$1"; }
SCHF="${SCH:-$(_paths SCH)}"
PROF="${PRO:-$(_paths PRO)}"
# A snapshot board has no project file next to it: fall back to the project's own.
if [ ! -f "$PROF" ]; then
  set -- "$KAT_ROOT"/*.kicad_pro
  if [ "$#" -eq 1 ] && [ -f "$1" ]; then PROF="$1"; fi
fi
PNAME="$(basename "${PROF%.kicad_pro}")"
[ -f "$SCHF" ] || SCHF="${PROF%.kicad_pro}.kicad_sch"
echo "board: $BRDF"
echo "kit:   $OUT"

# --- copper, mask, paste, silkscreen, outline ---
# Copper layers are read from the board's own layer table (canonical names), so the list
# fits any stack-up; override with LAYERS.
if [ -z "${LAYERS:-}" ]; then
  CU="$(python3 - "$BRDF" <<'PY'
import re, sys
text = open(sys.argv[1], encoding='utf-8').read()
m = re.search(r'\(layers\b(.*?)\n\s*\)', text, re.S)
print(','.join(re.findall(r'\(\d+\s+"([^"]+\.Cu)"', m.group(1) if m else '')))
PY
)"
  LAYERS="$CU,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,Edge.Cuts,F.Mask,B.Mask"
fi
"$KICAD_CLI" pcb export gerbers --output "$OUT/gerbers/" --layers "$LAYERS" \
  --no-protel-ext --subtract-soldermask --use-drill-file-origin "$BRDF" | tail -1

# --- drilling: two files (PTH and NPTH) with maps, origin at the drill origin ---
"$KICAD_CLI" pcb export drill --output "$OUT/gerbers/" --format excellon \
  --drill-origin absolute --excellon-separate-th --generate-map --map-format gerberx2 \
  --excellon-units mm "$BRDF" | tail -1

# --- placement: ONLY parts to be assembled ---
# --exclude-dnp is mandatory: without it DNP parts go to assembly
"$KICAD_CLI" pcb export pos --output "$OUT/positions.csv" --format csv --units mm \
  --side both --exclude-dnp "$BRDF" | tail -1

# --- BOM: also without DNP, with LCSC and MPN codes ---
"$KICAD_CLI" sch export bom --output "$OUT/bom.csv" --exclude-dnp \
  --fields 'Reference,Value,Footprint,LCSC,MPN,${QUANTITY}' \
  --labels 'Designator,Value,Footprint,LCSC,MPN,Qty' \
  --group-by 'Value,Footprint' "$SCHF" | tail -1

# --- BOM with ALL parts, DNP included, as a separate file for cross-checking ---
"$KICAD_CLI" sch export bom --output "$OUT/bom-with-dnp.csv" \
  --fields 'Reference,Value,Footprint,LCSC,MPN,${QUANTITY},${DNP}' \
  --labels 'Designator,Value,Footprint,LCSC,MPN,Qty,DNP' \
  --group-by 'Value,Footprint' "$SCHF" | tail -1

"$KICAD_CLI" sch export pdf --output "$OUT/$BOARD_NAME-schematic.pdf" "$SCHF" | tail -1

# --- assembly drawing ---
# The kit needs one: in dense areas part of the silkscreen text is inevitably clipped by
# mask openings (Gerbers use --subtract-soldermask), and designator legibility then relies
# on F.Fab, which is not in LAYERS above. Without the drawing the assembler only gets
# positions.csv and the BOM, so "readable on the assembly drawing" is a promise without a file.
# --crossout-DNP: unplaced parts are crossed out on the drawing, a second line of defence
# after --exclude-dnp.
# --sketch-pads: pad outlines with numbers, used to check package orientation.
# --scale 0 (auto scale) is mandatory: without it a large board takes a third of an A4 sheet
# and designators of small packages are unreadable in print.
# Top side only; for bottom-side assembly add a second call with
# --layers B.Fab,Edge.Cuts --mirror.
"$KICAD_CLI" pcb export pdf --output "$OUT/$BOARD_NAME-assembly-top.pdf" \
  --layers F.Fab,Edge.Cuts --mode-single --include-border-title --black-and-white --scale 0 \
  --sketch-pads-on-fab-layers --crossout-DNP-footprints-on-fab-layers "$BRDF" | tail -1

# --- manifest: the numbers the kit is accepted by ---
# DRC must run with the PROJECT rules. On a board copy without its .kicad_pro and .kicad_dru
# next to it, KiCad uses default rules and reports hundreds of violations instead of zero;
# in a release manifest such a number is worse than none. Put board and rules in one dir.
DRCDIR="$OUT/.drc"
mkdir -p "$DRCDIR"
# The board is copied under the PROJECT name so that KiCad pairs it with the project's
# .kicad_pro/.kicad_dru/.kicad_sch (the board itself may be a snapshot with another name).
cp "$BRDF" "$DRCDIR/$PNAME.kicad_pcb"
cp "$PROF" "${PROF%.kicad_pro}.kicad_dru" "$DRCDIR/" 2>/dev/null || true
# The schematic is needed for --schematic-parity: without it that report section is empty,
# and "0 mismatches" would only mean the check did not run.
cp "$(dirname "$PROF")"/*.kicad_sch "$DRCDIR/" 2>/dev/null || true
cp -r "$KAT_ROOT/lib" "$DRCDIR/" 2>/dev/null || true
# Library tables are required along with the libraries: without fp-lib-table KiCad has no
# ENTRY for the project library name, however many .pretty dirs lie next to it. Without
# them the release drc.json gets phantom lib_footprint_issues ("library not in the
# configuration") that hide real lib_footprint_mismatch items.
cp "$KAT_ROOT/fp-lib-table" "$KAT_ROOT/sym-lib-table" "$DRCDIR/" 2>/dev/null || true
[ -f "$DRCDIR/$PNAME.kicad_pro" ] || { echo "REFUSED: no $PNAME.kicad_pro, DRC would use default rules"; exit 2; }
[ -f "$DRCDIR/fp-lib-table" ] || { echo "REFUSED: no fp-lib-table, DRC would not see the project libraries"; exit 2; }
python3 - "$OUT" "$DRCDIR/$PNAME.kicad_pcb" "$BOARD_NAME" <<'PY'
import csv, json, os, subprocess, sys
sys.path.insert(0, os.path.join(os.environ['KAT_TOOLS']))
import drcinfo as D
out, brd, name = sys.argv[1], sys.argv[2], sys.argv[3]
drc = D.run_drc(brd, os.path.join(out, 'drc.json'), parity=True)
# "Configuration does not include the library" means the DRC copy is incomplete and part
# of the footprint checks did not run: the manifest numbers would be computed blind.
blind = [v for v in drc.get('violations', [])
         if 'does not include the footprint library' in v.get('description', '')]
if blind:
    raise SystemExit('REFUSED: DRC did not see %d librar(y/ies), the .drc copy is incomplete:\n  %s'
                     % (len(blind), blind[0]['description']))
u, e, w = D.score(drc)
par = len(drc.get('schematic_parity', []))
def rows(p):
    with open(p) as f:
        return sum(1 for _ in csv.reader(f)) - 1
dnp = 0
with open(os.path.join(out, 'bom-with-dnp.csv')) as f:
    for r in csv.DictReader(f):
        if (r.get('DNP') or '').strip().lower() in ('yes', 'true', '1', 'dnp'):
            dnp += 1
man = {
    'board': os.path.basename(brd),
    'unconnected': u, 'violations of significant severity': e, 'warnings': w,
    'parity mismatches': par,
    'BOM rows to assemble': rows(os.path.join(out, 'bom.csv')),
    'BOM rows total': rows(os.path.join(out, 'bom-with-dnp.csv')),
    'of them DNP': dnp,
    'positions in placement file': rows(os.path.join(out, 'positions.csv')),
    'Gerber files': len([n for n in os.listdir(os.path.join(out, 'gerbers'))]),
    'assembly drawing': 'yes' if os.path.exists(os.path.join(out, name + '-assembly-top.pdf')) else 'NO',
}
with open(os.path.join(out, 'manifest.json'), 'w') as f:
    json.dump(man, f, ensure_ascii=False, indent=1)
for k, v in man.items():
    print('  %-36s %s' % (k, v))
if e:
    print('\nREFUSED: %d violations of significant severity, the kit must not be released' % e)
    sys.exit(2)
if u:
    print('\nWARNING: %d unconnected, routing not finished' % u)
PY
rm -rf "$DRCDIR"
echo "done: $OUT"
