#!/usr/bin/env bash
# Transplant copper from another board ONE net at a time, with a DRC check after each.
#
# Transplanting many nets at once (17 in one measured case) produced more breaks and many
# violations, and pruning what DRC rejected wiped out the whole gain. The cause is the same
# as with per-net merging: the source solution is consistent only with itself, so pieces of
# two solutions collide with each other, not just with the existing copper.
#
# Hence nets are moved one by one, and a net is kept only if the break count drops and no
# violations of significant severity appear.
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
cd "$KAT_ROOT"
W="$KAT_WORK"
SRC="${SRC:?set SRC=path to the source board}"
LIST="${LIST:-$W/transplant_nets.txt}"
PY="${PYTHON:-python3}"
LOG="$W/transplant.log"
say() { echo "$(date '+%H:%M:%S')  $*" | tee -a "$LOG"; }

score() {
  "$KICAD_CLI" pcb drc --refill-zones --severity-all --format json -o "$W/t_drc.json" \
      "$BOARD" >/dev/null 2>&1 || true
  python3 -c "
import sys; sys.path.insert(0,'$KAT_TOOLS')
import drcinfo as D
u,e,w = D.score(D.load('$W/t_drc.json')); print(u,e)"
}

read -r U E < <(score)
say "=== per-net transplant: start $U breaks, $E violations ==="
[ "$E" != "0" ] && { say "REFUSED: board already has $E violations"; exit 1; }
OK=0; NO=0
while IFS= read -r net; do
  [ -z "$net" ] && continue
  printf '%s\n' "$net" > "$W/one_net.txt"
  cp "$BOARD" "$W/t_before.kicad_pcb"
  "$PY" "$KAT_TOOLS/transplant_net.py" --from-board "$SRC" \
      --nets-file "$W/one_net.txt" --apply >/dev/null 2>&1 || true
  read -r U2 E2 < <(score)
  if [ "$E2" = "0" ] && [ "$U2" -lt "$U" ]; then
    say "ACCEPTED  $U -> $U2" ; say "   net: $net"
    U=$U2; OK=$((OK+1))
  else
    # try pruning what DRC rejected, then score again
    if [ "$E2" != "0" ]; then
      "$PY" "$KAT_TOOLS/prune_new.py" --since "$W/t_before.kicad_pcb" \
          --drc "$W/t_drc.json" --apply >/dev/null 2>&1 || true
      read -r U2 E2 < <(score)
    fi
    if [ "$E2" = "0" ] && [ "$U2" -lt "$U" ]; then
      say "ACCEPTED after pruning: $U -> $U2 | $net"
      U=$U2; OK=$((OK+1))
    else
      cp "$W/t_before.kicad_pcb" "$BOARD"
      say "rejected ($U2 breaks, $E2 violations): $(echo "$net" | sed 's|.*/||')"
      NO=$((NO+1))
    fi
  fi
done < "$LIST"
say "=== result: accepted $OK, rejected $NO, breaks $U ==="
