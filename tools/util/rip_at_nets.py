"""Remove copper of the listed nets around planned via points, to make room for pre-placed vias.

Usage: python3 tools/util/rip_at_nets.py BOARD.kicad_pcb PLAN.json [--r 1.2] [--apply]

Each plan entry has a via point (x, y) and the list of nets blocking that particular point
under key "rip" (as produced by island_stitch.py --relax). Only those nets are removed, and
only within radius R, unlike fix_clash.py, which removes everything near violation points and
would also take out the new vias. The ripped net names are written to $KAT_WORK/rip_nets.txt
for a follow-up routing run.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import json, math, os, sys, pcbnew
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import paths
MM = 1e6
bp, planp = sys.argv[1], sys.argv[2]
R = float(sys.argv[sys.argv.index('--r') + 1]) if '--r' in sys.argv else 1.2
APPLY = '--apply' in sys.argv
plan = json.load(open(planp))
b = pcbnew.LoadBoard(bp)


def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


dead, by_net = {}, {}
for e in plan:
    want = set(e.get('rip', []))
    if not want:
        continue
    for t in b.GetTracks():
        n = t.GetNetname().split('/')[-1]
        if n not in want:
            continue
        p = t.GetPosition()
        if t.GetClass() == 'PCB_VIA':
            d = math.hypot(p.x / MM - e['x'], p.y / MM - e['y'])
        else:
            s, en = t.GetStart(), t.GetEnd()
            d = dseg(e['x'], e['y'], s.x / MM, s.y / MM, en.x / MM, en.y / MM)
        if d < R:
            dead[id(t)] = t
            by_net[n] = by_net.get(n, 0) + 1
for n, k in sorted(by_net.items(), key=lambda kv: -kv[1]):
    print('  %-14s removing %d' % (n, k))
print('total to remove: %d items, %d nets' % (len(dead), len(by_net)))
rip_txt = paths.work('rip_nets.txt')
open(rip_txt, 'w').write('\n'.join(sorted(by_net)) + '\n')
print('nets ->', rip_txt)
if APPLY and dead:
    for t in dead.values():
        b.Remove(t)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(bp, b)
    print('written', bp)
