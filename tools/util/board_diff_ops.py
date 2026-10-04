"""Copper difference between two boards (live vs. trial copy) as an operation list for edit_ops.py.

Usage: python3 tools/util/board_diff_ops.py LIVE.kicad_pcb COPY.kicad_pcb OPS.json

Footprints must match (position/rotation/layer), otherwise the tool refuses: transfer part moves
with nudge_part.py first. Each board is loaded in its own subprocess, because a second LoadBoard
in one KiCad 10 SWIG process is unreliable. Apply the result with edit_ops.py LIVE OPS.json.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, json, subprocess
DUMP = '''
import pcbnew, json, sys
b=pcbnew.LoadBoard(sys.argv[1]); MM=1e6; R=[]
for t in b.GetTracks():
    n=t.GetNetname().split('/')[-1]
    if t.GetClass()=='PCB_VIA': R.append(['V', n, round(t.GetPosition().x/MM,3), round(t.GetPosition().y/MM,3), round(t.GetWidth()/MM,3), round(t.GetDrillValue()/MM,3)])
    else: R.append(['T', n, b.GetLayerName(t.GetLayer()), round(t.GetStart().x/MM,3), round(t.GetStart().y/MM,3), round(t.GetEnd().x/MM,3), round(t.GetEnd().y/MM,3), round(t.GetWidth()/MM,3)])
F=[[f.GetReference(), round(f.GetPosition().x/MM,3), round(f.GetPosition().y/MM,3), round(f.GetOrientationDegrees(),1), f.GetLayer()] for f in b.GetFootprints()]
print(json.dumps({'tr':R,'fp':F}))
'''
def dump(path):
    out = subprocess.run([sys.executable, '-c', DUMP, path], capture_output=True, text=True)
    return json.loads([l for l in out.stdout.splitlines() if l.startswith('{')][-1])
live, copy, dst = sys.argv[1:4]
a, c = dump(live), dump(copy)
if a['fp'] != c['fp']:
    moved = [x for x, y in zip(a['fp'], c['fp']) if x != y]
    sys.exit('footprints differ (%d): %s' % (len(moved), moved[:5]))
import collections
A = collections.Counter(map(tuple, a['tr'])); C = collections.Counter(map(tuple, c['tr']))   # multisets: duplicate vias at one point stay distinguishable
ops = []
for r in sorted((A - C).elements()):
    if r[0] == 'V': ops.append({'op': 'del_via_at', 'net': r[1], 'x': r[2], 'y': r[3], 'r': 0.01})
    else: ops.append({'op': 'del_seg', 'net': r[1], 'layer': r[2], 'a': [r[3], r[4]], 'b': [r[5], r[6]]})
for r in sorted((C - A).elements()):
    if r[0] == 'V': ops.append({'op': 'add_via', 'net': r[1], 'x': r[2], 'y': r[3], 'size': r[4], 'drill': r[5]})
    else: ops.append({'op': 'add_seg', 'net': r[1], 'layer': r[2], 'a': [r[3], r[4]], 'b': [r[5], r[6]], 'w': r[7]})
json.dump(ops, open(dst, 'w'), indent=0)
print('operations: %d (remove %d, add %d) -> %s' % (len(ops), sum((A - C).values()), sum((C - A).values()), dst))
