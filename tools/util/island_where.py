"""Where the fill islands of zones on a layer are: area, bounding box, centre.

Usage: python3 tools/util/island_where.py BOARD.kicad_pcb LAYER [ZONE_SUBSTR]

Lists every filled outline (largest first) of the zones on LAYER, optionally only zones whose
name contains ZONE_SUBSTR. Use it to locate cut-off islands reported by DRC.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import sys, pcbnew
MM, MM2 = 1e6, 1e12
b = pcbnew.LoadBoard(sys.argv[1]); lay = sys.argv[2]
sub = sys.argv[3] if len(sys.argv) > 3 else ''
for z in b.Zones():
    for li in z.GetLayerSet().Seq():
        if b.GetLayerName(li) != lay: continue
        if sub and sub not in (z.GetZoneName() or ''): continue
        sp = z.GetFilledPolysList(li)
        rows = []
        for i in range(sp.OutlineCount()):
            o = sp.Outline(i)
            xs = [o.CPoint(j).x / MM for j in range(o.PointCount())]
            ys = [o.CPoint(j).y / MM for j in range(o.PointCount())]
            rows.append((o.Area() / MM2, min(xs), min(ys), max(xs), max(ys)))
        rows.sort(reverse=True)
        print('%s %s %s: islands %d' % (z.GetZoneName(), lay, z.GetNetname(), len(rows)))
        for a, x0, y0, x1, y1 in rows:
            print('   %9.2f mm2  bbox (%.2f,%.2f)-(%.2f,%.2f)  centre (%.2f,%.2f)'
                  % (a, x0, y0, x1, y1, (x0 + x1) / 2, (y0 + y1) / 2))
