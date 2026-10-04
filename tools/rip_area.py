#!/usr/bin/env python3
"""Remove the copper of the given nets inside an area - for targeted re-routing of a node.

Unlike `del_net_run` (util/edit_ops.py), where the distance to the net's OWN pads decides,
here it is being inside the area, because what blocks is usually a local escape, not a long
run. Pads are never touched: only moving the footprint moves them.

    python3 tools/rip_area.py --board B --nets A,B,C --at x,y --r 3.0 [--apply]
    python3 tools/rip_area.py --board B --nets A,B --box x0,y0,x1,y1 [--apply]
"""
import argparse
import collections
import math
import sys

import pcbnew

MM = 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--nets', required=True)
    ap.add_argument('--at', default=None, help='x,y - area centre')
    ap.add_argument('--r', type=float, default=3.0)
    ap.add_argument('--box', default=None, help='x0,y0,x1,y1 - a box instead of a circle')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    nets = {s.strip() for s in a.nets.split(',') if s.strip()}
    b = pcbnew.LoadBoard(a.board)
    if a.box:
        x0, y0, x1, y1 = (float(v) for v in a.box.split(','))
        def hit(pts):
            return any(x0 <= x <= x1 and y0 <= y <= y1 for x, y in pts)
    else:
        cx, cy = (float(v) for v in a.at.split(','))
        def hit(pts):
            return any(math.hypot(x - cx, y - cy) <= a.r for x, y in pts)

    gone = collections.Counter()
    doomed = []
    for t in b.GetTracks():
        n = t.GetNetname().split('/')[-1]
        if n not in nets:
            continue
        if t.GetClass() == 'PCB_VIA':
            q = t.GetPosition()
            pts = [(q.x / MM, q.y / MM)]
        else:
            s, e = t.GetStart(), t.GetEnd()
            pts = [(s.x / MM, s.y / MM), (e.x / MM, e.y / MM)]
        if hit(pts):
            doomed.append(t)
            gone[n] += 1
    for t in doomed:
        b.Remove(t)
    print('objects to remove: %d in %d nets' % (sum(gone.values()), len(gone)))
    for n, k in gone.most_common():
        print('   %-16s %d' % (n, k))
    if a.apply:
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        pcbnew.SaveBoard(a.board, b)
        print('board written')
    else:
        print('this is a plan, add --apply to write')
    return 0


if __name__ == '__main__':
    sys.exit(main())
