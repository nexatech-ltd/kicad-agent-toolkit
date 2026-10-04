"""Remove copper that sits in DRC violations (by the report's item coordinates) and refill zones.

Usage: python3 tools/util/fix_clash.py BOARD.kicad_pcb DRC.json [--r 0.7]

Intended after a re-placement or a bulk ground-stitching pass: it removes exactly the tracks
and vias near error locations (skipping copper of the pad's own net) and leaves the verdict
to a repeated DRC run. Writes the board in place.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, math, json, pcbnew
MM = 1e6
board = sys.argv[1]
drc = json.load(open(sys.argv[2]))
R = float(sys.argv[sys.argv.index('--r') + 1]) if '--r' in sys.argv else 0.7
b = pcbnew.LoadBoard(board)
pts = []
for x in drc['violations']:
    if x['severity'] != 'error':
        continue
    for i in x['items']:
        pts.append((i['pos']['x'], i['pos']['y']))
pads = {}
for f in b.GetFootprints():
    for p in f.Pads():
        pads[(round(p.GetPosition().x / MM, 1), round(p.GetPosition().y / MM, 1))] = p.GetNetname()


def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


dead = []
for t in b.GetTracks():
    n = t.GetNetname()
    for (px, py) in pts:
        own = pads.get((round(px, 1), round(py, 1)))
        if own and n == own:
            continue
        if t.GetClass() == 'PCB_VIA':
            if math.hypot(t.GetPosition().x / MM - px, t.GetPosition().y / MM - py) < R + 0.2:
                dead.append(t)
                break
        else:
            s, e = t.GetStart(), t.GetEnd()
            if dseg(px, py, s.x / MM, s.y / MM, e.x / MM, e.y / MM) < R:
                dead.append(t)
                break
dead = list({id(t): t for t in dead}.values())
print('violation points: %d, copper items removed: %d' % (len(pts), len(dead)))
for t in dead:
    b.Remove(t)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
pcbnew.SaveBoard(board, b)
print('written', board)
