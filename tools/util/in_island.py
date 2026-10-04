"""Which zone-fill island contains a point, and how large that island is.

Usage: python3 tools/util/in_island.py BOARD.kicad_pcb NET LAYER x,y [x,y ...]

A via into a cut-off island of a plane connects nothing, so a stitching or plane via must land
in the main (largest) fill outline. Each point is reported as MAIN, a small island, or no fill.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, pcbnew
MM, MM2 = 1e6, 1e12
b = pcbnew.LoadBoard(sys.argv[1]); net, lay = sys.argv[2], sys.argv[3]
li = b.GetLayerID(lay)
polys = []
for z in b.Zones():
    if z.GetNetname().split('/')[-1] != net or not z.IsOnLayer(li): continue
    sp = z.GetFilledPolysList(li)
    for i in range(sp.OutlineCount()):
        o = sp.Outline(i)
        polys.append((z.GetZoneName(), i, o.Area() / MM2, o))
best = max((p[2] for p in polys), default=0)
print('zone %s/%s: islands %d, main %.1f mm2' % (net, lay, len(polys), best))
for arg in sys.argv[4:]:
    x, y = (float(v) for v in arg.split(','))
    P = pcbnew.VECTOR2I(int(x * MM), int(y * MM))
    hit = [(zn, i, a) for zn, i, a, o in polys if o.PointInside(P)]
    if not hit:
        print('  (%.3f,%.3f): NO FILL' % (x, y)); continue
    zn, i, a = hit[0]
    print('  (%.3f,%.3f): island #%d of zone %s, area %.1f mm2 - %s'
          % (x, y, i, zn, a, 'MAIN' if abs(a - best) < 1e-6 else 'ISLAND, not usable'))
