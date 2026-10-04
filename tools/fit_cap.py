#!/usr/bin/env python3
"""Find a spot for a two-pad part using the board's REAL rules, ranked by distance to a pin.

`util/cc_spots.py` answers "where is there no foreign copper" with a simplified criterion, and its
answers have to be checked with DRC one by one (~40 s per position). Here, for every position and
rotation, the tool computes the minimum clearance of EACH pad to foreign copper on its layer (tracks,
vias, pads - by real shape), the clearance to holes (`min_hole_clearance`) and courtyard overlap.
A position is valid only if all three hold; the distance to the target pin is printed.

A zone of the part's own net is not an obstacle: on refill it pulls back, and the pad connects to it.

    python3 tools/fit_cap.py --board B --ref C1 --near U1.3 \
        --nets VBUS,GND --radius 4 [--clr 0.15] [--hole-clr 0.25] [--step 0.05] [--top 8]

--clr defaults to the board's Track-Pad rule from the .kicad_dru (0.15 mm if there is none).
"""
import argparse
import math
import os
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project

MM = 1e6


def rect_dist(px, py, x0, y0, x1, y1):
    """Distance from a point to a rectangle (0 inside)."""
    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return math.hypot(dx, dy)


def seg_rect_dist(ax, ay, bx, by, x0, y0, x1, y1, samples=24):
    """Segment-to-rectangle distance: samples along the segment plus the rectangle corners."""
    best = min(rect_dist(ax + (bx - ax) * k / samples, ay + (by - ay) * k / samples,
                         x0, y0, x1, y1) for k in range(samples + 1))
    for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / l2))
        best = min(best, math.hypot(cx - (ax + t * dx), cy - (ay + t * dy)))
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--ref', required=True, help='footprint to place (already in the board file)')
    ap.add_argument('--near', required=True, help='REF.PIN - the pin to get close to')
    ap.add_argument('--nets', required=True, help='nets of pad 1 and pad 2, comma-separated')
    ap.add_argument('--radius', type=float, default=4.0)
    ap.add_argument('--clr', type=float, default=None,
                    help='pad-to-copper clearance (default: Track-Pad rule of the board, else 0.15)')
    ap.add_argument('--hole-clr', type=float, default=0.25, help="the board's min_hole_clearance")
    ap.add_argument('--step', type=float, default=0.05)
    ap.add_argument('--top', type=int, default=8)
    a = ap.parse_args()
    if a.clr is None:
        a.clr = project.rule_clearance('Track', 'Pad', a.board, default=0.15)

    own = [n.strip() for n in a.nets.split(',')]
    b = pcbnew.LoadBoard(a.board)
    fp = [f for f in b.GetFootprints() if f.GetReference() == a.ref][0]
    tref, tpin = a.near.split('.')
    tp = [p for f in b.GetFootprints() if f.GetReference() == tref
          for p in f.Pads() if p.GetNumber() == tpin][0]
    tx, ty = tp.GetPosition().x / MM, tp.GetPosition().y / MM
    L = pcbnew.F_Cu if not fp.IsFlipped() else pcbnew.B_Cu

    # own geometry: pad offsets from the centre at zero rotation and their sizes
    c0 = fp.GetPosition()
    base = []
    for p in fp.Pads():
        q = p.GetPosition()
        s = p.GetSize()
        base.append((p.GetNumber(), (q.x - c0.x) / MM, (q.y - c0.y) / MM, s.x / MM, s.y / MM))
    base.sort()
    cb = fp.GetCourtyard(pcbnew.F_CrtYd).BBox()
    cw, ch = cb.GetWidth() / MM, cb.GetHeight() / MM

    # Window: copper beyond the search radius plus a clearance margin is ignored. Without it a
    # 161x161 position sweep with four rotations takes hours - the whole board per position.
    WIN = a.radius + 2.5
    def near(*pts):
        return any(abs(x - tx) <= WIN and abs(y - ty) <= WIN for x, y in pts)

    # foreign copper on the mounting layer + holes + neighbours' courtyards
    segs, discs, prects, holes, crt = [], [], [], [], []
    for t in b.GetTracks():
        n = t.GetNetname().split('/')[-1]
        if t.GetClass() == 'PCB_VIA':
            q = t.GetPosition()
            if not near((q.x / MM, q.y / MM)):
                continue
            holes.append((q.x / MM, q.y / MM, t.GetDrill() / MM / 2))
            if n not in own:
                discs.append((n, q.x / MM, q.y / MM, t.GetWidth(L) / MM / 2))
        elif t.GetLayer() == L and n not in own:
            s, e = t.GetStart(), t.GetEnd()
            if not near((s.x / MM, s.y / MM), (e.x / MM, e.y / MM)):
                continue
            segs.append((n, s.x / MM, s.y / MM, e.x / MM, e.y / MM, t.GetWidth() / MM / 2))
    for f in b.GetFootprints():
        if f.GetReference() == a.ref:
            continue
        fq = f.GetPosition()
        if abs(fq.x / MM - tx) > WIN + 8 or abs(fq.y / MM - ty) > WIN + 8:
            continue
        bb = f.GetCourtyard(pcbnew.F_CrtYd if not f.IsFlipped() else pcbnew.B_CrtYd).BBox()
        if bb.GetWidth() > 0:
            crt.append((f.GetReference(), bb.GetLeft() / MM, bb.GetTop() / MM,
                        bb.GetRight() / MM, bb.GetBottom() / MM))
        for p in f.Pads():
            q = p.GetPosition()
            if not near((q.x / MM, q.y / MM)):
                continue
            d = p.GetDrillSizeX() / MM
            if d > 0:
                holes.append((q.x / MM, q.y / MM, d / 2))
            if not p.IsOnLayer(L) or p.GetNetname().split('/')[-1] in own:
                continue
            s = p.GetSize()
            prects.append(('%s.%s' % (f.GetReference(), p.GetNumber()),
                           q.x / MM - s.x / MM / 2, q.y / MM - s.y / MM / 2,
                           q.x / MM + s.x / MM / 2, q.y / MM + s.y / MM / 2))

    out = []
    n = int(a.radius / a.step)
    for iy in range(-n, n + 1):
        for ix in range(-n, n + 1):
            cx, cy = tx + ix * a.step, ty + iy * a.step
            for rot in (0, 90, 180, 270):
                ok = True
                worst = 9e9
                who = ''
                # courtyard
                w, h = (cw, ch) if rot % 180 == 0 else (ch, cw)
                gx0, gy0, gx1, gy1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
                for ref, x0, y0, x1, y1 in crt:
                    if gx0 < x1 and x0 < gx1 and gy0 < y1 and y0 < gy1:
                        ok = False
                        who = 'courtyard %s' % ref
                        break
                if not ok:
                    continue
                pads = []
                for num, ox, oy, pw, ph in base:
                    if rot == 0:
                        dx, dy, w2, h2 = ox, oy, pw, ph
                    elif rot == 90:
                        dx, dy, w2, h2 = -oy, ox, ph, pw
                    elif rot == 180:
                        dx, dy, w2, h2 = -ox, -oy, pw, ph
                    else:
                        dx, dy, w2, h2 = oy, -ox, ph, pw
                    pads.append((num, cx + dx - w2 / 2, cy + dy - h2 / 2,
                                 cx + dx + w2 / 2, cy + dy + h2 / 2))
                for num, x0, y0, x1, y1 in pads:
                    for nm, ax, ay, bx, by, hw in segs:
                        d = seg_rect_dist(ax, ay, bx, by, x0, y0, x1, y1) - hw
                        if d < worst:
                            worst, who = d, '%s track' % nm
                    for nm, vx, vy, vr in discs:
                        d = rect_dist(vx, vy, x0, y0, x1, y1) - vr
                        if d < worst:
                            worst, who = d, '%s via' % nm
                    for nm, px0, py0, px1, py1 in prects:
                        dx = max(px0 - x1, 0.0, x0 - px1)
                        dy = max(py0 - y1, 0.0, y0 - py1)
                        d = math.hypot(dx, dy)
                        if d < worst:
                            worst, who = d, 'pad %s' % nm
                    for hx, hy, hr in holes:
                        d = rect_dist(hx, hy, x0, y0, x1, y1) - hr - (a.hole_clr - a.clr)
                        if d < worst:
                            worst, who = d, 'hole (min_hole_clearance)'
                if worst >= a.clr:
                    d1 = min(math.hypot((p[1] + p[3]) / 2 - tx, (p[2] + p[4]) / 2 - ty)
                             for p in pads[:1])
                    out.append((d1, cx, cy, rot, worst, who))
    out.sort()
    print('part %s (courtyard %.2f x %.2f), target %s at (%.3f, %.3f); clearance %.2f, holes %.2f'
          % (a.ref, cw, ch, a.near, tx, ty, a.clr, a.hole_clr))
    print('valid positions: %d' % len(out))
    seen = []
    for d1, cx, cy, rot, worst, who in out:
        if any(math.hypot(cx - x, cy - y) < 0.4 for x, y, _ in seen):
            continue
        seen.append((cx, cy, rot))
        print('  (%.2f, %.2f) rot %-3d pad 1 at %.2f mm from the pin, margin %+.3f (%s)'
              % (cx, cy, rot, d1, worst - a.clr, who))
        if len(seen) >= a.top:
            break
    return 0


if __name__ == '__main__':
    sys.exit(main())
