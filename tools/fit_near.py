#!/usr/bin/env python3
"""Where a footprint fits next to a given pin - and what is in the way if it does not.

For cases where the location is dictated by physics, not convenience: an SMPS loop,
a bypass capacitor, a current shunt. A placer that targets the net's centre of mass
will honestly answer "14.9 mm" in a dense area - but the real question is what to
move to get 3 mm.

Answers two questions:
  where     nearest free points for footprint REF near pin REF2.PIN
  blockers  which footprints occupy the needed radius (candidates for moving)
"""
import sys, os, argparse, math
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np
import pcbnew
import raster, paths

MM = 1e6


def courtyard_size(fp):
    """Courtyard size of a footprint, mm.

    Courtyard, not GetBoundingBox: the latter includes reference and value texts and
    for a 2.5x2.0 mm inductor gives 9.4x6.1 - three times too large, after which
    "fits nowhere" is reported on empty space.
    """
    for layer in (pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd,
                  pcbnew.F_CrtYd if fp.IsFlipped() else pcbnew.B_CrtYd):
        ps = fp.GetCourtyard(layer)
        if ps.OutlineCount():
            bb = ps.BBox()
            return bb.GetWidth() / MM, bb.GetHeight() / MM
    bb = fp.GetBoundingBox()
    return bb.GetWidth() / MM, bb.GetHeight() / MM


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--ref', required=True, help='footprint to place')
    ap.add_argument('--near', required=True, help='REF.PIN - the pin to place near')
    ap.add_argument('--radius', type=float, default=8.0)
    ap.add_argument('--skip', default='', help='treat these footprints as absent')
    ap.add_argument('--step', type=float, default=0.1)
    a = ap.parse_args()

    b = pcbnew.LoadBoard(a.board)
    fps = {f.GetReference(): f for f in b.GetFootprints()}
    fp = fps[a.ref]
    tref, tpin = a.near.split('.')
    tp = {p.GetPadName(): p for p in fps[tref].Pads()}[tpin]
    tx, ty = tp.GetPosition().x / MM, tp.GetPosition().y / MM
    w, h = courtyard_size(fp)
    skip = {a.ref} | {s.strip() for s in a.skip.split(',') if s.strip()}
    print('footprint %s: courtyard %.2f x %.2f mm' % (a.ref, w, h))
    print('target: %s pin %s at (%.2f, %.2f), search radius %.1f mm'
          % (tref, tpin, tx, ty, a.radius))
    if skip - {a.ref}:
        print('treated as absent: %s' % ', '.join(sorted(skip - {a.ref})))

    g = raster.Grid(b, step=a.step)
    occ, n_cy = raster.courtyard_mask_except(b, g, skip)
    inside = raster.inside_board(b, g, inset=0.3)
    if isinstance(inside, tuple):
        inside = inside[0]
    free = inside & ~occ
    # the footprint fits if its whole rectangle is free -> erosion
    kw = max(1, int(round(w / a.step)))
    kh = max(1, int(round(h / a.step)))
    ok = raster.erode(free, max(w, h) / 2.0, a.step)

    rr, cc = g.row(ty), g.col(tx)
    k = int(a.radius / a.step)
    best = []
    for r in range(max(0, rr - k), min(g.h, rr + k + 1)):
        for c in range(max(0, cc - k), min(g.w, cc + k + 1)):
            if not ok[r, c]:
                continue
            d = math.hypot(g.x(c) - tx, g.y(r) - ty)
            if d <= a.radius:
                best.append((d, g.x(c), g.y(r)))
    best.sort()
    if best:
        print('\nnearest free points:')
        seen = []
        for d, x, y in best:
            if any(math.hypot(x - u, y - v) < 1.0 for u, v in seen):
                continue
            seen.append((x, y))
            print('   %5.2f mm  (%.2f, %.2f)' % (d, x, y))
            if len(seen) >= 6:
                break
    else:
        print('\nfits nowhere within %.1f mm' % a.radius)

    # blockers: footprints whose courtyard falls within the radius
    print('\nwhat occupies the %.1f mm radius (candidates for moving):' % a.radius)
    rows = []
    for f in b.GetFootprints():
        if f.GetReference() in (a.ref, tref):
            continue
        q = f.GetPosition()
        d = math.hypot(q.x / MM - tx, q.y / MM - ty)
        if d <= a.radius + 3:
            cw, ch = courtyard_size(f)
            nets = sorted({p.GetNetname().split('/')[-1] for p in f.Pads() if p.GetNetname()})
            rows.append((d, f.GetReference(), f.GetFieldText('Value')[:18], cw, ch,
                         ','.join(nets)[:30]))
    for d, r, v, cw, ch, nn in sorted(rows):
        print('   %5.2f mm  %-6s %-18s %4.1fx%-4.1f  %s' % (d, r, v, cw, ch, nn))


if __name__ == '__main__':
    main()
