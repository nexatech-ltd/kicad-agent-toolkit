#!/usr/bin/env python3
"""Push the ring of small passives away from a large package to free its fan-out zone.

Motivation (measured on a large LQFP): ~40% of the signal pins had NO room for a via within
4 mm, and most had no escape from under the package at all. The cause was placement, not
routing: the decoupling ring sat 1.15..1.62 mm from the pad centres - exactly where the
fan-out vias must go.

Geometry that sets the target. A 1.5 mm long LQFP pad has its edge 0.75 mm from its centre.
A 0.6/0.3 via at 0.15 clearance: its centre is no closer than 0.75 + 0.15 + 0.30 = 1.20 mm
from the pad centre. A neighbour's courtyard must therefore start no closer than
1.20 + 0.30 + 0.15 = 1.65 mm for one via row, and 1.20 + 0.75 + 0.45 = 2.40 mm for two
staggered rows (via pitch within a row 1.0 mm where 0.75 is required).

So the needed shift is 0.5..1.3 mm - electrically negligible for a decoupling capacitor,
especially when its path to the pin goes through a plane anyway. The shift is strictly
radial from the package centre, which preserves which pin each capacitor serves.
"""
import sys, os, math, json, argparse, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp, paths

MM = 1e6


def courtyard_bbox(fp):
    import pcbnew
    for layer in (pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd,
                  pcbnew.F_CrtYd if fp.IsFlipped() else pcbnew.B_CrtYd):
        ps = fp.GetCourtyard(layer)
        if ps.OutlineCount():
            return ps.BBox()
    return fp.GetBoundingBox()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--ref', required=True, help='package to push the ring away from')
    ap.add_argument('--target', type=float, default=2.40,
                    help='required distance, mm, from courtyards to the pad centres')
    ap.add_argument('--max-move', type=float, default=3.0)
    ap.add_argument('--skip', default='', help='do not move these footprints')
    ap.add_argument('--step', type=float, default=0.1, help='raster step, mm')
    ap.add_argument('--clearance', type=float, default=0.15)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    import pcbnew
    b = pcbnew.LoadBoard(a.board)
    fps = {f.GetReference(): f for f in b.GetFootprints()}
    big = fps[a.ref]
    pads = [(p.GetPosition().x / MM, p.GetPosition().y / MM) for p in big.Pads()]
    cx, cy = big.GetPosition().x / MM, big.GetPosition().y / MM
    skip = {s.strip() for s in a.skip.split(',') if s.strip()} | {a.ref}

    def clearance(x, y, bb, dx=0.0, dy=0.0):
        """minimum distance from the courtyard corners (shifted by dx,dy) to the pad centres"""
        cs = [(bb.GetLeft() / MM + dx, bb.GetTop() / MM + dy),
              (bb.GetRight() / MM + dx, bb.GetTop() / MM + dy),
              (bb.GetLeft() / MM + dx, bb.GetBottom() / MM + dy),
              (bb.GetRight() / MM + dx, bb.GetBottom() / MM + dy)]
        return min(min(math.hypot(qx - px, qy - py) for px, py in pads) for qx, qy in cs)

    # Free space: board outline minus other courtyards, minus OTHER nets' copper and holes.
    # Without copper a blind radial shift drops capacitors right onto long tracks (measured:
    # tracks of 9-28 mm got blocked), trading one congestion for another.
    import numpy as np
    import raster
    g = raster.Grid(b, step=a.step)
    inb = raster.inside_board(b, g, inset=0.3)
    if isinstance(inb, tuple):
        inb = inb[0]
    holes, _ = raster.hole_mask(b, g, extra=a.clearance)
    cu_all = {}
    for L in ('F.Cu', 'B.Cu'):
        cu_all[L], _ = raster.copper_mask(b, g, L)
    print('raster ready: %dx%d, step %.2f mm' % (g.h, g.w, a.step))

    def free_for(ref, nets):
        cy, _ = raster.courtyard_mask_except(b, g, {ref})
        m = inb & ~cy & ~holes
        # other nets' copper on the outer layer (single-sided assembly, pads on F.Cu)
        cu, _ = raster.copper_mask(b, g, 'F.Cu', nets_skip=set(nets))
        m &= ~raster.dilate(cu, a.clearance, a.step)
        return m

    plan = []
    for ref, f in sorted(fps.items()):
        if ref in skip:
            continue
        bb = courtyard_bbox(f)
        if bb.GetWidth() > 12 * MM or bb.GetHeight() > 12 * MM:
            continue                       # large packages stay put
        d0 = clearance(0, 0, bb)
        if d0 >= a.target or d0 > 6.0:
            continue
        x, y = f.GetPosition().x / MM, f.GetPosition().y / MM
        vx, vy = x - cx, y - cy
        n = math.hypot(vx, vy) or 1.0
        vx, vy = vx / n, vy / n
        nets = sorted({p.GetNetname() for p in f.Pads() if p.GetNetname()})
        fm = free_for(ref, nets)
        hw = bb.GetWidth() / MM / 2.0
        hh = bb.GetHeight() / MM / 2.0
        fits = raster.erode(fm, max(hw, hh), a.step)
        best = None
        # search: radially outward first, then with a sideways offset
        s = 0.1
        while s <= a.max_move + 1e-9 and best is None:
            for off in [z * 0.25 for z in range(-8, 9)]:
                nx = x + vx * s - vy * off
                ny = y + vy * s + vx * off
                if clearance(0, 0, bb, nx - x, ny - y) < a.target:
                    continue
                r, c = g.row(ny), g.col(nx)
                if not (0 <= r < g.h and 0 <= c < g.w) or not fits[r, c]:
                    continue
                d = math.hypot(nx - x, ny - y)
                if best is None or d < best[0]:
                    best = (d, nx, ny)
            s += 0.1
        plan.append((ref, f.GetFieldText('Value')[:18], round(d0, 2),
                     round(best[0], 2) if best else None,
                     round(x, 3), round(y, 3),
                     round(best[1], 3) if best else round(x, 3),
                     round(best[2], 3) if best else round(y, 3)))

    print('footprints in the fan-out zone (closer than %.2f mm): %d' % (a.target, len(plan)))
    ok = [p for p in plan if p[3]]
    print('movable: %d, total shift %.1f mm, max %.1f mm'
          % (len(ok), sum(p[3] for p in ok), max((p[3] for p in ok), default=0)))
    for p in plan:
        print('   %-6s %-18s was %4.2f mm  shift %s -> (%.2f, %.2f)'
              % (p[0], p[1], p[2], ('%.1f mm' % p[3]) if p[3] else 'NOT FOUND', p[6], p[7]))
    json.dump(plan, open(paths.work('ring_plan.json'), 'w'), indent=0, ensure_ascii=False)

    if not a.apply:
        print('\ndry run; add --apply to write')
        return 0

    txt = open(a.board).read()
    n = 0
    for ref, val, d0, move, x0, y0, x1, y1 in plan:
        if not move:
            continue
        blk = None
        for i, j in sexp.blocks(txt, 'footprint'):
            t = txt[i:j]
            if '"%s"' % ref in t and '(property "Reference" "%s"' % ref in t:
                blk = (i, j)
                break
        if not blk:
            print('   %s: block not found' % ref)
            continue
        i, j = blk
        body = txt[i:j]
        at = sexp.sub(body, 'at')[0]        # the first (at ...) is the footprint position
        v = sexp.parse(at)[0]
        rest = ' '.join(str(z) for z in v[3:])
        new = '(at %.4f %.4f%s)' % (x1, y1, (' ' + rest) if rest else '')
        body = body.replace(at, new, 1)
        txt = txt[:i] + body + txt[j:]
        n += 1
    shutil.copy(a.board, a.board + '.bakRing')
    open(a.board, 'w').write(txt)
    print('\nfootprints moved: %d, backup .bakRing' % n)
    print('NOTE: copper did not follow the footprints - expect breaks; the router will close them')


if __name__ == '__main__':
    main()
