"""Undo the stitching links and vias (gnd_link / sig_link ops) that produced DRC errors.

Usage: python3 tools/util/rollback_links.py BOARD.kicad_pcb OPS.json DRC.json OUT.json

OPS.json is the operation list that added the links; DRC.json is a report taken after
applying it. A via and its link segment are removed together. The output is an operation
list for edit_ops.py.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, json, math

board, ops_path, drc_path, out_path = sys.argv[1:5]
ops = json.load(open(ops_path))
d = json.load(open(drc_path))
vias = [(o['x'], o['y'], o['net']) for o in ops if o['op'] == 'add_via']
segs = [(tuple(o['a']), tuple(o['b']), o['net'], o.get('layer', 'F.Cu')) for o in ops if o['op'] == 'add_seg']


def dseg(px, py, a, b):
    x1, y1 = a
    x2, y2 = b
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


bad = set()
for x in d['violations']:
    if x['severity'] != 'error':
        continue
    for i in x['items']:
        px, py = i['pos']['x'], i['pos']['y']
        for k, v in enumerate(vias):
            if math.hypot(px - v[0], py - v[1]) < 0.5:
                bad.add(('v', k))
        for k, s in enumerate(segs):
            if dseg(px, py, s[0], s[1]) < 0.35:
                bad.add(('s', k))
pair = set()
for kind, k in bad:
    if kind == 'v':
        for j, s in enumerate(segs):
            if math.hypot(s[1][0] - vias[k][0], s[1][1] - vias[k][1]) < 0.01:
                pair.add(('s', j))
    else:
        for j, v in enumerate(vias):
            if math.hypot(segs[k][1][0] - v[0], segs[k][1][1] - v[1]) < 0.01:
                pair.add(('v', j))
bad |= pair
out = [{'op': 'del_via_at', 'net': vias[k][2], 'x': vias[k][0], 'y': vias[k][1], 'r': 0.05} for kind, k in bad if kind == 'v']
out += [{'op': 'del_seg', 'net': segs[k][2], 'layer': segs[k][3], 'a': list(segs[k][0]), 'b': list(segs[k][1])} for kind, k in bad if kind == 's']
json.dump(out, open(out_path, 'w'))
print('removing %d of %d operations' % (len(out), len(ops)))
