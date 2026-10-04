#!/bin/bash
# Usage: tools/util/killtree.sh PID...
# Kill a process and all of its descendants (children first). Safer than pkill -f, whose
# pattern also matches the shell running the pkill command itself.
kt(){ for c in $(ps -o pid= --ppid "$1"); do kt "$c"; done; kill "$1" 2>/dev/null && echo "  kill $1 $(ps -o comm= -p $1 2>/dev/null)"; }
for p in "$@"; do kt "$p"; done
