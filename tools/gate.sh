#!/usr/bin/env bash
# Full acceptance gate in one command.
#
# There are too many checks to keep in one's head: some look at the board, some at the
# schematic, some need numpy (a separate venv), and verify.py must run FROM the project
# directory, otherwise one of its checks silently degrades to selecting pins by name.
#
#   bash tools/gate.sh            # run, verdict in the exit code
#   bash tools/gate.sh --quick    # without DRC and ERC (the slowest ones)
#
# Environment: VENV (python with numpy + pcbnew, default python3), OUT (log directory,
# default $KAT_WORK/gate), VIEWS (rendered views directory for check_views_fresh.py;
# empty = no views maintained).
# No `set -u`: env.sh tests KICAD_CLI for emptiness via [ -z "$KICAD_CLI" ], and an unset
# variable would abort the run on the first line.
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
cd "$KAT_ROOT"
VENV="${VENV:-python3}"
OUT="${OUT:-$KAT_WORK/gate}"
NET="$KAT_WORK/$BOARD_NAME.net"
SCH="${SCH:-${BOARD%.kicad_pcb}.kicad_sch}"
VIEWS="${VIEWS:-}"
mkdir -p "$OUT"
QUICK=0; [ "${1:-}" = "--quick" ] && QUICK=1
FAIL=0
# Status is printed FIRST at a fixed ASCII width: aligning on the name breaks as soon as a
# name contains multi-byte characters (printf counts bytes).
line() { printf '[%-9s] %s\n' "$2" "$1"; }

run() {   # name, command...
  local name="$1"; shift
  local log="$OUT/$(echo "$name" | tr ' /:' '___').log"
  if "$@" > "$log" 2>&1; then
    line "$name" "passed"
  else
    line "$name" "ISSUES"; echo "              log: ${log#$KAT_ROOT/}"
    FAIL=$((FAIL+1))
  fi
}

echo "=== acceptance gate $BOARD_NAME: $(date '+%Y-%m-%d %H:%M') ==="
echo "board: $BOARD"
# The previous log may predate the last schematic edit, and then its numbers no longer
# describe this board. Print the state BEFORE the run so it is visible what is stale; this
# does not affect the gate's exit code.
python3 "$KAT_TOOLS/gate_stamp.py" --check --out "$OUT" || true

# Zone fills must match the copper. Finishing scripts (fix_plane_pads.py, stitch_gnd.py,
# widen_net.py) write copper as text and do not refill zones; then the "nothing lies inside
# a foreign fill" check reports false positives on tracks inside a stale GND fill, while DRC
# with --refill-zones shows zero violations (no real overlap). Refill if the board is free.
BUSY=0
[ -e "$(dirname "$BOARD")/~$(basename "$BOARD").lck" ] && BUSY=1
pgrep -f campaign.py >/dev/null 2>&1 && BUSY=1
if [ "$BUSY" = "0" ]; then
  "$KICAD_CLI" pcb drc --refill-zones --save-board --severity-error \
    --format json -o "$OUT/refill.json" "$BOARD" > "$OUT/refill.log" 2>&1 || true
  echo "zone fills refreshed"
else
  echo "WARNING: board is busy (editor or campaign): zone fills not refreshed,"
  echo "         the 'inside a foreign fill' check may give false positives"
fi
echo

run "verify.py gate"                   python3 "$KAT_TOOLS/verify.py" "$BOARD"
run "netlist: open drains and pull-ups" python3 "$KAT_TOOLS/check_netlist.py" "$NET"
run "RF: reference under the feed"     "$VENV" "$KAT_TOOLS/check_rf_reference.py" "$BOARD"
run "cross-sections and IR drop"       "$VENV" "$KAT_TOOLS/check_ir_drop.py" "$BOARD"
run "power-up sequence"                python3 "$KAT_TOOLS/check_power_sequence.py" "$NET"
run "uniformity of support parts"      python3 "$KAT_TOOLS/check_uniformity.py" "$NET"
run "widths vs net classes"            python3 "$KAT_TOOLS/audit_widths.py" --board "$BOARD"
run "tracks over foreign pads"         python3 "$KAT_TOOLS/check_track_on_pad.py"
# Check against the component registry: five axes plus coverage. No heavy PDF parsing here,
# only YAML, the netlist and the board, so it takes seconds. --allow-incomplete is needed
# while the registry is being filled: without it an unfilled component fails the run. It
# does NOT forgive defects on any axis.
run "parts vs registry"                python3 "$KAT_TOOLS/check_components.py" --net "$NET" \
    --board "$BOARD" --allow-incomplete
run "stock and order codes"            python3 "$KAT_TOOLS/check_stock.py" "$NET"
if [ -n "$VIEWS" ]; then
  run "views are fresh"                python3 "$KAT_TOOLS/check_views_fresh.py" \
      --board "$BOARD" --views "$VIEWS"
else
  line "views are fresh (VIEWS not set)" "skipped"   # a check that did not run must not read as passed
fi
run "pads without a net"               python3 "$KAT_TOOLS/check_pads_no_net.py" \
    --board "$BOARD" --net "$NET"

if [ "$QUICK" = "0" ]; then
  "$KICAD_CLI" sch export netlist --format kicadsexpr -o "$NET" \
    "$SCH" > "$OUT/netlist.log" 2>&1 || true
  "$KICAD_CLI" pcb drc --schematic-parity --refill-zones --severity-all \
    --format json -o "$OUT/drc.json" "$BOARD" > "$OUT/drc.log" 2>&1 || true
  "$KICAD_CLI" sch erc --severity-all --format json -o "$OUT/erc.json" \
    "$SCH" > "$OUT/erc.log" 2>&1 || true
  python3 - "$OUT" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ['KAT_TOOLS'])
import drcinfo as D
out = sys.argv[1]
d = D.load(os.path.join(out, 'drc.json'))
u, e, w = D.score(d)
par = len(d.get('schematic_parity', []))
print('[%-9s] board DRC: unconnected %d | errors %d | warnings %d | parity %d'
      % ('errors 0' if e == 0 else 'ERRORS', u, e, w, par))
for (sev, typ), n in D.types(d).most_common():
    print('      %5d  %-8s %s' % (n, sev, typ))
p = os.path.join(out, 'erc.json')
if os.path.exists(p):
    ec = json.load(open(p))
    # ERC violations live INSIDE the sheets: the kicad-cli report has no top-level
    # violations array, and ec.get('violations', []) alone always returned an empty list,
    # so the gate printed "errors 0" regardless of the schematic. Collect per sheet; keep
    # the top level in case of a flat schematic.
    vs = list(ec.get('violations', []))
    for sh in ec.get('sheets', []):
        vs += sh.get('violations', [])
    ee = sum(1 for v in vs if v.get('severity') == 'error')
    print('[%-9s] schematic ERC: errors %d | warnings %d'
          % ('errors 0' if ee == 0 else 'ERRORS', ee, len(vs) - ee))
open(os.path.join(out, 'verdict.txt'), 'w').write('%d %d %d\n' % (u, e, par))
PY
  read -r U E PAR < "$OUT/verdict.txt"
  echo
  [ "$E" -eq 0 ] || { line "violations of significant severity" "FOUND: $E"; FAIL=$((FAIL+1)); }
  [ "$U" -eq 0 ] || line "unconnected" "$U, routing not finished"
fi

# The input fingerprint is written AFTER all runs and only on a full run: it certifies which
# state of schematic and board the numbers above belong to. In --quick DRC and ERC did not
# run, and a fingerprint would certify old drc.json/erc.json as fresh (exactly the error it
# exists to prevent), so in quick mode the previous fingerprint is REMOVED.
if [ "$QUICK" = "0" ]; then
  python3 "$KAT_TOOLS/gate_stamp.py" --write --out "$OUT" --board "$BOARD" || true
else
  rm -f "$OUT/inputs.sha"
  echo "quick run: input fingerprint removed (DRC and ERC did not run)"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: all checks passed"
else
  echo "RESULT: checks with issues: $FAIL (logs in $OUT)"
fi
exit "$FAIL"
