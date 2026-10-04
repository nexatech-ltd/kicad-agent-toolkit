#!/usr/bin/env bash
# Alternate two routing engines until no breaks remain.
#
# Division of labour, established by measurement:
#   built-in maze router - short breaks (up to ~12 mm): targeted, fast, no server;
#     stalls on long connections because it cannot rip up;
#   Freerouting in stepwise mode - long connections: it can rip-up-and-retry, but a round
#     costs ~280 s and vias must NOT be hidden from it (otherwise many shorts on import,
#     see campaign.py --keep-stitch).
#
# Both are judged by one rule: fewer breaks, no more violations of significant severity.
# A board with violations is refused up front - otherwise they would become the ceiling.
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
cd "$KAT_ROOT"
W="$KAT_WORK"
LOG="$W/route_all.log"
CYCLES="${CYCLES:-20}"
JAR="${JAR:-$W/fr-2.4.1.jar}"
JAVA="${JAVA:-java}"           # Freerouting 2.4.x needs a recent JDK (25 for 2.4.1)
PY="${PYTHON:-python3}"
say() { echo "$(date '+%H:%M:%S')  == $*" | tee -a "$LOG"; }

score() {
  "$KICAD_CLI" pcb drc --refill-zones --severity-all --format json \
      -o "$W/cur_drc.json" "$BOARD" >/dev/null 2>&1 || true
  python3 -c "
import sys; sys.path.insert(0,'$KAT_TOOLS')
import drcinfo as D
u,e,w = D.score(D.load('$W/cur_drc.json')); print(u,e)"
}

read -r U E < <(score)
say "start: breaks $U, errors $E"
[ "$E" != "0" ] && { say "REFUSED: board has $E violations - clean up first"; exit 1; }

for cy in $(seq 1 "$CYCLES"); do
  # Pads of plane nets are not given to the router (the plane is poured, the pin list is
  # empty), so a pad without a via into its plane stays unconnected forever - nobody will
  # route it. In one measured case a single plane net had 17 such breaks totalling 53 mm,
  # i.e. nearly a tenth of the remainder was closed by one tool rather than by routing.
  say "cycle $cy/$CYCLES: vias for plane-net pads"
  cp "$BOARD" "$W/before_pp.kicad_pcb"
  "$PY" "$KAT_TOOLS/fix_plane_pads.py" "$W/cur_drc.json" --apply >> "$W/plane_pads.log" 2>&1 || true
  read -r U0 E0 < <(score)
  # The rule mask cannot be reproduced exactly, so DRC has the last word: objects from this
  # run that end up in violations are removed. Measured: 8 via-to-via shorts between two
  # plane nets went away by removing 4 objects out of 28.
  if [ "$E0" != "0" ]; then
    "$PY" "$KAT_TOOLS/prune_new.py" --since "$W/before_pp.kicad_pcb" \
        --drc "$W/cur_drc.json" --apply >> "$W/plane_pads.log" 2>&1 || true
    read -r U0 E0 < <(score)
    say "cycle $cy: after removing what DRC rejected - breaks $U0, errors $E0"
  fi
  say "cycle $cy: plane-net pads $U -> $U0, errors $E0"
  if [ "$E0" != "0" ]; then say "STOP: $E0 violations appeared"; break; fi
  U=$U0

  # Two waves of the built-in router: short breaks first (fast, many), then without a length
  # limit. With a 25 mm search window it handles 100 mm connections too, but such a net
  # costs ~20 s, so the cheap ones go first.
  say "cycle $cy/$CYCLES: maze router, short breaks"
  cp "$W/cur_drc.json" "$W/maze_cur.json"
  BATCH=40 ROUNDS=25 RETRY=2 MAXLEN=12 bash "$KAT_TOOLS/maze_campaign.sh" >> "$W/maze.log" 2>&1
  read -r U1 E1 < <(score)
  say "cycle $cy: maze router (short) $U -> $U1, errors $E1"
  if [ "$E1" = "0" ]; then
    say "cycle $cy/$CYCLES: maze router, all lengths"
    cp "$W/cur_drc.json" "$W/maze_cur.json"
    BATCH=25 ROUNDS=25 RETRY=2 MINLEN=12 bash "$KAT_TOOLS/maze_campaign.sh" >> "$W/maze.log" 2>&1
    read -r U1 E1 < <(score)
    say "cycle $cy: maze router (medium) -> $U1, errors $E1"
  fi
  if [ "$E1" = "0" ]; then
    # Third wave: longest first. Without the reverse order the batch is filled in ascending
    # length, medium connections consume it, and the 70-110 mm ones are never reached.
    say "cycle $cy/$CYCLES: maze router, longest first"
    cp "$W/cur_drc.json" "$W/maze_cur.json"
    BATCH=12 ROUNDS=25 RETRY=2 MINLEN=25 LONGFIRST=1 \
        bash "$KAT_TOOLS/maze_campaign.sh" >> "$W/maze.log" 2>&1
    read -r U1 E1 < <(score)
    say "cycle $cy: maze router (long) -> $U1, errors $E1"
  fi
  if [ "$E1" != "0" ]; then say "STOP: $E1 violations appeared"; break; fi

  say "cycle $cy/$CYCLES: Freerouting, long connections"
  rm -f "$W/campaign.json"
  "$PY" "$KAT_TOOLS/campaign.py" --rounds 6 --batch 30 --seconds 240 --keep-stitch \
      --jar "$JAR" --java "$JAVA" >> "$W/fr-loop.log" 2>&1
  read -r U2 E2 < <(score)
  say "cycle $cy: Freerouting $U1 -> $U2, errors $E2"
  if [ "$E2" != "0" ]; then say "STOP: $E2 violations appeared"; break; fi
  if [ "$U2" = "0" ]; then say "NO BREAKS LEFT"; U=0; break; fi
  if [ "$U2" -ge "$U" ]; then
    say "cycle $cy gave no gain ($U -> $U2) - both engines exhausted"
    U=$U2; break
  fi
  U=$U2
done
say "result: breaks $U"
