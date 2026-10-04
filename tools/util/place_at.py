"""Move a footprint to a point, optionally with a new rotation.

Usage: python3 tools/util/place_at.py BOARD.kicad_pcb U1 X Y [ROT] [--apply]

Prints the old and new position and the resulting pad positions. Zones are refilled before
saving: without a refill SaveBoard may propagate nets onto the wrong items. Without --apply
it is a dry run.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import sys, pcbnew
bp, ref, x, y = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
rot = float(sys.argv[5]) if len(sys.argv) > 5 and not sys.argv[5].startswith('--') else None
b = pcbnew.LoadBoard(bp); MM = 1e6
fp = [f for f in b.GetFootprints() if f.GetReference() == ref][0]
p = fp.GetPosition()
print('%s: (%.2f,%.2f) rot %.0f -> (%.2f,%.2f)%s' % (ref, p.x/MM, p.y/MM, fp.GetOrientationDegrees(), x, y,
      '' if rot is None else ' rot %.0f' % rot))
fp.SetPosition(pcbnew.VECTOR2I(int(x*MM), int(y*MM)))
if rot is not None: fp.SetOrientationDegrees(rot)
for pd in fp.Pads():
    q = pd.GetPosition(); print('  pad %-3s %-10s (%.2f,%.2f)' % (pd.GetNumber(), pd.GetNetname().split('/')[-1], q.x/MM, q.y/MM))
if '--apply' in sys.argv:
    pcbnew.ZONE_FILLER(b).Fill(b.Zones()); pcbnew.SaveBoard(bp, b); print('written', bp)
else:
    print('dry run')
