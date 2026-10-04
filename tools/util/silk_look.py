"""Silkscreen objects of a footprint: texts (height/width/thickness, visibility) and lines.

Usage: python3 tools/util/silk_look.py BOARD.kicad_pcb U1 [R1 ...]
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, pcbnew
MM = 1e6
b = pcbnew.LoadBoard(sys.argv[1])
want = set(sys.argv[2:])
for fp in b.GetFootprints():
    if fp.GetReference() not in want: continue
    p = fp.GetPosition()
    print('=== %s @ (%.2f,%.2f) rot %.0f  layer %s' % (fp.GetReference(), p.x/MM, p.y/MM,
          fp.GetOrientationDegrees(), b.GetLayerName(fp.GetLayer())))
    for t in [fp.Reference(), fp.Value()] + [x for x in fp.GraphicalItems() if x.GetClass() == 'PCB_TEXT']:
        tp = t.GetPosition()
        print('   text %-22r layer %-14s h=%.3f w=%.3f thickness=%.3f @ (%.2f,%.2f) %s'
              % (t.GetText(), b.GetLayerName(t.GetLayer()), t.GetTextHeight()/MM,
                 t.GetTextWidth()/MM, t.GetTextThickness()/MM, tp.x/MM, tp.y/MM,
                 'visible' if t.IsVisible() else 'HIDDEN'))
    for g in fp.GraphicalItems():
        if g.GetClass() == 'PCB_TEXT': continue
        ln = b.GetLayerName(g.GetLayer())
        if 'Silkscreen' not in ln: continue
        s, e = g.GetStart(), g.GetEnd()
        print('   %-14s %-14s (%.3f,%.3f)-(%.3f,%.3f) thickness=%.3f'
              % (g.GetClass(), ln, s.x/MM, s.y/MM, e.x/MM, e.y/MM, g.GetWidth()/MM))
