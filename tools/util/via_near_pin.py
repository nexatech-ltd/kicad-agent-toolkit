"""Distance from a pad to the nearest via of its own net.

Usage: python3 tools/util/via_near_pin.py BOARD.kicad_pcb U1.4 [C1.2 ...]

Mirrors the local-decoupling criterion of verify.py (VIA_NEAR = 2.0 mm): a pad is OK when a
via of its net (e.g. a plane via) lies within 2.0 mm.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, math, pcbnew
MM = 1e6
b = pcbnew.LoadBoard(sys.argv[1])
short = lambda s: s.split('/')[-1]
vias = {}
for t in b.GetTracks():
    if t.GetClass() != 'PCB_VIA': continue
    p = t.GetPosition(); vias.setdefault(short(t.GetNetname()), []).append((p.x/MM, p.y/MM))
for spec in sys.argv[2:]:
    ref, num = spec.split('.')
    for fp in b.GetFootprints():
        if fp.GetReference() != ref: continue
        for pd in fp.Pads():
            if pd.GetNumber() != num: continue
            p = pd.GetPosition(); x, y = p.x/MM, p.y/MM; n = short(pd.GetNetname())
            vs = vias.get(n, [])
            d = min((math.hypot(vx-x, vy-y) for vx, vy in vs), default=99.0)
            print('%-8s net %-8s (%.3f,%.3f): nearest via %.2f mm - %s'
                  % (spec, n, x, y, d, 'OK' if d <= 2.0 else 'FAR (>2.0)'))
