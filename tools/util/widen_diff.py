"""Which track segments changed width between two boards.

Usage: python3 tools/util/widen_diff.py BEFORE.kicad_pcb AFTER.kicad_pcb [NET ...]

A segment is keyed by (layer, start, end, net); old and new widths are printed. Use it to
verify that a widening pass really wrote what it reported.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, pcbnew
MM = 1e6
short = lambda s: s.split('/')[-1]

def load(p, want):
    b = pcbnew.LoadBoard(p)
    out = {}
    for t in b.GetTracks():
        if t.GetClass() == 'PCB_VIA': continue
        n = short(t.GetNetname())
        if want and n not in want: continue
        s, e = t.GetStart(), t.GetEnd()
        k = (b.GetLayerName(t.GetLayer()), s.x, s.y, e.x, e.y, n)
        out[k] = t.GetWidth() / MM
    return out

want = set(sys.argv[3:])
A, B = load(sys.argv[1], want), load(sys.argv[2], want)
ch = [(k, A[k], B[k]) for k in A if k in B and abs(A[k] - B[k]) > 1e-6]
print('width changed: %d segments' % len(ch))
for (lay, x0, y0, x1, y1, n), a, b in sorted(ch, key=lambda r: (r[0][5], r[0][0])):
    print('  %-10s %-9s %.3f -> %.3f  (%.3f,%.3f)->(%.3f,%.3f)'
          % (n, lay, a, b, x0/MM, y0/MM, x1/MM, y1/MM))
only = [k for k in B if k not in A]
if only: print('new segments: %d' % len(only))
