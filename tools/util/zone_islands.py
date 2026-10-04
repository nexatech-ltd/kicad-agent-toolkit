"""Zone fill polygons: how many islands each zone has and their areas.

Usage: python3 tools/util/zone_islands.py BOARD.kicad_pcb [LAYER] [NET_SUBSTR]

Prints per zone and layer the island count and the 12 largest areas (mm2). A plane split
into many islands usually means tracks on the plane layer are cutting the fill.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import sys, pcbnew
MM2 = 1e12
b = pcbnew.LoadBoard(sys.argv[1])
lay = sys.argv[2] if len(sys.argv) > 2 else None
netf = sys.argv[3] if len(sys.argv) > 3 else None
for z in b.Zones():
    for li in z.GetLayerSet().Seq():
        ln = b.GetLayerName(li)
        if lay and ln != lay: continue
        if netf and netf not in z.GetNetname(): continue
        sp = z.GetFilledPolysList(li)
        n = sp.OutlineCount()
        ar = sorted((sp.Outline(i).Area() / MM2 for i in range(n)), reverse=True)
        print('%-16s %-6s %-8s islands %3d  areas: %s'
              % (z.GetZoneName() or '(unnamed)', ln, z.GetNetname(), n,
                 ' '.join('%.2f' % a for a in ar[:12])))
