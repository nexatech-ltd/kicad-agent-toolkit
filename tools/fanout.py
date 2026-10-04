#!/usr/bin/env python3
"""Systematic fan-out from under a package: radial escape + via.

Why. On a large 0.5 mm-pitch LQFP most signal pins can end up with no escape at all
and no room for a via. The main cause is usually not a crowded board but the
package's own escapes, laid at an angle by earlier routing campaigns: they occupy
the ring and lock the remaining pins. After clearing the ring (`strip_annulus.py`)
room appeared for the large majority of pins, median distance ~1.25 mm.

Row geometry. Pad length 1.5 mm, its edge 0.75 mm from the centre. A 0.6/0.3 via
with 0.10 clearance (the net-class clearance; the `.kicad_dru` rules typically cover
only track-track and track-pad pairs) needs its centre at least
0.75 + 0.10 + 0.30 = 1.15 mm away. Hence two staggered rows:

    row 1 - 1.20 mm from the pad centre
    row 2 - 1.90 mm (row pitch 0.70 = 0.6 via + 0.1 clearance)

Within a row the pitch is 1.0 mm (every other pad) against the required 0.70 - fits.
A row-2 escape passes between row-1 vias: gap 1.0 - 0.6 = 0.4 mm, and a 0.15 mm
track leaves 0.125 mm on each side - more than the 0.10 net-class clearance.

The escape is laid 0.15 mm wide: it is an escape, not a power track, and
`audit_widths.py` classifies it as a package escape. The net gets its class width
beyond the ring.
"""
import sys, os, math, json, argparse, shutil, collections
import uuid as _uuid
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np
import pcbnew
import raster, paths

MM = 1e6
ESCAPE_W = 0.15
VIA_SIZE, VIA_DRILL = 0.6, 0.3
CLR = 0.10                 # Default net-class clearance
H2H = 0.20                 # board min_hole_to_hole
ROW = (1.20, 1.90, 2.60)   # row radii; the third is for pins whose first two are taken


def local(fp, x, y):
    """point coordinates in the footprint frame (centre at origin, rotation removed)"""
    cx, cy = fp.GetPosition().x / MM, fp.GetPosition().y / MM
    t = math.radians(-fp.GetOrientationDegrees())
    dx, dy = x - cx, y - cy
    return dx * math.cos(t) - dy * math.sin(t), dx * math.sin(t) + dy * math.cos(t)


def outward_dir(fp, x, y, half):
    """Outward direction tied to the package SIDE, not to its centre.

    The side is chosen by the larger normalised coordinate: escapes of one side are
    then strictly parallel, the in-row pitch is kept, and the stagger is not broken.
    A direction from the centre gave a diverging fan, vias drifted off the grid and
    locked their neighbours (115 vs 76 pins fanned out).

    Works for any peripheral package - QFP, QFN, TSSOP, FPC - because it does not
    rely on pin numbering.
    """
    lx, ly = local(fp, x, y)
    hx, hy = half
    nx = abs(lx) / hx if hx else 0.0
    ny = abs(ly) / hy if hy else 0.0
    if nx >= ny:
        d = (1.0 if lx >= 0 else -1.0, 0.0)
        along = ly
    else:
        d = (0.0, 1.0 if ly >= 0 else -1.0)
        along = lx
    t = math.radians(fp.GetOrientationDegrees())
    return (d[0] * math.cos(t) - d[1] * math.sin(t),
            d[0] * math.sin(t) + d[1] * math.cos(t)), along


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--refs', required=True,
                    help='comma-separated footprint references (e.g. U1,U2); fanned out one by one, '
                         'vias already placed count as obstacles')
    ap.add_argument('--step', type=float, default=0.05)
    ap.add_argument('--max-radius', type=float, default=3.0)
    ap.add_argument('--second-pass', action='store_true', default=True,
                    help='second pass with offsets for pins the strict grid found no room for')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    b = pcbnew.LoadBoard(a.board)
    allfp = {f.GetReference(): f for f in b.GetFootprints()}
    refs = [r.strip() for r in a.refs.split(',') if r.strip()]
    missing = [r for r in refs if r not in allfp]
    if missing:
        raise SystemExit('no such footprints: %s' % ', '.join(missing))

    g = raster.Grid(b, step=a.step)
    # Edge inset: with board rule min_copper_edge_clearance = 0.5 mm the via centre must be
    # at least 0.5 + 0.3 = 0.8 mm from the edge, the escape centre 0.5 + 0.075.
    # A smaller inset produced copper_edge_clearance violations.
    inb_via = raster.inside_board(b, g, inset=0.5 + VIA_SIZE / 2.0)
    if isinstance(inb_via, tuple):
        inb_via = inb_via[0]
    inb_trk = raster.inside_board(b, g, inset=0.5 + ESCAPE_W / 2.0)
    if isinstance(inb_trk, tuple):
        inb_trk = inb_trk[0]
    holes, _ = raster.hole_mask(b, g, extra=VIA_DRILL / 2.0 + H2H)
    cyM, _ = raster.courtyard_mask_except(b, g, set(refs))
    via_free = inb_via & ~holes & ~cyM
    trk_free = inb_trk & ~holes
    for L in raster.SIGNAL:
        cu, _ = raster.copper_mask(b, g, L)
        via_free &= ~raster.dilate(cu, VIA_SIZE / 2.0 + CLR, g.step)
        # Keepout zones: without them in the mask vias landed inside keepouts and DRC
        # reported items_not_allowed. The via is through-hole, so a keepout on ANY
        # layer excludes it.
        ko, _ = raster.keepout_mask(b, g, L)
        via_free &= ~raster.dilate(ko, VIA_SIZE / 2.0, g.step)
    cuF, _ = raster.copper_mask(b, g, 'F.Cu')
    koF, _ = raster.keepout_mask(b, g, 'F.Cu')
    trk_free &= ~raster.dilate(cuF, ESCAPE_W / 2.0 + CLR, g.step)
    trk_free &= ~raster.dilate(koF, ESCAPE_W / 2.0, g.step)
    print('raster %dx%d step %.2f | via cells free %d, escape cells free %d'
          % (g.h, g.w, a.step, int(via_free.sum()), int(trk_free.sum())))

    via_neg = g.empty()
    trk_neg = g.empty()
    made = []
    stat = []

    for ref in refs:
        fp = allfp[ref]
        bb = fp.GetBoundingBox()
        half = (max(bb.GetWidth() / MM / 2.0, 0.1), max(bb.GetHeight() / MM / 2.0, 0.1))
        pads = []
        for p in fp.Pads():
            net = p.GetNetname()
            if not net or net.startswith('unconnected-'):
                continue
            sx, sy = p.GetSizeX() / MM, p.GetSizeY() / MM
            if min(sx, sy) > 1.5:
                continue          # thermal pad: needs a via field, not an escape
            q = p.GetPosition()
            px, py = q.x / MM, q.y / MM
            # The margin within which the pin's OWN pad is not an obstacle is
            # rectangular by its size, not circular. A circular radius max(sx,sy)/2
            # for a 0.30x1.50 pad is 0.975 mm and covered the neighbouring pad 0.5 mm
            # away: the check skipped it and the escape was laid over it.
            skip = (sx / 2.0 + ESCAPE_W / 2.0 + CLR + 0.05,
                    sy / 2.0 + ESCAPE_W / 2.0 + CLR + 0.05)
            d, along = outward_dir(fp, px, py, half)
            pads.append((px, py, p.GetPadName(), net, skip, d, along))
        # pins that already have an escape are left alone
        todo = []
        for px, py, pn, net, skip, d, along in pads:
            has = False
            for t in b.GetTracks():
                if t.GetNetname() != net:
                    continue
                if t.GetClass() == 'PCB_VIA':
                    q = t.GetPosition()
                    if math.hypot(q.x / MM - px, q.y / MM - py) < 1.2:
                        has = True
                else:
                    for e in (t.GetStart(), t.GetEnd()):
                        if math.hypot(e.x / MM - px, e.y / MM - py) < 0.35:
                            has = True
                            break
                if has:
                    break
            if not has:
                todo.append((px, py, pn, net, skip, d, along))
        # row chosen by the parity of the index ALONG the side
        bydir = collections.defaultdict(list)
        for z in todo:
            bydir[(round(z[5][0], 3), round(z[5][1], 3))].append(z)
        ok = 0
        for key, group in bydir.items():
            group.sort(key=lambda z: z[6])
            for i, (px, py, pn, net, skip, d, along) in enumerate(group):
                mine = i % 2
                order = [ROW[mine]] + [r for k, r in enumerate(ROW) if k != mine]
                offs = [0.0] if a.second_pass is False else [0.0]
                placed = None
                for rad in order:
                    for off in offs:
                        X = px + d[0] * rad - d[1] * off
                        Y = py + d[1] * rad + d[0] * off
                        if try_place(g, via_free, via_neg, trk_free, trk_neg,
                                     px, py, X, Y, skip, a.step):
                            placed = (X, Y, rad)
                            break
                    if placed:
                        break
                if not placed and a.second_pass:
                    for rad in [x * 0.05 for x in range(int(ROW[0] / 0.05),
                                                        int(a.max_radius / 0.05) + 1)]:
                        for off in (0.0, -0.2, 0.2, -0.35, 0.35, -0.5, 0.5):
                            X = px + d[0] * rad - d[1] * off
                            Y = py + d[1] * rad + d[0] * off
                            if try_place(g, via_free, via_neg, trk_free, trk_neg,
                                         px, py, X, Y, skip, a.step):
                                placed = (X, Y, rad)
                                break
                        if placed:
                            break
                if placed:
                    X, Y, rad = placed
                    # Mutual reservations. Reserving a via only against vias and an escape
                    # only against escapes let a new via land on an escape just laid:
                    # shorting_items and clearance errors, all between objects of the SAME run.
                    raster.fill_disc(g, via_neg, X, Y, VIA_SIZE + CLR)
                    raster.fill_disc(g, trk_neg, X, Y,
                                     VIA_SIZE / 2.0 + CLR + ESCAPE_W / 2.0)
                    raster.fill_segment(g, trk_neg, px, py, X, Y, ESCAPE_W / 2.0 + CLR)
                    raster.fill_segment(g, via_neg, px, py, X, Y,
                                        ESCAPE_W / 2.0 + CLR + VIA_SIZE / 2.0)
                    made.append((pn, net, px, py, X, Y, round(rad, 2)))
                    ok += 1
        stat.append((ref, len(pads), len(todo), ok))

    print()
    print('  ref      pins     no escape   fanned out')
    for ref, tot, need, ok in stat:
        print('   %-7s %6d %10d %13d' % (ref, tot, need, ok))
    print('TOTAL: %d escapes and vias' % len(made))
    json.dump(made, open(paths.work('fanout.json'), 'w'), indent=0, ensure_ascii=False)

    if not a.apply:
        print('\nthis is a plan, add --apply to write')
        return 0
    if not made:
        return 0
    blocks = []
    for pn, net, px, py, X, Y, d in made:
        blocks.append('\t(segment\n\t\t(start %.4f %.4f)\n\t\t(end %.4f %.4f)\n'
                      '\t\t(width %.3f)\n\t\t(layer "F.Cu")\n\t\t(net "%s")\n'
                      '\t\t(uuid "%s")\n\t)'
                      % (px, py, X, Y, ESCAPE_W, net, _uuid.uuid4()))
        blocks.append('\t(via\n\t\t(at %.4f %.4f)\n\t\t(size %s)\n\t\t(drill %s)\n'
                      '\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "%s")\n\t\t(uuid "%s")\n\t)'
                      % (X, Y, VIA_SIZE, VIA_DRILL, net, _uuid.uuid4()))
    txt = open(a.board).read()
    k = txt.rstrip().rfind(')')
    txt = txt.rstrip()[:k] + '\n' + '\n'.join(blocks) + '\n)\n'
    shutil.copy(a.board, a.board + '.bakFanout')
    open(a.board, 'w').write(txt)
    print('written: %d escapes and %d vias, backup .bakFanout' % (len(made), len(made)))


def try_place(g, via_free, via_neg, trk_free, trk_neg, px, py, X, Y, skip, step):
    """whether (X,Y) is free for a via and the escape path from (px,py) to it is free"""
    r, c = g.row(Y), g.col(X)
    if not (0 <= r < g.h and 0 <= c < g.w):
        return False
    if not via_free[r, c] or via_neg[r, c]:
        return False
    dist = math.hypot(X - px, Y - py)
    steps = max(2, int(dist / step) + 1)
    for s in range(steps + 1):
        XX = px + (X - px) * s / steps
        YY = py + (Y - py) * s / steps
        rr, cc = g.row(YY), g.col(XX)
        if not (0 <= rr < g.h and 0 <= cc < g.w):
            return False
        if abs(XX - px) <= skip[0] and abs(YY - py) <= skip[1]:
            continue
        if not trk_free[rr, cc] or trk_neg[rr, cc]:
            return False
    return True


if __name__ == '__main__':
    main()
