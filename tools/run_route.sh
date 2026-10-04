#!/usr/bin/env bash
# One Freerouting round: export DSN, run the engine, import SES into a trial copy, accept or reject.
# Usage: tools/run_route.sh   (env: FR_JAR, JAVA, FR_API, ONLY_NETS_FILE, ROUTE_SECONDS)
set -e
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/env.sh"
# Default mode is STEPWISE (ONLY_NETS_FILE): only a batch of nets is routed, the rest of the
# copper is passed as protected. A synthetic test (tools/test_protect.py) showed both engines
# do route around a wall of protected copper; shorts seen on import came from half-defined
# keepout zones, not from the stepwise mode itself.
#
# Keep the batch SMALL. Measured: 20-45 nets give 0.3-1.0 closed breaks/min, 120 nets give
# 0.00-0.06. The cause is per-net acceptance: a large batch's solution diverges from the board
# in many places, import collisions pile up and almost every net is rolled back.
PY="${PYTHON:-python3}"
"$PY" "$KAT_TOOLS/dsn_export.py" | tail -2
# The router reads freerouting.json from the CURRENT directory, so run from the work dir.
cd "$KAT_WORK"
FR_JAR="${FR_JAR:-fr.jar}"
JAVA="${JAVA:-java}"
FR_API="${FR_API:-http://127.0.0.1:37864/v1}"; export FR_API
API="$FR_API/system/status"
echo "engine: $FR_JAR | java: $JAVA"

# Reuse a live server: startup costs ~25 s of waiting plus 3 s to kill the old one, i.e.
# almost half a minute per round. State lives in the job, not in the process.
if [ "${FR_RESTART:-0}" != "1" ] && curl -fsS -m 3 "$API" >/dev/null 2>&1; then
  echo "router server already running - reusing it"
else
  # Kill the old router ONLY by java process name. `pkill -f` matches the full command line,
  # so any wrapper whose command line contains the pattern gets killed together with the
  # round - the script would kill itself and exit with code 144 and no output.
  for _p in $(pgrep -f -- "-jar fr" 2>/dev/null); do
    [ "$(ps -o comm= -p "$_p" 2>/dev/null)" = java ] && kill "$_p" 2>/dev/null || true
  done
  sleep 3
  # ENDPOINTS CANNOT be set via the environment: the field is a string array, an env var
  # yields a string, and Freerouting fails with "Can not set [Ljava.lang.String; ...",
  # silently starting the server on the default localhost:37864. Freerouting 2.4.x requires
  # authentication by default and the env var does not disable it - only the CLI flag does.
  nohup env FREEROUTING__GUI__ENABLED=false FREEROUTING__API_SERVER__ENABLED=true \
    FREEROUTING__API_SERVER__HTTP_ALLOWED=true \
    "$JAVA" -Djava.awt.headless=true -jar "$FR_JAR" \
      --api_server.enabled=true --api_server.http_allowed=true \
      --api_server.authentication.enabled=false --gui.enabled=false \
      > api.log 2>&1 &
  n=0
  until curl -fsS -m 2 "$API" >/dev/null 2>&1 || [ $n -ge 40 ]; do sleep 2; n=$((n+1)); done
  echo "server came up in $((n*2)) s"
fi

# FR_SECONDS=1 means: enqueue the job and exit immediately. The API's /cancel answers 501 and
# makes the result unreachable, so the job is left running and the SES is fetched from the
# running job via output/stream. Do NOT silence the output: it once hid an auth rejection.
FR_SECONDS=1 "$PY" "$KAT_TOOLS/fr_api.py" "$BOARD_NAME.dsn" 2>&1 | tail -3
echo "job started: $(tail -1 "$KAT_WORK/fr_job.txt")"
sleep ${ROUTE_SECONDS:-900}
echo "passes: $(grep -c 'pass #' api.log) | last: $(grep 'pass #' api.log | tail -1 | sed 's/.*score of/score/;s/using.*//')"
timeout 280 "$PY" "$KAT_TOOLS/fr_grab.py" "$BOARD_NAME.ses"
bash "$KAT_TOOLS/after_ses.sh"
