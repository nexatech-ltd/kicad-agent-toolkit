"""Walk a net's copper as a path from pad to pad and show where it leaves the straight line.

Usage: python3 tools/util/path_trace.py BOARD.kicad_pcb NET

Prints the path nodes (including layer changes through vias), the distance travelled, and
the position along and perpendicular to the straight line between the first and last pad.
Only nodes where the sideways offset changes by more than 1.5 mm are shown.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, math, pcbnew
from collections import defaultdict
MM = 1e6
b = pcbnew.LoadBoard(sys.argv[1]); net = sys.argv[2]
short = lambda s: s.split('/')[-1]
segs, vias, pads = [], [], []
for fp in b.GetFootprints():
    for pd in fp.Pads():
        if short(pd.GetNetname()) == net:
            p = pd.GetPosition(); pads.append((fp.GetReference()+'.'+pd.GetNumber(), round(p.x/MM,3), round(p.y/MM,3)))
for t in b.GetTracks():
    if short(t.GetNetname()) != net: continue
    if t.GetClass() == 'PCB_VIA':
        p = t.GetPosition(); vias.append((round(p.x/MM,3), round(p.y/MM,3)))
    else:
        s, e = t.GetStart(), t.GetEnd()
        segs.append((b.GetLayerName(t.GetLayer()), (round(s.x/MM,3), round(s.y/MM,3)), (round(e.x/MM,3), round(e.y/MM,3))))
VIA = {v for v in vias}
adj = defaultdict(list)
for i, (lay, a, c) in enumerate(segs):
    adj[(lay, a)].append((i, (lay, c))); adj[(lay, c)].append((i, (lay, a)))
for v in VIA:                                        # a via joins layers at one point
    ks = [k for k in adj if abs(k[1][0]-v[0]) < 0.03 and abs(k[1][1]-v[1]) < 0.03]
    for i in range(len(ks)):
        for j in range(i+1, len(ks)):
            adj[ks[i]].append((-1, ks[j])); adj[ks[j]].append((-1, ks[i]))
start = None
for r, x, y in pads:
    for k in adj:
        if abs(k[1][0]-x) < 0.35 and abs(k[1][1]-y) < 0.35: start = (k, r); break
    if start: break
if not start: sys.exit('no path start found at any pad')
node, ref = start
seen, path, tot = set(), [(node, 0.0)], 0.0
while True:
    nxt = None
    for i, k in adj[node]:
        if i in seen and i >= 0: continue
        nxt = (i, k); break
    if not nxt: break
    i, k = nxt
    if i >= 0:
        seen.add(i); tot += math.hypot(k[1][0]-node[1][0], k[1][1]-node[1][1])
    node = k; path.append((node, tot))
A = (pads[0][1], pads[0][2]); B = (pads[-1][1], pads[-1][2])
L = math.hypot(B[0]-A[0], B[1]-A[1]); ux, uy = (B[0]-A[0])/L, (B[1]-A[1])/L
print('%s: from %s (%.2f,%.2f) to %s (%.2f,%.2f), straight %.1f mm, copper along path %.1f mm' %
      (net, pads[0][0], A[0], A[1], pads[-1][0], B[0], B[1], L, tot))
print('  path nodes (progress along the straight line and perpendicular offset from it):')
prev = None
for (lay, p), d in path:
    dx, dy = p[0]-A[0], p[1]-A[1]
    along = dx*ux + dy*uy; off = dx*(-uy) + dy*ux
    if prev is None or abs(off-prev) > 1.5 or (lay, p) == path[-1][0]:
        print('    %-6s (%7.2f,%6.2f)  along %6.1f  side %+7.2f  travelled %6.1f' % (lay, p[0], p[1], along, off, d))
        prev = off
