"""Track copper area of a net (sum of length * width) and a histogram of track widths.

Usage: python3 tools/util/net_area.py BOARD.kicad_pcb NET [NET ...]

An independent re-measurement for current-capacity and width audits; vias are ignored.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, math, collections, pcbnew
MM = 1e6
b = pcbnew.LoadBoard(sys.argv[1])
want = set(sys.argv[2:])
short = lambda s: s.split('/')[-1]
for n in sys.argv[2:]:
    area = length = 0.0
    hist = collections.Counter()
    for t in b.GetTracks():
        if t.GetClass() == 'PCB_VIA' or short(t.GetNetname()) != n: continue
        s, e = t.GetStart(), t.GetEnd()
        L = math.hypot(e.x - s.x, e.y - s.y) / MM
        w = t.GetWidth() / MM
        area += L * w; length += L; hist[round(w, 3)] += 1
    print(f'{n}: copper {length:.1f} mm, area {area:.2f} mm2, segments {sum(hist.values())}')
    print('   ', ' '.join(f'{w}:{c}' for w, c in sorted(hist.items())))
