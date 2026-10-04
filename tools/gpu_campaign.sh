#!/usr/bin/env bash
# Campaign of the GPU router (tools/gpuroute) with per-net rollback - same as
# maze_campaign.sh with a different engine: PathFinder negotiation between the nets of a
# batch instead of A* one net at a time.
#
# Acceptance: fewer breaks, no more violations of significant severity. DRC is the judge.
#
# A round is not rejected as a whole. If errors appear, the offending nets are extracted
# from the DRC report, set aside, and the round is REPLAYED on the previous board without
# them. Blindly splitting the batch lost ~6 closed breaks because of one bad net out of 30;
# replaying keeps the other 29.
#
# A net set aside three times goes to hard_nets.txt and is not tried again - otherwise the
# campaign keeps hitting the same break forever.
set -u
# BRD (a board copy, e.g. from place_campaign.py) wins over an inherited BOARD, as before.
[ -n "${BRD:-}" ] && BOARD="$BRD"
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
cd "$KAT_ROOT"
# BOARD (or BRD) and KAT_WORK can point to a board copy to run the campaign on it.
W="$KAT_WORK"
PY="${PYTHON:-python3}"
TPY="${TORCH_PYTHON:-$PY}"   # interpreter with torch for the GPU router
# CUDA JIT cache: the 256 MB default gets evicted, and the first call in a new process
# can then take ~14 s on GPUs that need JIT compilation.
export CUDA_CACHE_MAXSIZE="${CUDA_CACHE_MAXSIZE:-4294967296}"
BATCH="${BATCH:-30}"
ROUNDS="${ROUNDS:-120}"
RETRY="${RETRY:-3}"
# Early exit: a round without gain costs the same ~70 s, and 25 empty rounds waste half an
# hour. After STALL consecutive rounds without gain the campaign ends so another engine
# can take over.
STALL="${STALL:-3}"
HARD="$W/hard_nets.txt"
SKIP="$W/round_skip.txt"
LOG="$W/gpu.log"
touch "$HARD"
say() { echo "$(date '+%H:%M:%S')  $*" | tee -a "$LOG"; }

drc() {
  "$KICAD_CLI" pcb drc --refill-zones --severity-all --format json -o "$1" "$BOARD" >/dev/null 2>&1 || true
  python3 -c "
import sys; sys.path.insert(0,'$KAT_TOOLS')
import drcinfo as D
u,e,w = D.score(D.load('$1')); print(u,e)"
}

# nets involved in the report's errors
offenders() {
  python3 -c "
import json, re, sys
d=json.load(open('$1'))
out=[]
for v in d.get('violations',[]):
    if v.get('severity')!='error': continue
    for i in v.get('items',[]):
        m=re.search(r'\[([^\]]+)\]', i.get('description',''))
        if m: out.append(m.group(1))
seen=[]
for n in out:
    if n not in seen: seen.append(n)
print('\n'.join(seen))"
}

say "=== campaign: batch $BATCH, rounds $ROUNDS, replays $RETRY, hard nets $(wc -l < "$HARD") ==="
NOGAIN=0
read -r U E < <(drc "$W/maze_cur.json")
# The campaign refuses to start on a board with violations: the acceptance rule is monotone
# (E2 <= E), so one error left over from an interrupted run becomes the new ceiling and
# rounds start passing with errors. This did happen.
if [ "$E" != "0" ]; then
  say "REFUSED: board already has $E violations of significant severity - clean up first"
  exit 1
fi
for i in $(seq 1 "$ROUNDS"); do
  say "--- round $i/$ROUNDS --- breaks $U, errors $E"
  [ "$U" = "0" ] && { say "no breaks left"; break; }
  : > "$SKIP"
  cat "$HARD" > "$SKIP"
  OK=0
  for try in $(seq 1 "$RETRY"); do
    cp "$BOARD" "$W/gpu.bak"
    "$TPY" "$KAT_TOOLS/gpuroute/route.py" --drc "$W/maze_cur.json" --limit "$BATCH" --iters "${ITERS:-8}" \
        ${NOPATH:+--nopath "$NOPATH"} \
        --exclude-net "$SKIP" \
        ${MAXLEN:+--max-len "$MAXLEN"} ${MINLEN:+--min-len "$MINLEN"} \
        ${LONGFIRST:+--longest-first} ${PLANES:+--planes} ${PLANES_ALL:+--planes-all} --apply 2>&1 | grep -v "assert\|property.h" \
        | tail -6 | while IFS= read -r l; do say "  | $l"; done
    read -r U2 E2 < <(drc "$W/maze_new.json")
    if [ "$E2" -le "$E" ] && [ "$U2" -lt "$U" ]; then
      say "ACCEPTED: $U -> $U2 ($((U2-U))), errors $E2 (attempt $try)"
      rm -f "$W/gpu.bak"; mv "$W/maze_new.json" "$W/maze_cur.json"
      U=$U2; E=$E2; OK=1; break
    fi
    mv "$W/gpu.bak" "$BOARD"
    BAD=$(offenders "$W/maze_new.json")
    NB=$(echo "$BAD" | grep -c . || true)
    if [ "$NB" = "0" ] || [ "$U2" -ge "$U" ] && [ "$E2" -le "$E" ]; then
      say "no gain ($U -> $U2) and no offenders - round is empty"
      break
    fi
    echo "$BAD" >> "$SKIP"
    # Net names may contain spaces and commas (hierarchical names such as
    # "/Sheet Name - IO, misc/NET"), so `for n in $BAD` split them into words and polluted
    # the hard list with fragments. Read line by line.
    printf '%s\n' "$BAD" >> "$W/offender_hits.txt"
    say "REJECTED ($U->$U2, errors $E->$E2): set aside $NB nets, replay $((try+1))"
  done
  if [ "$OK" = "0" ]; then
    # nets set aside three or more times go to the hard list
    if [ -f "$W/offender_hits.txt" ]; then
      sort "$W/offender_hits.txt" | uniq -c | awk '$1>=3 {$1="";sub(/^ /,"");print}' \
        | while IFS= read -r n; do
            grep -qxF "$n" "$HARD" || { echo "$n" >> "$HARD"; say "DEFERRED hard net: $n"; }
          done
    fi
    NH=$(wc -l < "$HARD")
    NOGAIN=$((NOGAIN+1))
    say "round $i without gain ($NOGAIN in a row), hard nets now $NH"
    [ "$NH" -gt 60 ] && { say "more than 60 hard nets - stop"; break; }
    [ "$NOGAIN" -ge "$STALL" ] && { say "$NOGAIN rounds in a row without gain - campaign finished"; break; }
  else
    NOGAIN=0
  fi
done
say "=== campaign finished: breaks $U, errors $E, hard nets $(wc -l < "$HARD") ==="
