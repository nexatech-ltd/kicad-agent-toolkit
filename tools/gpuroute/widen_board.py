#!/usr/bin/env python3
"""Widen the board: everything right of X0 moves right by dx - footprints (with their zones),
tracks, vias, zone vertices, outline graphics and silkscreen. Segments crossing X0 are stretched
(only the right end moves): connectivity is kept, and DRC checks the clearances.

Why: a dense block (e.g. a row of motor drivers) that does not autoroute - there is no room
between the ICs even to shift capacitors, and other parts are packed beside it. When the
project allows a larger board, this is the cheapest way to get the space.

    python3 tools/gpuroute/widen_board.py --board B --x0 137.5 --dx 10 [--apply]
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pcbnew

import paths

MM = 1e6


def sh(v, dx_nm, x0_nm):
    return pcbnew.VECTOR2I(v.x + dx_nm, v.y) if v.x > x0_nm else pcbnew.VECTOR2I(v.x, v.y)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--x0', type=float, required=True)
    ap.add_argument('--dx', type=float, required=True)
    ap.add_argument('--rip-crossing', action='store_true',
                    help='remove segments crossing X0 (the router re-routes them across the new gap) instead of '
                         'stretching them: stretching ~90 segments gave 24 shorts and 9 track crossings')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    x0, dx = int(round(a.x0 * MM)), int(round(a.dx * MM))
    board = pcbnew.LoadBoard(a.board)
    n_fp = n_seg = n_stretch = n_via = n_zone_pts = n_gr = n_txt = 0
    for fp in board.GetFootprints():
        p = fp.GetPosition()
        if p.x > x0:
            fp.SetPosition(pcbnew.VECTOR2I(p.x + dx, p.y))
            n_fp += 1
    kill = []
    for t in board.GetTracks():
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition()
            if p.x > x0:
                t.SetPosition(pcbnew.VECTOR2I(p.x + dx, p.y)); n_via += 1
            continue
        s, e = t.GetStart(), t.GetEnd()
        rs, re_ = s.x > x0, e.x > x0
        if rs and re_:
            t.SetStart(sh(s, dx, x0)); t.SetEnd(sh(e, dx, x0)); n_seg += 1
        elif rs or re_:
            if a.rip_crossing:
                kill.append(t)
            else:
                t.SetStart(sh(s, dx, x0)); t.SetEnd(sh(e, dx, x0))
            n_stretch += 1
    for t in kill:
        board.Remove(t)
    for z in board.Zones():
        poly = z.Outline()
        for i in range(poly.OutlineCount()):
            o = poly.Outline(i)
            for j in range(o.PointCount()):
                pt = o.CPoint(j)
                if pt.x > x0:
                    o.SetPoint(j, pcbnew.VECTOR2I(pt.x + dx, pt.y)); n_zone_pts += 1
            for h in range(poly.HoleCount(i)):
                hl = poly.Hole(i, h)
                for j in range(hl.PointCount()):
                    pt = hl.CPoint(j)
                    if pt.x > x0:
                        hl.SetPoint(j, pcbnew.VECTOR2I(pt.x + dx, pt.y)); n_zone_pts += 1
        z.UnFill()
    for d in board.GetDrawings():
        cls = d.GetClass()
        if cls == 'PCB_SHAPE':
            shp = d.GetShape()
            if shp in (pcbnew.SHAPE_T_SEGMENT, pcbnew.SHAPE_T_RECT):
                d.SetStart(sh(d.GetStart(), dx, x0)); d.SetEnd(sh(d.GetEnd(), dx, x0)); n_gr += 1
            elif shp == pcbnew.SHAPE_T_ARC:
                c = d.GetCenter()
                if c.x > x0:
                    d.SetStart(sh(d.GetStart(), dx, x0)); d.SetEnd(sh(d.GetEnd(), dx, x0))
                    d.SetCenter(pcbnew.VECTOR2I(c.x + dx, c.y)); n_gr += 1
            elif shp == pcbnew.SHAPE_T_CIRCLE:
                c = d.GetCenter()
                if c.x > x0:
                    d.SetCenter(pcbnew.VECTOR2I(c.x + dx, c.y)); d.SetEnd(sh(d.GetEnd(), dx, x0)); n_gr += 1
            elif shp == pcbnew.SHAPE_T_POLY:
                ps = d.GetPolyShape()
                for i in range(ps.OutlineCount()):
                    o = ps.Outline(i)
                    for j in range(o.PointCount()):
                        pt = o.CPoint(j)
                        if pt.x > x0:
                            o.SetPoint(j, pcbnew.VECTOR2I(pt.x + dx, pt.y))
                n_gr += 1
        elif cls in ('PCB_TEXT', 'PCB_TEXTBOX', 'PCB_DIMENSION_T', 'PCB_DIM_ALIGNED', 'PCB_DIM_ORTHOGONAL', 'PCB_DIM_LEADER', 'PCB_DIM_CENTER', 'PCB_DIM_RADIAL'):
            p = d.GetPosition()
            if p.x > x0:
                d.SetPosition(pcbnew.VECTOR2I(p.x + dx, p.y)); n_txt += 1
    bb = board.GetBoardEdgesBoundingBox()
    print('shift x > %.1f by %.1f mm: footprints %d, segments %d (crossing %d%s), vias %d, '
          'zone vertices %d, graphics %d, texts %d; outline now %.1f x %.1f mm'
          % (a.x0, a.dx, n_fp, n_seg, n_stretch, ' - removed' if a.rip_crossing else ' - stretched', n_via, n_zone_pts, n_gr, n_txt,
             bb.GetWidth() / MM, bb.GetHeight() / MM))
    if not a.apply:
        print('this is a plan; add --apply to write it')
        return 0
    shutil.copy2(a.board, a.board + '.bakWiden')
    pcbnew.SaveBoard(a.board, board)
    print('board written, backup .bakWiden')
    return 0


if __name__ == '__main__':
    sys.exit(main())
