"""Assign a net to the vias at planned points (after foreign copper under them was removed).

Usage: python3 tools/util/set_via_nets.py BOARD.kicad_pcb PLAN.json NET [--r 0.05] [--apply]

PLAN.json is a list of {"x": .., "y": ..} entries; every via within R of a point gets NET.
Zones are refilled before saving.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import json, math, sys, pcbnew
MM = 1e6
bp, planp, net = sys.argv[1], sys.argv[2], sys.argv[3]
R = float(sys.argv[sys.argv.index('--r') + 1]) if '--r' in sys.argv else 0.05
APPLY = '--apply' in sys.argv
b = pcbnew.LoadBoard(bp)
ni = b.GetNetInfo().GetNetItem(net)
assert ni, 'no such net ' + net
plan = json.load(open(planp))
n = 0
for e in plan:
    for t in b.GetTracks():
        if t.GetClass() != 'PCB_VIA':
            continue
        p = t.GetPosition()
        if math.hypot(p.x / MM - e['x'], p.y / MM - e['y']) <= R:
            if t.GetNetname() != net:
                print('  (%.3f,%.3f): %s -> %s' % (e['x'], e['y'], t.GetNetname(), net))
                t.SetNet(ni)
                n += 1
print('vias reassigned: %d' % n)
if APPLY and n:
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(bp, b)
    print('written', bp)
