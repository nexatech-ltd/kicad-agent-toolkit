#!/usr/bin/env python3
"""Push the small parts that lock the pins of a package outwards, to open an escape corridor.

Typical picture around a QFN power or driver IC: a row of 0402 capacitors sits 0.2 mm from a
column of QFN pins, leaving a corridor for one 0.15 track while three to five nets must escape
there. No routing solves this - placement does: the row is moved away from the package by
0.3...1.5 mm (all parts of the row together, so their mutual clearances do not change), and
track ends and vias lying in the moved pads move with them.

Criteria: the courtyard does not intersect others (0.1 margin), stays inside the board, not in
keepouts; after the shift at least one locked pin of the package becomes reachable (locked check).
The smallest shift giving the most opened pins wins. DRC judges (the wrapper campaign).

    python3 tools/gpuroute/nudge.py --board B --drc D --ref U1 [--apply]
    python3 tools/gpuroute/nudge.py --board B --drc D --all [--apply]
"""
import argparse
import collections
import math
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pcbnew
import torch

import drcinfo as D
import paths
import project
import raster
import maze_route as MR
import connectivity as CN
import place_geom as P
from model import Model
from wave import Wave
from rip_campaign import locked_ends

MM = 1e6
SMALL = 4.0          # mm: largest courtyard side of a 'small' part (0805/1206 shunts are 3.2-3.6)
NEAR = 3.0           # mm: a part counts as locking if its centre is closer to the pin
STEPS = [0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5]
# Nets whose copper must never be removed by a nudge: those listed in `protect_nets`
# and those whose class clearance is >= `never_rip_min_clearance` (e.g. a 0.30 RF class).
PROTECT_NETS = set(project.get('protect_nets', []))
PROTECTED_CLEARANCE = float(project.get('never_rip_min_clearance', 0.3))


def protected(board_path, net):
    if net in PROTECT_NETS or net.split('/')[-1] in PROTECT_NETS:
        return True
    return bool(board_path) and MR.net_class_width(board_path, net)[1] >= PROTECTED_CLEARANCE


def v2(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def outward(fp, x, y):
    """Axis-aligned unit vector from the package centre towards the pin."""
    cx, cy = fp.GetPosition().x / MM, fp.GetPosition().y / MM
    dx, dy = x - cx, y - cy
    if abs(dx) >= abs(dy):
        return (1.0 if dx > 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy > 0 else -1.0)


def pad_rect(p):
    """Pad bounding rectangle in mm - for finding track ends inside it."""
    bb = p.GetBoundingBox()
    return (bb.GetX() / MM, bb.GetY() / MM, (bb.GetX() + bb.GetWidth()) / MM,
            (bb.GetY() + bb.GetHeight()) / MM)


def inside(r, x, y, grow=0.05):
    return r[0] - grow <= x <= r[2] + grow and r[1] - grow <= y <= r[3] + grow


def movers_for(board, ic, ends, u):
    """Small parts in the half-plane outwards from the locked pins of side u."""
    out = {}
    for fp in board.GetFootprints():
        if fp.GetReference() == ic.GetReference():
            continue
        bb = P.bbox_mm(fp)
        if max(bb[2] - bb[0], bb[3] - bb[1]) > SMALL:
            continue
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        for (_, x, y, _, _) in ends:
            d = math.hypot(cx - x, cy - y)
            along = (cx - x) * u[0] + (cy - y) * u[1]
            if d <= NEAR and along > 0.1:
                out[fp.GetReference()] = fp
                break
    return out


def seg_hits_rect(x1, y1, x2, y2, r):
    """Does the segment cross the rectangle: segment points every 0.05 mm (20 points on a long
    segment missed a 0.4 mm-wide pad, which gave a 0.105 clearance violation after a shift)."""
    n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.05) + 1)
    for i in range(n + 1):
        t = i / float(n)
        if inside(r, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, 0.0):
            return True
    return False


def shift_ok(board, movers, dx, dy, board_path=None, clr=0.3):
    """(acceptable?, foreign copper under the moved pads).

    Not acceptable: the courtyard touches another one / a keepout / the edge, or copper of a
    protected net (class clearance >= PROTECTED_CLEARANCE, e.g. an RF feed) ends up under a pad.
    Foreign TRACKS and VIAS under a pad are allowed - they are removed together with the shift
    (the router re-routes their nets); the list is returned."""
    rules = P.rule_boxes(board)
    others = [P.bbox_mm(o, 0.1) for o in board.GetFootprints() if o.GetReference() not in movers]
    pads = []
    for fp in movers.values():
        b = P.bbox_mm(fp)
        nb = (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)
        if not P.inside_board(nb):
            return False, []
        if any(P.overlap(nb, o) for o in others) or any(P.overlap(nb, rb) for rb in rules):
            return False, []
        for p in fp.Pads():
            r = pad_rect(p)
            pads.append(((r[0] + dx - clr, r[1] + dy - clr, r[2] + dx + clr, r[3] + dy + clr),
                         p.GetNetname()))
    blockers = []
    for t in board.GetTracks():
        net = t.GetNetname()
        if t.GetClass() == 'PCB_VIA':
            # A foreign via under the new place is removed too (fix_plane_pads restores it for a
            # plane net, the router re-routes a signal net). As a hard ban it gave "does not fit"
            # at every step for every driver IC - GND vias sit under the capacitors.
            pos = t.GetPosition()
            if any(n != net and inside(r, pos.x / MM, pos.y / MM, 0.15) for r, n in pads):
                if protected(board_path, net):
                    return False, []
                blockers.append(t)
            continue
        s, e = t.GetStart(), t.GetEnd()
        if any(n != net and seg_hits_rect(s.x / MM, s.y / MM, e.x / MM, e.y / MM, r) for r, n in pads):
            if protected(board_path, net):
                return False, []
            blockers.append(t)
    return True, blockers


def do_shift(board, movers, dx, dy):
    """Move the parts and the ends of their tracks/vias that lay in the pads."""
    rects = []
    for fp in movers.values():
        for p in fp.Pads():
            rects.append((pad_rect(p), p.GetNetname()))
    moved = 0
    kill = []
    for t in board.GetTracks():
        net = t.GetNetname()
        if t.GetClass() == 'PCB_VIA':
            pos = t.GetPosition()
            if any(n == net and inside(r, pos.x / MM, pos.y / MM) for r, n in rects):
                t.SetPosition(v2(pos.x / MM + dx, pos.y / MM + dy))
                moved += 1
            continue
        s, e = t.GetStart(), t.GetEnd()
        if any(n == net and (inside(r, s.x / MM, s.y / MM) or inside(r, e.x / MM, e.y / MM))
               for r, n in rects):
            # A stretched segment caused clearance violations to neighbouring pads (0.105 and
            # 0.005 mm). The segment is removed: the router lays a new one by the rules.
            kill.append(t)
    for t in kill:
        board.Remove(t)
    for fp in movers.values():
        pos = fp.GetPosition()
        fp.SetPosition(v2(pos.x / MM + dx, pos.y / MM + dy))
    return moved, len(kill)


def reach_of(board, board_path, ends):
    """How many of the ends opened (reachable >= LOCKED_CELLS)."""
    g = raster.Grid(board, step=MR.STEP)
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    nets = sorted({e[0] for e in ends})
    model = Model(board, board_path, g, nets, dev, log=lambda *x: None)
    wave = Wave([MR.LAYER_COST[l] for l in raster.SIGNAL], MR.VIA_COST, dev)
    zeros = torch.zeros((len(raster.SIGNAL), g.h, g.w), device=dev)
    k = int(4.0 / g.step)
    opened = 0
    for (net, x, y, _, _) in ends:
        cost, fv = model.cost_for(net, zeros, zeros, zeros, 0.0, 0.0)
        lab = torch.from_numpy(model.labels[net]).to(dev)
        la = CN.label_at(model.labels[net], g, x, y)
        r0, c0 = g.row(y), g.col(x)
        rr = (max(0, r0 - k), min(g.h - 1, r0 + k))
        cc = (max(0, c0 - k), min(g.w - 1, c0 + k))
        st = torch.nonzero(lab[:, rr[0]:rr[1] + 1, cc[0]:cc[1] + 1] == la, as_tuple=False).T
        dist = wave.reach(cost[:, rr[0]:rr[1] + 1, cc[0]:cc[1] + 1],
                          fv[rr[0]:rr[1] + 1, cc[0]:cc[1] + 1], st, iters=k)
        if int((torch.isfinite(dist) & (dist > 0)).sum()) >= 40:
            opened += 1
    return opened


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--ref', default=None, help='package whose pins to open')
    ap.add_argument('--all', action='store_true', help='all packages with locked pins, one by one')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--ends', default=None, help='json with the list of locked ends (from nudge_campaign)')
    a = ap.parse_args()
    lck = os.path.join(os.path.dirname(os.path.abspath(a.board)),
                       '~' + os.path.basename(a.board) + '.lck')
    if a.apply and os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')
    t = time.time()
    if a.ends and os.path.exists(a.ends):
        import json
        ends = [tuple(e) for e in json.load(open(a.ends))]
    else:
        ends = locked_ends(a.board, D.load(a.drc))
    by_ref = collections.defaultdict(list)
    for e in ends:
        ref = e[4].split()[-1]
        by_ref[ref].append(e)
    print('locked ends %d at packages: %s (%.0f s)' % (
        len(ends), ', '.join('%sx%d' % (r, len(v)) for r, v in
                             sorted(by_ref.items(), key=lambda kv: -len(kv[1]))), time.time() - t))
    refs = [a.ref] if a.ref else ([r for r, _ in sorted(by_ref.items(), key=lambda kv: -len(kv[1]))]
                                  if a.all else [])
    if not refs:
        print('give --ref or --all')
        return 0
    board = pcbnew.LoadBoard(a.board)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
    changed = []
    for ref in refs:
        if ref not in by_ref or ref not in fps:
            print('%s: no locked pins' % ref)
            continue
        ic = fps[ref]
        sides = collections.defaultdict(list)
        for e in by_ref[ref]:
            sides[outward(ic, e[1], e[2])].append(e)
        for u, es in sides.items():
            movers = movers_for(board, ic, es, u)
            if not movers:
                print('%s side %s: locked %d, nothing to move (no small parts nearby)'
                      % (ref, u, len(es)))
                continue
            print('%s side %s: locked %d, parts nearby: %s' % (
                ref, u, len(es), ', '.join(sorted(movers))))
            base_open = reach_of(board, a.board, es)
            best = None
            blk_by_d = {}
            for d in STEPS:
                dx, dy = u[0] * d, u[1] * d
                ok, blk = shift_ok(board, movers, dx, dy, a.board)
                if not ok:
                    print('   shift %.1f: does not fit' % d)
                    continue
                blk_by_d[d] = blk
                # Trial shift: the parts are moved, copper under the old place (stubs) and under the
                # new one (foreign segments/vias) is removed for the evaluation - otherwise reachability
                # was computed with the very copper the shift is meant to remove, and always
                # answered "opened 0".
                saved = {r: (fp.GetPosition().x, fp.GetPosition().y) for r, fp in movers.items()}
                rects_old = [(pad_rect(p), p.GetNetname()) for fp in movers.values() for p in fp.Pads()]
                stubs = []
                for t in board.GetTracks():
                    if t.GetClass() == 'PCB_VIA':
                        continue
                    net_t = t.GetNetname()
                    s_, e_ = t.GetStart(), t.GetEnd()
                    if any(n == net_t and (inside(r, s_.x / MM, s_.y / MM) or inside(r, e_.x / MM, e_.y / MM))
                           for r, n in rects_old):
                        stubs.append(t)
                temp = list(blk) + [t for t in stubs if t not in blk]
                for t in temp:
                    board.Remove(t)
                for fp in movers.values():
                    pos = fp.GetPosition()
                    fp.SetPosition(v2(pos.x / MM + dx, pos.y / MM + dy))
                opened = reach_of(board, a.board, es)
                print('   shift %.1f: opened %d of %d (was %d), foreign items under pads %d, stubs %d'
                      % (d, opened, len(es), base_open, len(blk), len(stubs)))
                if opened > base_open and (best is None or opened > best[1]
                                           or (opened == best[1] and len(blk) < len(blk_by_d[best[0]]))):
                    best = (d, opened)
                for r, (x, y) in saved.items():
                    movers[r].SetPosition(pcbnew.VECTOR2I(x, y))
                for t in temp:
                    board.Add(t)
                if best and opened == len(es):
                    break
            if best:
                d = best[0]
                blk = blk_by_d[d]
                nets_blk = sorted({t.GetNetname().split('/')[-1] for t in blk})
                for t in blk:
                    board.Remove(t)
                n_via, n_kill = do_shift(board, movers, u[0] * d, u[1] * d)
                changed.append((ref, u, d, sorted(movers), best[1], len(es)))
                print('   SELECTED: shift %.1f mm, opened %d/%d; own stubs removed %d, foreign items %d (%s), vias moved %d'
                      % (d, best[1], len(es), n_kill, len(blk), ', '.join(nets_blk)[:80], n_via))
    print('\nshifts: %d' % len(changed))
    for c in changed:
        print('  %s side %s: %.1f mm, %s - opened %d/%d' % (c[0], c[1], c[2], ','.join(c[3]), c[4], c[5]))
    if a.apply and changed:
        shutil.copy2(a.board, a.board + '.bakNudge')
        pcbnew.SaveBoard(a.board, board)
        print('board written, backup .bakNudge')
    elif changed:
        print('this is a plan; add --apply to write it')
    return 0


if __name__ == '__main__':
    sys.exit(main())
