#!/usr/bin/env python3
"""Spread a row of identical packages together with their support parts and clear the region's copper.

Why. In a tight row of identical ICs (e.g. motor drivers at a 9 mm pitch) the capacitors of both
neighbours sit between the packages; there is nowhere to move them (nudge: "does not fit" at every
step), pins are locked, and the region does not route from scratch (around a hundred nets - the
negotiation does not converge). The only resource is free space beyond one end of the row.

What it does: the k-th package of the row (in the order given) moves by k*dx in --dir together
with its "satellites" - small parts in the row band whose centre is closer to it than to the
other packages along the row. Tracks/vias touching the pads of moved parts are removed; copper
inside the band is removed as well, so the router can re-route the region in its new layout.
DRC and the router campaign judge the result.

    python3 tools/gpuroute/spread_row.py --board B --refs U1,U2,U3,U4 --dx 2.0 [--dir right] [--apply]
"""
import argparse
import math
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pcbnew

import drcinfo as D
import paths
import place_geom as P

MM = 1e6
SMALL = 4.5
DIRS = {'right': (1, 0), 'left': (-1, 0), 'down': (0, 1), 'up': (0, -1)}


def v2(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def pad_rects(fps):
    out = []
    for fp in fps:
        for p in fp.Pads():
            bb = p.GetBoundingBox()
            out.append(((bb.GetX() / MM, bb.GetY() / MM, (bb.GetX() + bb.GetWidth()) / MM,
                         (bb.GetY() + bb.GetHeight()) / MM), p.GetNetname()))
    return out


def inside(r, x, y, g=0.05):
    return r[0] - g <= x <= r[2] + g and r[1] - g <= y <= r[3] + g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--refs', required=True,
                    help='packages of the row, comma-separated, in order: the first stays, the k-th moves by k*dx')
    ap.add_argument('--dx', type=float, default=2.0, help='spread step per package, mm')
    ap.add_argument('--dir', choices=sorted(DIRS), default='right',
                    help='direction to spread into (where the free space is); right/left = row along X')
    ap.add_argument('--band', type=float, default=7.0, help='half-width of the row band across the row, mm')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    ux, uy = DIRS[a.dir]
    horiz = ux != 0
    sgn = ux + uy

    def along(x, y):
        return x if horiz else y

    def across(x, y):
        return y if horiz else x

    refs = [r.strip() for r in a.refs.split(',')]
    board = pcbnew.LoadBoard(a.board)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
    row = [fps[r] for r in refs]
    ss = [along(fp.GetPosition().x / MM, fp.GetPosition().y / MM) for fp in row]
    tt = [across(fp.GetPosition().x / MM, fp.GetPosition().y / MM) for fp in row]
    t0 = sum(tt) / len(tt)
    s_lo, s_hi = min(ss) - 5.0, max(ss) + 5.0
    # satellites: small parts in the row band, assigned to the nearest package of the row
    groups = {r: [fps[r]] for r in refs}
    for fp in board.GetFootprints():
        r = fp.GetReference()
        if r in refs:
            continue
        bb = P.bbox_mm(fp)
        if max(bb[2] - bb[0], bb[3] - bb[1]) > SMALL:
            continue
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        cs, ct = along(cx, cy), across(cx, cy)
        if not (s_lo <= cs <= s_hi and abs(ct - t0) <= a.band):
            continue
        k = min(range(len(refs)), key=lambda i: abs(cs - ss[i]))
        groups[refs[k]].append(fp)
    moves = {}
    for k, r in enumerate(refs):
        d = k * a.dx
        for fp in groups[r]:
            moves[fp.GetReference()] = (fp, d)
    print('row %s, dx=%.1f %s: groups %s' % (', '.join(refs), a.dx, a.dir,
          ', '.join('%s+%d' % (r, len(groups[r]) - 1) for r in refs)))
    # courtyard check after the shift: only NEW intersections count (the original placement
    # already has courtyard contacts within 0.1 mm - not our concern)
    rules = P.rule_boxes(board)
    allbb = {o.GetReference(): P.bbox_mm(o) for o in board.GetFootprints()}
    def pairs(bbs):
        out = set()
        keys = sorted(bbs)
        for i, r1 in enumerate(keys):
            for r2 in keys[i + 1:]:
                if P.overlap(bbs[r1], bbs[r2]):
                    out.add((r1, r2))
        return out
    pre = pairs(allbb)
    newbb = dict(allbb)
    for r, (fp, d) in moves.items():
        b = allbb[r]
        newbb[r] = (b[0] + d * ux, b[1] + d * uy, b[2] + d * ux, b[3] + d * uy)
    bad = sorted(pairs(newbb) - pre)
    for r, (fp, d) in moves.items():
        if not P.inside_board(newbb[r]) or any(P.overlap(newbb[r], rb) for rb in rules):
            bad.append((r, 'edge/keepout'))
    if bad:
        print('DOES NOT FIT: %s' % ', '.join('%sx%s' % pr for pr in bad))
        return 1
    # copper to remove: touching the pads of moved parts + everything inside the band
    rects_old = pad_rects([fp for fp, _ in moves.values()])
    dmax = max(moves[r][1] for r in moves)
    lo = s_lo - 1.0 - (dmax if sgn < 0 else 0.0)
    hi = s_hi + 1.0 + (dmax if sgn > 0 else 0.0)
    if horiz:
        band = (lo, t0 - a.band - 1.0, hi, t0 + a.band + 1.0)
    else:
        band = (t0 - a.band - 1.0, lo, t0 + a.band + 1.0, hi)
    kill = []
    for t in board.GetTracks():
        net = t.GetNetname()
        plane = net.split('/')[-1] in D.PLANE_NETS
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition(); x, y = p.x / MM, p.y / MM
            touch = any(n == net and inside(r, x, y) for r, n in rects_old)
            inband = inside(band, x, y, 0)
        else:
            s, e = t.GetStart(), t.GetEnd()
            touch = any(n == net and (inside(r, s.x / MM, s.y / MM) or inside(r, e.x / MM, e.y / MM))
                        for r, n in rects_old)
            inband = inside(band, s.x / MM, s.y / MM, 0) and inside(band, e.x / MM, e.y / MM, 0)
        # ALL copper of the band is removed, plane nets included: plane vias left behind ended up
        # under the moved pads (hole_clearance violations and shorts). fix_plane_pads.py
        # reconnects the plane pads afterwards.
        if touch or inband:
            kill.append(t)
    print('copper to remove: %d items (band x %.0f..%.0f, y %.0f..%.0f)'
          % (len(kill), band[0], band[2], band[1], band[3]))
    if not a.apply:
        print('this is a plan; add --apply to write it')
        return 0
    for t in kill:
        board.Remove(t)
    for r, (fp, d) in moves.items():
        pos = fp.GetPosition()
        fp.SetPosition(v2(pos.x / MM + d * ux, pos.y / MM + d * uy))
    # Foreign copper under the NEW pad positions (any layer: a QFN exposed pad may be through-hole) -
    # a long signal track on an inner layer ran under a moved package and caused a short.
    rects_new = pad_rects([fp for fp, _ in moves.values()])
    kill2 = []
    for t in board.GetTracks():
        net = t.GetNetname()
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition()
            hit = any(n != net and inside(r, p.x / MM, p.y / MM, 0.35) for r, n in rects_new)
        else:
            s_, e_ = t.GetStart(), t.GetEnd()
            x1, y1, x2, y2 = s_.x / MM, s_.y / MM, e_.x / MM, e_.y / MM
            n_ = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.05) + 1)
            hit = False
            for r, n in rects_new:
                if n == net:
                    continue
                if any(inside(r, x1 + (x2 - x1) * i / n_, y1 + (y2 - y1) * i / n_, 0.25) for i in range(n_ + 1)):
                    hit = True
                    break
        if hit:
            kill2.append(t)
    for t in kill2:
        board.Remove(t)
    print('foreign copper removed under the new pads: %d' % len(kill2))
    shutil.copy2(a.board, a.board + '.bakSpread')
    pcbnew.SaveBoard(a.board, board)
    print('board written: %d parts moved, %d items removed, backup .bakSpread' % (len(moves), len(kill)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
