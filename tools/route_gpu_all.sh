#!/usr/bin/env bash
# Alternate the GPU router and regional rip-up until no breaks remain.
#
#   cycle: plane-net pads (fix_plane_pads) -> gpu_campaign.sh (until plateau)
#          -> rip_campaign.py (locked ends) -> part nudging -> gpu_campaign.sh again ...
#
# Every step is judged by DRC and accepted only with fewer breaks and zero violations of
# significant severity. A cycle without gain ends the run.
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
cd "$KAT_ROOT"
W="$KAT_WORK"
LOG="$W/route_gpu_all.log"
PY="${PYTHON:-python3}"
TPY="${TORCH_PYTHON:-$PY}"      # interpreter with torch for tools/gpuroute
CYCLES="${CYCLES:-10}"
BATCH="${BATCH:-60}"
RIP_ROUNDS="${RIP_ROUNDS:-12}"
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
  U_CYCLE=$U
  say "cycle $cy/$CYCLES: vias for plane-net pads"
  cp "$BOARD" "$W/before_pp.kicad_pcb"
  "$PY" "$KAT_TOOLS/fix_plane_pads.py" "$W/cur_drc.json" --apply >> "$W/plane_pads.log" 2>&1 || true
  read -r U0 E0 < <(score)
  if [ "$E0" != "0" ]; then
    "$PY" "$KAT_TOOLS/prune_new.py" --since "$W/before_pp.kicad_pcb" \
        --drc "$W/cur_drc.json" --apply >> "$W/plane_pads.log" 2>&1 || true
    read -r U0 E0 < <(score)
  fi
  if [ "$E0" != "0" ] || [ "$U0" -gt "$U" ]; then
    cp "$W/before_pp.kicad_pcb" "$BOARD"; read -r U0 E0 < <(score)
    say "cycle $cy: plane-net pads rolled back ($U0, errors $E0)"
  else
    say "cycle $cy: plane-net pads $U -> $U0"
  fi
  U=$U0

  say "cycle $cy/$CYCLES: GPU router, batches of $BATCH"
  rm -f "$W/gpu_nopath.txt"
  NOPATH="$W/gpu_nopath.txt" BATCH=$BATCH ROUNDS=40 RETRY=2 ITERS=8 STALL=3 \
      bash "$KAT_TOOLS/gpu_campaign.sh" >> "$W/gpu_campaign.out" 2>&1
  read -r U1 E1 < <(score)
  say "cycle $cy: GPU router $U -> $U1, errors $E1"
  if [ "$E1" != "0" ]; then say "STOP: $E1 violations appeared"; break; fi
  U=$U1
  [ "$U" = "0" ] && { say "NO BREAKS LEFT"; break; }

  say "cycle $cy/$CYCLES: regional rip-up of locked ends"
  "$TPY" "$KAT_TOOLS/gpuroute/rip_campaign.py" --rounds "$RIP_ROUNDS" >> "$W/rip_campaign.out" 2>&1
  read -r U2 E2 < <(score)
  say "cycle $cy: rip-up $U -> $U2, errors $E2"
  if [ "$E2" != "0" ]; then say "STOP: $E2 violations appeared"; break; fi
  U=$U2
  [ "$U" = "0" ] && { say "NO BREAKS LEFT"; break; }

  # Nudging the locking parts: the break count MAY grow here (stubs removed at moved pads),
  # so this step is judged only by violations, and the gain by the whole cycle: if after the
  # next router pass there are not fewer breaks than at the start of the cycle, the cycle is
  # rolled back to the snapshot.
  cp "$BOARD" "$W/cycle_${cy}_pre_nudge.kicad_pcb"
  say "cycle $cy/$CYCLES: nudging locking parts"
  "$TPY" "$KAT_TOOLS/gpuroute/nudge_campaign.py" --max-refs "${NUDGE_REFS:-8}" >> "$W/nudge_campaign.out" 2>&1
  read -r U3 E3 < <(score)
  say "cycle $cy: nudges $U -> $U3, errors $E3"
  if [ "$E3" != "0" ]; then
    cp "$W/cycle_${cy}_pre_nudge.kicad_pcb" "$BOARD"; read -r U3 E3 < <(score)
    say "cycle $cy: nudges rolled back ($U3, errors $E3)"
  fi
  if [ "$U3" != "$U" ]; then
    say "cycle $cy/$CYCLES: GPU router after nudges"
    rm -f "$W/gpu_nopath.txt"
    NOPATH="$W/gpu_nopath.txt" BATCH=$BATCH ROUNDS=40 RETRY=2 ITERS=8 STALL=3 \
        bash "$KAT_TOOLS/gpu_campaign.sh" >> "$W/gpu_campaign.out" 2>&1
    read -r U4 E4 < <(score)
    say "cycle $cy: GPU router after nudges $U3 -> $U4, errors $E4"
    if [ "$E4" != "0" ] || [ "$U4" -ge "$U" ]; then
      cp "$W/cycle_${cy}_pre_nudge.kicad_pcb" "$BOARD"; read -r U4 E4 < <(score)
      say "cycle $cy: nudges did not pay off - rolled back to $U4"
    fi
    U=$U4
  fi
  [ "$U" = "0" ] && { say "NO BREAKS LEFT"; break; }
  if [ "$U" -ge "$U_CYCLE" ]; then
    say "cycle $cy without gain ($U_CYCLE -> $U) - techniques exhausted"
    break
  fi
done
say "result: breaks $U"
