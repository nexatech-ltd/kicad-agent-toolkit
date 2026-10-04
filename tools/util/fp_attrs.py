"""Set or clear footprint fabrication attributes.

Usage: python3 tools/util/fp_attrs.py BOARD.kicad_pcb U1[,R1,...] [--bom yes|no] [--pos yes|no] [--apply]

yes = included in the BOM / position files, no = excluded. Fixes the schematic-parity
warning "Exclude from BOM settings differ". Without --apply nothing is saved.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, pcbnew
bp, refs = sys.argv[1], set(sys.argv[2].split(',')); apply = '--apply' in sys.argv
def opt(k): return sys.argv[sys.argv.index(k) + 1] if k in sys.argv else None
bom, pos = opt('--bom'), opt('--pos')
b = pcbnew.LoadBoard(bp); n = 0
for f in b.GetFootprints():
    if f.GetReference() not in refs: continue
    a = f.GetAttributes()
    if bom == 'yes': a &= ~pcbnew.FP_EXCLUDE_FROM_BOM
    if bom == 'no': a |= pcbnew.FP_EXCLUDE_FROM_BOM
    if pos == 'yes': a &= ~pcbnew.FP_EXCLUDE_FROM_POS_FILES
    if pos == 'no': a |= pcbnew.FP_EXCLUDE_FROM_POS_FILES
    f.SetAttributes(a); n += 1; print('  %s: excl_bom=%s excl_pos=%s' % (f.GetReference(), bool(a & pcbnew.FP_EXCLUDE_FROM_BOM), bool(a & pcbnew.FP_EXCLUDE_FROM_POS_FILES)))
if apply: pcbnew.SaveBoard(bp, b); print('saved', bp, 'changed', n)
