"""Single source of paths for every tool in this toolkit.

Nothing is hard-coded: all values are derived from the project directory and can be
overridden with environment variables, so each tool stays runnable by hand.

    KAT_ROOT   project directory that holds the .kicad_pro / .kicad_pcb
               (default: the current working directory)
    KAT_WORK   scratch directory for DSN, SES, DRC reports, logs, trial copies
               (default: $KAT_ROOT/work). It lives inside the project on purpose:
               intermediate files such as the Specctra net map are required to
               import a session back, and a system temp dir loses them.
    BRD        the .kicad_pcb to work on; takes precedence over BOARD (used to point
               a child process at a trial copy)
    BOARD      the .kicad_pcb. Default: "board" from kicad-agent.json,
               otherwise the only *.kicad_pcb in KAT_ROOT.
    SCH        root schematic (default: board name with .kicad_sch)
    KICAD_CLI  kicad-cli binary (default: PATH, then the macOS app bundle)
"""
import glob
import os
import shutil

KAT_TOOLS = os.path.dirname(os.path.abspath(__file__))
KAT_ROOT = os.path.abspath(os.environ.get('KAT_ROOT') or os.getcwd())
KAT_WORK = os.environ.get('KAT_WORK') or os.path.join(KAT_ROOT, 'work')
CONFIG = os.environ.get('KAT_CONFIG') or os.path.join(KAT_ROOT, 'kicad-agent.json')


def _board():
    # BRD wins over BOARD: tools hand a trial copy to a child process via BRD while
    # BOARD (exported by env.sh) is inherited; the copy must be the one edited.
    v = os.environ.get('BRD') or os.environ.get('BOARD')
    if v:
        return os.path.abspath(v)
    if os.path.exists(CONFIG):
        import json
        with open(CONFIG, encoding='utf-8') as f:
            b = json.load(f).get('board')
        if b:
            return os.path.join(KAT_ROOT, b)
    found = sorted(glob.glob(os.path.join(KAT_ROOT, '*.kicad_pcb')))
    if len(found) == 1:
        return found[0]
    # Several or none: return a predictable name; tools that need the file report it.
    return found[0] if found else os.path.join(KAT_ROOT, 'board.kicad_pcb')


BOARD = _board()
STEM = os.path.splitext(BOARD)[0]
NAME = os.path.basename(STEM)
SCH = os.environ.get('SCH') or STEM + '.kicad_sch'
PRO = STEM + '.kicad_pro'
DRU = STEM + '.kicad_dru'
NETLIST = os.environ.get('NETLIST') or os.path.join(KAT_WORK, NAME + '.net')

# Backwards-compatible alias used by some tools.
PIPELINE = KAT_TOOLS


def sibling(board, ext):
    """Project file next to a given board: sibling('x/B.kicad_pcb', '.kicad_pro')."""
    return os.path.splitext(os.path.abspath(board))[0] + ext


def _kicad_cli():
    v = os.environ.get('KICAD_CLI')
    if v:
        return v
    v = shutil.which('kicad-cli')
    if v:
        return v
    mac = '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli'
    return mac if os.path.exists(mac) else 'kicad-cli'


KICAD_CLI = _kicad_cli()


def work(*parts):
    """Path inside the work directory (created on first use)."""
    os.makedirs(KAT_WORK, exist_ok=True)
    return os.path.join(KAT_WORK, *parts)
