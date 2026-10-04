#!/usr/bin/env python3
"""Where to open a "keyhole" for a through via while removing the MINIMUM of foreign copper.

`util/via_spots.py` answers "where is there room right now". When there is none anywhere - as
happens in a dense node - the question changes: which copper to remove to make room, and where
that price is lowest. For every grid cell this tool computes the set of NETS whose copper blocks
a via on at least one copper layer, and ranks cells by the number of such nets, then by their
size (a small net is cheaper to re-route).

What cannot be removed and thus disqualifies a cell: a foreign-net pad, its drill, a keepout, the
board-edge clearance. Plane-net pours are NOT obstacles - they pull back on refill; tracks and vias
of those nets are, and a plane stitch on the removal list is fine (`stitch_gnd.py` re-adds
stitching elsewhere).

    python3 tools/keyhole.py --board B --net NET \
        --window 110,72,114,78 [--near 111.9,72.9] [--via 0.5,0.2] [--clr 0.15] [--step 0.05] [--top 10]
"""
import argparse
import collections
import math
import os
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project

MM = 1e6
LAYERS = ('F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu')   # replaced by the board's stack in main()
PLANES = project.plane_nets()


def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--net', required=True, help='net that needs the keyhole (its own copper is not an obstacle)')
    ap.add_argument('--window', required=True, help='x0,y0,x1,y1')
    ap.add_argument('--near', default=None, help='x,y - point to stay close to (default: window centre)')
    ap.add_argument('--via', default='0.5,0.2')
    ap.add_argument('--clr', type=float, default=0.15)
    ap.add_argument('--hole-clr', type=float, default=0.25, help="the board's min_hole_clearance")
    ap.add_argument('--hole-to-hole', type=float, default=0.2)
    ap.add_argument('--edge', type=float, default=0.5, help='min_copper_edge_clearance')
    ap.add_argument('--step', type=float, default=0.05)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--protect', default=','.join(project.get('protect_nets', [])),
                    help='comma-separated nets that must not be removed (default: protect_nets from the config); '
                         'nets of the no_touch_classes net classes are always protected')
    a = ap.parse_args()

    vs, vd = (float(v) for v in a.via.split(','))
    vr, dr = vs / 2, vd / 2
    x0, y0, x1, y1 = (float(v) for v in a.window.split(','))
    nx, ny = (float(v) for v in a.near.split(',')) if a.near else ((x0 + x1) / 2, (y0 + y1) / 2)
    own = a.net.split('/')[-1]
    prot = {s.strip() for s in a.protect.split(',') if s.strip()}
    no_touch = set(project.get('no_touch_classes', []))
    WIN = 3.0

    b = pcbnew.LoadBoard(a.board)
    global LAYERS
    nin = b.GetCopperLayerCount() - 2
    LAYERS = tuple(['F.Cu'] + ['In%d.Cu' % i for i in range(1, nin + 1)] + ['B.Cu'])
    lid = {L: b.GetLayerID(L) for L in LAYERS}

    def near(*pts):
        return any(x0 - WIN <= x <= x1 + WIN and y0 - WIN <= y <= y1 + WIN for x, y in pts)

    segs = collections.defaultdict(list)   # layer -> (net, x1,y1,x2,y2, halfw)
    vias = []                              # (net, x, y, r, drill_r)
    padblock = []                          # (x0,y0,x1,y1) - foreign-net pads, cannot be removed
    padhole = []                           # (x, y, drill_r)
    size = collections.Counter()           # copper objects per net (re-route cost)
    for t in b.GetTracks():
        n = t.GetNetname().split('/')[-1]
        size[n] += 1
        if no_touch and no_touch & set(t.GetNetClassName().split(',')):
            prot.add(n)            # net of a protected net class (e.g. impedance-controlled RF)
        if t.GetClass() == 'PCB_VIA':
            q = t.GetPosition()
            if n != own and near((q.x / MM, q.y / MM)):
                vias.append((n, q.x / MM, q.y / MM, t.GetWidth(lid['F.Cu']) / MM / 2,
                             t.GetDrill() / MM / 2))
        else:
            s, e = t.GetStart(), t.GetEnd()
            if n != own and near((s.x / MM, s.y / MM), (e.x / MM, e.y / MM)):
                for L in LAYERS:
                    if t.GetLayer() == lid[L]:
                        segs[L].append((n, s.x / MM, s.y / MM, e.x / MM, e.y / MM,
                                        t.GetWidth() / MM / 2))
    for fp in b.GetFootprints():
        for p in fp.Pads():
            q = p.GetPosition()
            if not near((q.x / MM, q.y / MM)):
                continue
            d = p.GetDrillSizeX() / MM
            if d > 0:
                padhole.append((q.x / MM, q.y / MM, d / 2))
            if p.GetNetname().split('/')[-1] == own:
                continue
            sz = p.GetSize()
            padblock.append((q.x / MM - sz.x / MM / 2, q.y / MM - sz.y / MM / 2,
                             q.x / MM + sz.x / MM / 2, q.y / MM + sz.y / MM / 2))

    # board outline: use its bounding box, inset by edge clearance + via radius
    bb = b.GetBoardEdgesBoundingBox()
    bx0, by0 = bb.GetLeft() / MM + a.edge + vr, bb.GetTop() / MM + a.edge + vr
    bx1, by1 = bb.GetRight() / MM - a.edge - vr, bb.GetBottom() / MM - a.edge - vr

    out = []
    n_cells = 0
    yy = y0
    while yy <= y1 + 1e-9:
        xx = x0
        while xx <= x1 + 1e-9:
            n_cells += 1
            ok = bx0 <= xx <= bx1 and by0 <= yy <= by1
            if ok:
                for px0, py0, px1, py1 in padblock:
                    dx = max(px0 - xx, 0.0, xx - px1)
                    dy = max(py0 - yy, 0.0, yy - py1)
                    if math.hypot(dx, dy) < vr + a.clr:
                        ok = False
                        break
            if ok:
                for hx, hy, hr in padhole:
                    if math.hypot(hx - xx, hy - yy) < dr + hr + a.hole_to_hole:
                        ok = False
                        break
            if ok:
                rip = set()
                for L in LAYERS:
                    for n, ax, ay, bx, by, hw in segs[L]:
                        if dseg(xx, yy, ax, ay, bx, by) < vr + a.clr + hw:
                            rip.add(n)
                for n, vx, vy, r, d2 in vias:
                    dd = math.hypot(vx - xx, vy - yy)
                    if dd < vr + a.clr + r or dd < dr + d2 + a.hole_to_hole:
                        rip.add(n)
                if not (rip & prot):
                    out.append((len(rip), sum(size[n] for n in rip),
                                math.hypot(xx - nx, yy - ny), xx, yy, frozenset(rip)))
            xx += a.step
        yy += a.step

    print('window %s, net %s, via %.2f/%.2f, clearance %.2f, hole-to-hole %.2f'
          % (a.window, own, vs, vd, a.clr, a.hole_to_hole))
    print('cells checked %d, usable after copper removal %d' % (n_cells, len(out)))
    out.sort()
    seen = []
    for k, cost, dist, xx, yy, rip in out:
        if any(math.hypot(xx - px, yy - py) < 0.45 for px, py in seen):
            continue
        seen.append((xx, yy))
        tag = ' (already free)' if k == 0 else ''
        print('  (%.2f, %.2f) %.2f mm from target - nets to remove %d%s: %s'
              % (xx, yy, dist, k, tag,
                 ', '.join('%s[%d obj%s]' % (n, size[n], ', plane' if n in PLANES else '')
                           for n in sorted(rip)) or '-'))
        if len(seen) >= a.top:
            break
    return 0


if __name__ == '__main__':
    sys.exit(main())
