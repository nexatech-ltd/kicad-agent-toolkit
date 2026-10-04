#!/usr/bin/env python3
"""Who exactly locks a pin: for every unconnected pin, the first obstacle in its escape lane.

On a locked pin the router answers "no path (1 cells explored)" and does not say what is
in the way. For each such pin this tool probes a radial lane outward and names the first
obstacle: whose courtyard, foreign copper or a hole.

This makes re-placement targeted: instead of moving "the whole decoupling ring" (tried -
free space was found for only 6 parts of 20), move the parts that actually sit in the lane.
"""
import sys, os, math, json, argparse, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np
import pcbnew
import raster, paths, project, drcinfo as D

MM = 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--step', type=float, default=0.05)
    ap.add_argument('--reach', type=float, default=3.0)
    a = ap.parse_args()

    b = pcbnew.LoadBoard(a.board)
    d = D.load(a.drc)
    PLANE = project.plane_nets()
    # pins involved in signal breaks
    want = set()
    for net, kinds, dist, refs, pos in D.breaks(d):
        if 'Zone' in kinds:
            continue
        want.add(net)

    g = raster.Grid(b, step=a.step)
    # separate masks so the culprit can be named
    inb = raster.inside_board(b, g, inset=0.5)
    if isinstance(inb, tuple):
        inb = inb[0]
    holes, _ = raster.hole_mask(b, g, extra=0.25)
    cuF, _ = raster.copper_mask(b, g, 'F.Cu')
    cuF = raster.dilate(cuF, 0.25, g.step)
    # per-footprint courtyards: to know whose
    own_cy = {}
    for fp in b.GetFootprints():
        ps = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
        outs = raster._polyset_outlines(ps)
        if not outs:
            continue
        m = g.empty()
        for pts in outs:
            raster.fill_poly(g, m, pts)
        own_cy[fp.GetReference()] = m

    blame = collections.Counter()
    detail = collections.defaultdict(list)
    trapped = 0
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        cx, cy = fp.GetPosition().x / MM, fp.GetPosition().y / MM
        for p in fp.Pads():
            net = p.GetNetname()
            if net not in want:
                continue
            base = net.split('/')[-1]
            if base in PLANE:
                continue
            q = p.GetPosition()
            px, py = q.x / MM, q.y / MM
            # does the pin have own copper? if so, it is not locked
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
            trapped += 1
            vx, vy = px - cx, py - cy
            n = math.hypot(vx, vy) or 1.0
            vx, vy = vx / n, vy / n
            sz = max(p.GetSizeX(), p.GetSizeY()) / MM / 2.0
            first = None
            s = sz + 0.3
            while s <= a.reach and first is None:
                X, Y = px + vx * s, py + vy * s
                r, c = g.row(Y), g.col(X)
                if not (0 <= r < g.h and 0 <= c < g.w):
                    first = ('outside raster', None)
                    break
                if not inb[r, c]:
                    first = ('board edge', None)
                    break
                for oref, m in own_cy.items():
                    if oref == ref:
                        continue
                    if m[r, c]:
                        first = ('courtyard', oref)
                        break
                if first:
                    break
                if holes[r, c]:
                    first = ('hole', None)
                    break
                if cuF[r, c]:
                    first = ('F.Cu copper', None)
                    break
                s += a.step
            if first is None:
                continue          # lane is free, the pin is not locked by an obstacle
            kind, who = first
            blame[(kind, who)] += 1
            detail[(kind, who)].append('%s.%s' % (ref, p.GetPadName()))

    print('locked pins in signal breaks: %d' % trapped)
    print()
    print('first obstacle in the radial lane:')
    for (kind, who), n in blame.most_common(24):
        nm = '%s %s' % (kind, who) if who else kind
        print('   %-24s %3d   e.g. %s' % (nm, n, ', '.join(detail[(kind, who)][:5])))
    json.dump({('%s|%s' % k): v for k, v in detail.items()},
              open(paths.work('blockers.json'), 'w'), indent=0, ensure_ascii=False)


if __name__ == '__main__':
    main()
