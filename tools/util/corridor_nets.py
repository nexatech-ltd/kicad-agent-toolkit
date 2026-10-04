"""Which nets occupy the straight corridor between two points.

Usage: python3 tools/util/corridor_nets.py BOARD.kicad_pcb x0,y0 x1,y1 [--r 0.6] [--skip GND,+3V3]

Prints every net whose copper (any layer) falls into the band of width 2R along the segment,
with its length inside the band, segment and via counts and layers. Measure the corridor this
way before setting a length target for a route that has to pass through it.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, math, pcbnew
from collections import defaultdict
MM = 1e6
bp = sys.argv[1]
ax, ay = (float(v) for v in sys.argv[2].split(','))
bx, by = (float(v) for v in sys.argv[3].split(','))
R = float(sys.argv[sys.argv.index('--r')+1]) if '--r' in sys.argv else 0.6
skip = set((sys.argv[sys.argv.index('--skip')+1]).split(',')) if '--skip' in sys.argv else set()
b = pcbnew.LoadBoard(bp)
L = math.hypot(bx-ax, by-ay); ux, uy = (bx-ax)/L, (by-ay)/L
def dist(px, py):
    t = (px-ax)*ux + (py-ay)*uy
    t = max(0.0, min(L, t))
    return math.hypot(px-(ax+ux*t), py-(ay+uy*t))
per = defaultdict(lambda: [0.0, 0, 0, set()])     # length, segments, vias, layers
for t in b.GetTracks():
    n = t.GetNetname().split('/')[-1]
    if n in skip: continue
    if t.GetClass() == 'PCB_VIA':
        p = t.GetPosition()
        if dist(p.x/MM, p.y/MM) <= R: per[n][2] += 1
        continue
    s, e = t.GetStart(), t.GetEnd()
    x0, y0, x1, y1 = s.x/MM, s.y/MM, e.x/MM, e.y/MM
    m = 12; hit = 0.0
    seg = math.hypot(x1-x0, y1-y0)
    for i in range(m+1):
        px, py = x0+(x1-x0)*i/m, y0+(y1-y0)*i/m
        if dist(px, py) <= R: hit += seg/m
    if hit > 0:
        per[n][0] += hit; per[n][1] += 1; per[n][3].add(b.GetLayerName(t.GetLayer()))
print('corridor (%.1f,%.1f)-(%.1f,%.1f), half-width %.2f mm, length %.1f mm' % (ax,ay,bx,by,R,L))
tot = 0
for n in sorted(per, key=lambda k: -per[k][0]):
    d, ns, nv, lays = per[n]
    tot += 1
    print('  %-24s %6.1f mm, segments %3d, vias %2d, layers %s' % (n, d, ns, nv, ','.join(sorted(lays))))
print('nets in corridor: %d' % tot)
