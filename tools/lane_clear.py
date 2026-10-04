#!/usr/bin/env python3
"""Clear the escape lanes of locked pins of foreign copper on F.Cu.

Motivation: in a typical dense board most locked pins (roughly three quarters) are
locked by F.Cu copper, a fifth by vias, and only a few by a neighbour's courtyard.
So moving packages is not needed - top-layer transit traffic is in the way, while
copper density on the other layers is low: the room exists, it is just taken on the
wrong layer.

An escape lane is a rectangle from the pad outward: width of its short side plus
clearance, length `--reach`. Foreign F.Cu copper crossing the lane is removed. Own
copper and copper of plane nets are left alone.

Order matters: clear -> immediately occupy the lanes with a fan-out (`fanout.py`) ->
only then re-route what was removed. Otherwise the removed nets return to the same lanes.
"""
import sys, os, math, json, argparse, shutil, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcbnew
import sexp, paths, project, drcinfo as D

MM = 1e6
PLANE = project.plane_nets()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--reach', type=float, default=2.4, help='lane length outward, mm')
    ap.add_argument('--pad-clear', type=float, default=0.12,
                    help='margin on each side of the lane, mm')
    ap.add_argument('--keep-plane', action='store_true', default=True,
                    help='do not remove copper of plane nets')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    b = pcbnew.LoadBoard(a.board)
    d = D.load(a.drc)
    want = {net for net, k, dist, refs, pos in D.breaks(d) if 'Zone' not in k}

    lanes = []          # (net, x0,y0, x1,y1, half) - lane axis and half-width
    for fp in b.GetFootprints():
        cx, cy = fp.GetPosition().x / MM, fp.GetPosition().y / MM
        for p in fp.Pads():
            net = p.GetNetname()
            if net not in want or net.split('/')[-1] in PLANE:
                continue
            q = p.GetPosition()
            px, py = q.x / MM, q.y / MM
            has = False
            for t in b.GetTracks():
                if t.GetNetname() != net:
                    continue
                if t.GetClass() == 'PCB_VIA':
                    z = t.GetPosition()
                    if math.hypot(z.x / MM - px, z.y / MM - py) < 1.2:
                        has = True
                        break
                else:
                    for e in (t.GetStart(), t.GetEnd()):
                        if math.hypot(e.x / MM - px, e.y / MM - py) < 0.35:
                            has = True
                            break
                    if has:
                        break
            if has:
                continue
            vx, vy = px - cx, py - cy
            n = math.hypot(vx, vy) or 1.0
            vx, vy = vx / n, vy / n
            sx, sy = p.GetSizeX() / MM, p.GetSizeY() / MM
            half = min(sx, sy) / 2.0 + a.pad_clear
            long_half = max(sx, sy) / 2.0
            x0, y0 = px + vx * long_half, py + vy * long_half
            x1, y1 = px + vx * (long_half + a.reach), py + vy * (long_half + a.reach)
            lanes.append((net, x0, y0, x1, y1, half))
    print('escape lanes of locked pins: %d' % len(lanes))

    def seg_seg_dist(ax, ay, bx, by, cx2, cy2, dx2, dy2):
        def d_pt_seg(px, py, x1, y1, x2, y2):
            ddx, ddy = x2 - x1, y2 - y1
            L2 = ddx * ddx + ddy * ddy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * ddx + (py - y1) * ddy) / L2))
            return math.hypot(px - (x1 + t * ddx), py - (y1 + t * ddy))
        # For crossing segments all four "endpoint -> segment" distances are positive while the
        # true distance is zero: without this check a lane misses a track running straight across it.
        def cr(ox, oy, px, py, qx, qy):
            return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)
        d1 = cr(cx2, cy2, dx2, dy2, ax, ay); d2 = cr(cx2, cy2, dx2, dy2, bx, by)
        d3 = cr(ax, ay, bx, by, cx2, cy2); d4 = cr(ax, ay, bx, by, dx2, dy2)
        if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
            return 0.0
        return min(d_pt_seg(ax, ay, cx2, cy2, dx2, dy2), d_pt_seg(bx, by, cx2, cy2, dx2, dy2),
                   d_pt_seg(cx2, cy2, ax, ay, bx, by), d_pt_seg(dx2, dy2, ax, ay, bx, by))

    txt = open(a.board).read()
    kill = []
    stat = collections.Counter()
    F = 'F.Cu'
    for i, j in sexp.blocks(txt, 'segment'):
        blk = txt[i:j]
        lb = sexp.sub(blk, 'layer')
        nb = sexp.sub(blk, 'net')
        if not lb or not nb:
            continue
        if sexp.parse(lb[0])[0][1] != F:
            continue
        net = sexp.parse(nb[0])[0][1]
        base = net.split('/')[-1]
        if a.keep_plane and base in PLANE:
            continue
        sp = sexp.parse(sexp.sub(blk, 'start')[0])[0]
        ep = sexp.parse(sexp.sub(blk, 'end')[0])[0]
        wb = sexp.sub(blk, 'width')
        try:
            ax, ay = float(sp[1]), float(sp[2])
            bx, by = float(ep[1]), float(ep[2])
            w = float(sexp.parse(wb[0])[0][1]) if wb else 0.2
        except (ValueError, IndexError):
            continue
        for lnet, x0, y0, x1, y1, half in lanes:
            if lnet == net:
                continue          # own copper does not lock the lane
            if seg_seg_dist(ax, ay, bx, by, x0, y0, x1, y1) <= half + w / 2.0:
                kill.append((i, j))
                stat[base] += 1
                stat['__len'] += math.hypot(bx - ax, by - ay)
                break
    print('foreign F.Cu copper in lanes: %d segments, %.0f mm' % (len(kill), stat['__len']))
    top = [(k, v) for k, v in stat.most_common() if k != '__len']
    print('by net (top):', ', '.join('%s:%d' % z for z in top[:14]))
    for i, j in sorted(kill, reverse=True):
        txt = txt[:i - 1] + txt[j:]
    if a.apply:
        shutil.copy(a.board, a.board + '.bakLanes')
        open(a.board, 'w').write(txt)
        print('written, backup .bakLanes')
    else:
        print('dry run, --apply to write')


if __name__ == '__main__':
    main()
