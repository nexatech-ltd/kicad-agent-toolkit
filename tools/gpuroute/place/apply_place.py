#!/usr/bin/env python3
"""Apply a pocket placement found by the search to the board (one subprocess per pocket).

Steps: (1) move/rotate/flip the movable footprints (`pcbnew` SetPosition/
SetOrientationDegrees/Flip); (2) rip the pocket window copper: all tracks and vias of signal nets
inside the window, plane-net tracks inside the window (their vias stay), stubs that ended on
moved pads, and foreign copper under the new pad positions (never RF copper, see
NEVER_RIP_CLEARANCE); (3) if an L1 path file is given (`--paths`), append it as copper
(`model.emit_fine`); (4) `SaveBoard` with a backup.

Next: re-route the window nets, then place_campaign.py (DRC acceptance / rollback).

    python3 tools/gpuroute/place/apply_place.py --board B --best work/place/U1_U2/best.json --apply
"""
import argparse
import json
import math
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import pcbnew

import paths
import project
import drcinfo as D
import maze_route as MR

MM = 1e6
PLANE = D.PLANE_NETS
PLANE_RIP_MM = 1.0   # = escape.PLANE_RIP_MM: a plane via near the original pad of the same net of a moved part is ripped
# Nets whose class clearance is at least this (RF, controlled impedance) are never ripped;
# must match escape.NEVER_RIP_CLEARANCE so the candidate model and the applied board agree.
NEVER_RIP_CLEARANCE = float(project.get('never_rip_min_clearance', 0.3))


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


def seg_hits_rect(x1, y1, x2, y2, r, g):
    n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.05) + 1)
    return any(inside(r, x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n, g) for i in range(n + 1))


def apply(board, board_path, best, window, paths_blocks=None, log=print):
    """best: {'placement': {ref: [x, y, rot, flipped]}}; window: (x0, y0, x1, y1) mm."""
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
    placement = {r: tuple(v) for r, v in best['placement'].items()}
    moved = {}
    for r, (x, y, rot, fl) in placement.items():
        fp = fps.get(r)
        if fp is None:
            continue
        p = fp.GetPosition()
        if abs(p.x / MM - x) < 1e-4 and abs(p.y / MM - y) < 1e-4 and abs(fp.GetOrientationDegrees() - rot) < 1e-3 \
                and bool(fp.IsFlipped()) == bool(fl):
            continue
        moved[r] = fp
    rects_old = pad_rects(moved.values())
    # A via inside a same-net pad of a pocket IC (exposed-pad thermal vias) is static; same rule
    # as escape.Pocket._in_ic_pad (only the non-moving pocket ICs, best['refs'])
    ic_refs = [r for r in best.get('refs', []) if r in fps and r not in moved]
    rects_all = pad_rects(fps[r] for r in ic_refs)
    x0, y0, x1, y1 = window
    kill = []
    for t in board.GetTracks():
        net = t.GetNetname()
        plane = net.split('/')[-1] in PLANE
        rf = MR.net_class_width(board_path, net)[1] >= NEVER_RIP_CLEARANCE
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition(); x, y = p.x / MM, p.y / MM
            inwin = x0 <= x <= x1 and y0 <= y <= y1
            touch = any(n == net and inside(r, x, y) for r, n in rects_old)
            # plane via next to the original same-net pad of a moved part (decoupling) goes with it
            near = plane and any(n == net and math.hypot((r[0] + r[2]) / 2 - x, (r[1] + r[3]) / 2 - y) <= PLANE_RIP_MM
                                 for r, n in rects_old)
            if rf:
                continue
            # A plane via in the window outside same-net pads (not an exposed pad) is a routing
            # artifact and is ripped with the window copper; the plane connection is restored by
            # the L1 paths (via jobs) and fix_plane_pads
            in_pad = plane and any(n == net and inside(r, x, y, 0.0) for r, n in rects_all)
            if touch or near or (inwin and not plane) or (inwin and plane and not in_pad):
                kill.append(t)
            continue
        s, e = t.GetStart(), t.GetEnd()
        sx, sy, ex, ey = s.x / MM, s.y / MM, e.x / MM, e.y / MM
        inwin = (x0 <= sx <= x1 and y0 <= sy <= y1) and (x0 <= ex <= x1 and y0 <= ey <= y1)
        touch = any(n == net and (inside(r, sx, sy) or inside(r, ex, ey)) for r, n in rects_old)
        if rf:
            continue
        if touch or inwin:
            kill.append(t)
    for t in kill:
        board.Remove(t)
    # move the footprints (same-net vias inside pads would travel with them; they were already ripped as touch)
    for r, fp in moved.items():
        x, y, rot, fl = placement[r]
        if bool(fp.IsFlipped()) != bool(fl):
            # The model mirrors a footprint in X at the same rot; KiCad stores the back side as a
            # mirror in Y, so the same geometry = Flip(top/bottom) + rotation rot + 180 (measured:
            # Flip(pos, False) swaps the pads in X and adds 180 deg; SetOrientation(rot) alone would
            # put them back).
            fp.Flip(fp.GetPosition(), False)
            rot = (rot + 180.0) % 360.0
        fp.SetPosition(v2(x, y))
        fp.SetOrientationDegrees(rot)
    # foreign copper under the new pad positions (any layer), except never-rip (RF) nets
    rects_new = pad_rects(moved.values())
    kill2 = []
    for t in board.GetTracks():
        net = t.GetNetname()
        if MR.net_class_width(board_path, net)[1] >= NEVER_RIP_CLEARANCE:
            continue
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition()
            if any(n != net and inside(r, p.x / MM, p.y / MM, 0.35) for r, n in rects_new):
                kill2.append(t)
            continue
        s, e = t.GetStart(), t.GetEnd()
        if any(n != net and seg_hits_rect(s.x / MM, s.y / MM, e.x / MM, e.y / MM, r, 0.25) for r, n in rects_new):
            kill2.append(t)
    for t in kill2:
        board.Remove(t)
    log('moved footprints %d, ripped window copper %d, under new pads %d' % (len(moved), len(kill), len(kill2)))
    return moved, kill, kill2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--best', required=True, help='best.json from search.py')
    ap.add_argument('--window', default=None, help='window x0,y0,x1,y1 (otherwise taken from best.json)')
    ap.add_argument('--paths', default=None, help='text copper blocks of the L1 paths (emit_fine) to append to the file')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    lck = os.path.join(os.path.dirname(os.path.abspath(a.board)), '~' + os.path.basename(a.board) + '.lck')
    if a.apply and os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')
    best = json.load(open(a.best))
    if a.window:
        window = tuple(float(v) for v in a.window.split(','))
    else:
        window = tuple(best['window'])
    board = pcbnew.LoadBoard(a.board)
    moved, kill, kill2 = apply(board, a.board, best, window)
    if not a.apply:
        print('dry run; add --apply to write')
        return 0
    shutil.copy2(a.board, a.board + '.bakPlace')
    pcbnew.SaveBoard(a.board, board)
    if a.paths and os.path.exists(a.paths):
        blocks = open(a.paths).read().strip()
        if blocks:
            txt = open(a.board).read()
            head = txt.rstrip()
            assert head.endswith(')')
            open(a.board, 'w').write(head[:-1].rstrip('\n') + '\n' + blocks + '\n)\n')
            print('appended L1 path copper blocks')
    print('board written, backup .bakPlace')
    return 0


if __name__ == '__main__':
    sys.exit(main())
