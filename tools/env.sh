# Single source of paths for the shell scripts. Source it as:
#   . "$(dirname "${BASH_SOURCE[0]}")/env.sh"
# Mirrors tools/paths.py; every value can be overridden from the environment.
KAT_TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
KAT_ROOT="${KAT_ROOT:-$(pwd)}"
KAT_WORK="${KAT_WORK:-$KAT_ROOT/work}"
# BRD (a trial copy handed down by a caller) wins over an inherited BOARD.
# ${..:-} is required: scripts running with `set -u` would fail on an unbound variable.
if [ -n "${BRD:-}" ]; then
  BOARD="$BRD"
elif [ -z "${BOARD:-}" ]; then
  BOARD="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import paths; print(paths.BOARD)' "$KAT_TOOLS")"
fi
if [ -z "${KICAD_CLI:-}" ]; then
  KICAD_CLI="$(command -v kicad-cli 2>/dev/null)"
  [ -z "$KICAD_CLI" ] && [ -x /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli ] \
    && KICAD_CLI=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
  [ -z "$KICAD_CLI" ] && KICAD_CLI=kicad-cli
fi
BOARD_NAME="$(basename "${BOARD%.kicad_pcb}")"
export KAT_TOOLS KAT_ROOT KAT_WORK BOARD BOARD_NAME KICAD_CLI
mkdir -p "$KAT_WORK"
