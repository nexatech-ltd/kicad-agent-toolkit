#!/usr/bin/env python3
"""Remove overlapping vias.

Freerouting sometimes returns two vias of one net at the same point in the SES;
after import they give hole_to_hole with 0.0000 mm clearance. Electrically this
changes nothing, but the fab receives two coincident drill hits.

Duplicates and pairs closer than the hole clearance are removed only when they
belong to the SAME net: for different nets such proximity is not a duplicate but
a placement error that must be investigated, not hidden.

Usage:  python3 dedup_vias.py [--board B] [--apply]
"""
import re, sys, os, math, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py is a sibling
from sexp import blocks

import paths
BRD = sys.argv[sys.argv.index('--board') + 1] if '--board' in sys.argv else paths.BOARD
APPLY = '--apply' in sys.argv
HOLE_CLEAR = 0.20      # min hole-to-hole from the board setup

_lck = os.path.join(os.path.dirname(os.path.abspath(BRD)), '~' + os.path.basename(BRD) + '.lck')
if os.path.exists(_lck) and APPLY:
    raise SystemExit(f'REFUSED: the board is open in the KiCad editor ({_lck}).')

s = open(BRD).read()
vias = []
for a, b in blocks(s, 'via'):
    blk = s[a:b]
    at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)\)', blk)
    nm = re.search(r'\(net "([^"]*)"\)', blk)
    dr = re.search(r'\(drill ([\d.]+)\)', blk)
    if not (at and dr): continue
    vias.append((a, b, nm.group(1) if nm else '', float(at.group(1)), float(at.group(2)),
                 float(dr.group(1)) / 2))
print(f'vias parsed: {len(vias)}')
assert vias or '(via' not in s, 'incomplete parse'

CELL = 1.0
grid = collections.defaultdict(list)
for k, v in enumerate(vias):
    grid[(int(v[3] // CELL), int(v[4] // CELL))].append(k)

drop, pairs = set(), []
for k, v in enumerate(vias):
    if k in drop: continue
    ci, cj = int(v[3] // CELL), int(v[4] // CELL)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for m in grid.get((ci + di, cj + dj), ()):
                if m <= k or m in drop: continue
                w = vias[m]
                if w[2] != v[2]: continue          # different nets: not a duplicate
                d = math.hypot(w[3] - v[3], w[4] - v[4]) - v[5] - w[5]
                if d < HOLE_CLEAR:
                    drop.add(m)
                    pairs.append((round(d, 4), v[2].split('/')[-1], v[3], v[4], w[3], w[4]))

print(f'overlapping vias to remove: {len(drop)}')
for d, net, x1, y1, x2, y2 in sorted(pairs):
    kind = 'coincident' if d <= -0.0001 or abs(x1-x2) + abs(y1-y2) < 1e-6 else f'{d:.4f} mm'
    print(f'  {net:14s} ({x1},{y1}) and ({x2},{y2}): {kind}')
if not drop:
    sys.exit(0)
if not APPLY:
    print('\nplan only. To write: python3 dedup_vias.py --apply')
    sys.exit(0)

open(BRD + '.bakDedup', 'w').write(s)
out, prev = [], 0
for k in sorted(drop, key=lambda k: vias[k][0]):
    a, b = vias[k][0], vias[k][1]
    out.append(s[prev:a]); prev = b
out.append(s[prev:])
open(BRD, 'w').write(''.join(out))
print(f'\nremoved {len(drop)} vias, backup .bakDedup')
