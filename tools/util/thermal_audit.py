"""Thermal-pad audit: how many vias sit inside the large pads of a footprint.

Usage: python3 tools/util/thermal_audit.py BOARD.kicad_pcb U1 [U2 ...]

For every pad of at least 1 mm2 prints its size, net and the number of vias inside it
(by the pad's HitTest), plus the total per footprint.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, pcbnew
MM = 1e6
b = pcbnew.LoadBoard(sys.argv[1]); refs = sys.argv[2:]
vias = [t for t in b.GetTracks() if t.GetClass() == 'PCB_VIA']
short = lambda s: s.split('/')[-1]
for fp in b.GetFootprints():
    if fp.GetReference() not in refs: continue
    print('=== %s %s  (%.2f,%.2f) rot %.0f' % (fp.GetReference(), fp.GetValue(),
          fp.GetPosition().x/MM, fp.GetPosition().y/MM, fp.GetOrientationDegrees()))
    tot = 0
    for pd in sorted(fp.Pads(), key=lambda p: -p.GetSizeX()*p.GetSizeY()):
        sx, sy = pd.GetSizeX()/MM, pd.GetSizeY()/MM
        area = sx*sy
        if area < 1.0: continue          # only large pads matter for heat sinking
        p = pd.GetPosition()
        n = 0
        for v in vias:
            if pd.HitTest(v.GetPosition()): n += 1
        tot += n
        print('  pad %-4s %-14s %5.2f x %5.2f = %6.2f mm2  (%7.2f,%6.2f)  vias inside: %d'
              % (pd.GetNumber(), short(pd.GetNetname()), sx, sy, area, p.x/MM, p.y/MM, n))
    print('  total vias in large pads: %d' % tot)
