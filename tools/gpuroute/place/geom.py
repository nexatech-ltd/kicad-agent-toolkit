#!/usr/bin/env python3
"""Footprint geometry in LOCAL coordinates — a one-off export from pcbnew.

Why. To score placement candidates on the GPU without pcbnew, the pads of every footprint are
needed as primitives (rounded rectangle, circle, capsule, polygon) in the footprint's own
coordinate system: a candidate is then (x, y, rotation, side) per footprint and the raster is
computed analytically.

The only truth about a pad's shape is the polygon from `TransformShapeToPolySet` (lesson learned:
`(size)` of custom-shape pads can be wrong, and `pad.GetOrientation()` of rotated footprints is
unreliable). So the primitive parameters are fitted to the polygon transformed into local
coordinates and then verified on a 0.025 mm raster against the same polygon: more than 8
differing sub-cells -> the primitive is declared POLY and stores the polygon itself.

Rotation convention (KiCad, y axis down):
    board = pos + R(theta)*local,  where  bx = px + lx*cos(theta) + ly*sin(theta),  by = py - lx*sin(theta) + ly*cos(theta).

Footprints the search never moves are configured with `place_fixed_refs` (list of refs) and
`place_fixed_prefixes` (ref prefixes, default ["H", "ANT"]: mounting holes, antennas) in
kicad-agent.json; connectors ("J*") near the board edge are fixed automatically.

    python3 tools/gpuroute/place/geom.py [--board B] [--out work/place/geom.json]
"""
import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # gpuroute
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # tools

import numpy as np
import pcbnew

import paths
import project
import raster

MM = 1e6
LAYERS = raster.SIGNAL                     # signal copper layers, F.Cu ... B.Cu
KINDS = {'RRECT': 0, 'CIRCLE': 1, 'CAPSULE': 2, 'POLY': 3}
FINE = 0.025
CHECK_CELLS = 8

# Footprints the search does not move (typically edge connectors, antennas, mounting holes,
# an RF module with its matching network) — from the project config.
FIXED_PREFIX = tuple(project.get('place_fixed_prefixes', ['H', 'ANT']))
FIXED_REFS = set(project.get('place_fixed_refs', []))
EDGE_CONNECTOR_MM = 6.0                    # a connector is an edge connector if its centre is closer than this to the outline


def to_local(bx, by, px, py, th):
    """Board point -> local footprint coordinates (inverse of to_board)."""
    c, s = math.cos(th), math.sin(th)
    dx, dy = bx - px, by - py
    return dx * c - dy * s, dx * s + dy * c


def to_board(lx, ly, px, py, th):
    c, s = math.cos(th), math.sin(th)
    return px + lx * c + ly * s, py - lx * s + ly * c


def poly_area(pts):
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


def sdf_rrect(px, py, hw, hh, r):
    """Signed distance to a rounded rectangle (centred at 0, axis-aligned)."""
    qx = np.abs(px) - (hw - r)
    qy = np.abs(py) - (hh - r)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


def sdf_capsule(px, py, hw, hh):
    r = min(hw, hh)
    if hw >= hh:
        L = hw - r
        cx = np.clip(px, -L, L)
        return np.hypot(px - cx, py) - r
    L = hh - r
    cy = np.clip(py, -L, L)
    return np.hypot(px, py - cy) - r


def point_in_poly(px, py, pts):
    """Even-odd ray test for arrays of points."""
    inside = np.zeros_like(px, dtype=bool)
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cond = ((y1 <= py) & (py < y2)) | ((y2 <= py) & (py < y1))
        with np.errstate(divide='ignore', invalid='ignore'):
            xi = x1 + (py - y1) * (x2 - x1) / (y2 - y1) if y1 != y2 else np.full_like(px, np.inf)
        inside ^= cond & (px < xi)
    return inside


def fit_prim(shape_name, size_x, size_y, rr, local_pts):
    """Primitive parameters from the local polygon; 0/90 orientation is taken from the extent."""
    xs = [p[0] for p in local_pts]
    ys = [p[1] for p in local_pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    hw, hh = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
    if shape_name == 'CIRCLE':
        return dict(kind='CIRCLE', cx=cx, cy=cy, hw=(hw + hh) / 2, hh=(hw + hh) / 2, r=(hw + hh) / 2)
    if shape_name == 'OVAL':
        return dict(kind='CAPSULE', cx=cx, cy=cy, hw=hw, hh=hh, r=min(hw, hh))
    if shape_name in ('RECT', 'RECTANGLE', 'ROUNDRECT'):
        r = 0.0 if shape_name in ('RECT', 'RECTANGLE') else min(rr, hw, hh)
        return dict(kind='RRECT', cx=cx, cy=cy, hw=hw, hh=hh, r=r)
    return dict(kind='POLY', cx=cx, cy=cy, hw=hw, hh=hh, r=0.0)


def check_prim(prim, local_pts):
    """Number of 0.025 mm sub-cells where the primitive and the polygon rasters disagree."""
    xs = [p[0] for p in local_pts]
    ys = [p[1] for p in local_pts]
    # Sub-cell centres are offset by half a cell plus an irrational fraction: rectangle edges lie
    # exactly on multiples of 0.025, and without the offset a quarter of the border cells
    # "disagreed" because of ties (55 cells on a plain 1206 with correct geometry).
    off = FINE * 0.5 + FINE * 0.137
    x0, x1 = min(xs) - 2 * FINE + off, max(xs) + 2 * FINE
    y0, y1 = min(ys) - 2 * FINE + off, max(ys) + 2 * FINE
    gx = np.arange(x0, x1, FINE)
    gy = np.arange(y0, y1, FINE)
    px, py = np.meshgrid(gx, gy)
    inside_poly = point_in_poly(px, py, local_pts)
    dx, dy = px - prim['cx'], py - prim['cy']
    if prim['kind'] == 'RRECT':
        d = sdf_rrect(dx, dy, prim['hw'], prim['hh'], prim['r'])
    elif prim['kind'] == 'CIRCLE':
        d = np.hypot(dx, dy) - prim['r']
    elif prim['kind'] == 'CAPSULE':
        d = sdf_capsule(dx, dy, prim['hw'], prim['hh'])
    else:
        return 0
    # only clear disagreements count: a cell deeper than half a cell inside one and outside the other
    diff = (inside_poly & (d > FINE * 0.5)) | (~inside_poly & (d < -FINE * 0.5))
    return int(diff.sum())


def pad_layers(board, pad):
    lids = set(pad.GetLayerSet().Seq())
    on = [L for L in LAYERS if board.GetLayerID(L) in lids]
    if len(on) >= 2 and 'F.Cu' in on and 'B.Cu' in on:
        return 'THT'
    if on == ['F.Cu']:
        return 'F'
    if on == ['B.Cu']:
        return 'B'
    return ','.join(on) if on else ''


SHAPE_NAMES = None


def shape_name(pad, lid):
    global SHAPE_NAMES
    if SHAPE_NAMES is None:
        SHAPE_NAMES = {getattr(pcbnew, n): n.replace('PAD_SHAPE_', '') for n in dir(pcbnew) if n.startswith('PAD_SHAPE_')}
    try:
        s = pad.GetShape(lid)
    except TypeError:
        s = pad.GetShape()
    return SHAPE_NAMES.get(s, str(s))


def mobility(board, fp, bb_board):
    """fixed | ic | small — mobility class of a footprint."""
    ref = fp.GetReference()
    if ref in FIXED_REFS or (FIXED_PREFIX and ref.startswith(FIXED_PREFIX)):
        return 'fixed'
    x, y = fp.GetPosition().x / MM, fp.GetPosition().y / MM
    if ref.startswith('J'):
        near_edge = min(x - bb_board[0], bb_board[2] - x, y - bb_board[1], bb_board[3] - y) < EDGE_CONNECTOR_MM
        return 'fixed' if near_edge else 'ic'
    cy = fp.GetCourtyard(pcbnew.F_CrtYd)
    if cy.OutlineCount() == 0:
        cy = fp.GetCourtyard(pcbnew.B_CrtYd)
    if cy.OutlineCount():
        bb = cy.BBox()
        side = max(bb.GetWidth(), bb.GetHeight()) / MM
    else:
        bb = fp.GetBoundingBox(False, False)
        side = max(bb.GetWidth(), bb.GetHeight()) / MM
    return 'small' if side <= 4.5 else 'ic'


def extract(board, log=print):
    bbb = board.GetBoardEdgesBoundingBox()
    bb_board = (bbb.GetLeft() / MM, bbb.GetTop() / MM, bbb.GetRight() / MM, bbb.GetBottom() / MM)
    out = {'board_bbox': bb_board, 'layers': LAYERS, 'footprints': [], 'rule_areas': [], 'stats': {}}
    n_poly = n_pads = 0
    for fp in board.GetFootprints():
        px, py = fp.GetPosition().x / MM, fp.GetPosition().y / MM
        th = math.radians(fp.GetOrientationDegrees())
        cyard = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
        cbb = None
        if cyard.OutlineCount():
            b = cyard.BBox()
            corners = [(b.GetLeft() / MM, b.GetTop() / MM), (b.GetRight() / MM, b.GetTop() / MM),
                       (b.GetRight() / MM, b.GetBottom() / MM), (b.GetLeft() / MM, b.GetBottom() / MM)]
            loc = [to_local(x, y, px, py, th) for x, y in corners]
            cbb = (min(p[0] for p in loc), min(p[1] for p in loc), max(p[0] for p in loc), max(p[1] for p in loc))
        rec = {'ref': fp.GetReference(), 'value': str(fp.GetValue()), 'x': px, 'y': py,
               'rot': fp.GetOrientationDegrees(), 'flipped': bool(fp.IsFlipped()),
               'courtyard': cbb, 'mobility': mobility(board, fp, bb_board), 'pads': []}
        for pad in fp.Pads():
            lay = pad_layers(board, pad)
            if not lay:
                continue                       # paste/mask only (e.g. a segmented QFN exposed pad)
            lid = board.GetLayerID('B.Cu' if lay == 'B' else 'F.Cu')
            polys = raster.pad_polys(pad, lid)
            if not polys:
                continue
            pts = max(polys, key=poly_area)
            local = [to_local(x, y, px, py, th) for x, y in pts]
            try:
                sz = pad.GetSize(lid)
            except TypeError:
                sz = pad.GetSize()
            try:
                rr = pad.GetRoundRectCornerRadius(lid) / MM
            except Exception:
                rr = 0.0
            sname = shape_name(pad, lid)
            prim = fit_prim(sname, sz.x / MM, sz.y / MM, rr, local)
            bad = check_prim(prim, local) if prim['kind'] != 'POLY' else 0
            if bad > CHECK_CELLS:
                prim = fit_prim('CUSTOM', 0, 0, 0, local)
            if prim['kind'] == 'POLY':
                prim['poly'] = [(round(x, 4), round(y, 4)) for x, y in local]
                n_poly += 1
            prim.update(num=pad.GetNumber(), net=pad.GetNetname(), layers=lay,
                        drill=pad.GetDrillSizeX() / MM, shape=sname, check=bad)
            for k in ('cx', 'cy', 'hw', 'hh', 'r', 'drill'):
                prim[k] = round(float(prim[k]), 4)
            rec['pads'].append(prim)
            n_pads += 1
        out['footprints'].append(rec)
    for z in board.Zones():
        if z.GetIsRuleArea():
            zb = z.GetBoundingBox()
            out['rule_areas'].append({'name': z.GetZoneName(), 'bbox': (zb.GetX() / MM, zb.GetY() / MM,
                                      (zb.GetX() + zb.GetWidth()) / MM, (zb.GetY() + zb.GetHeight()) / MM),
                                      'footprints_allowed': not z.GetDoNotAllowFootprints() if hasattr(z, 'GetDoNotAllowFootprints') else True})
    out['stats'] = {'footprints': len(out['footprints']), 'pads': n_pads, 'poly_pads': n_poly}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--out', default=paths.work('place', 'geom.json'))
    a = ap.parse_args()
    t = time.time()
    board = pcbnew.LoadBoard(a.board)
    g = extract(board)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w') as f:
        json.dump(g, f, ensure_ascii=False)
    st = g['stats']
    kinds = {}
    worst = []
    mob = {}
    for fp in g['footprints']:
        mob[fp['mobility']] = mob.get(fp['mobility'], 0) + 1
        for p in fp['pads']:
            kinds[p['kind']] = kinds.get(p['kind'], 0) + 1
            if p['check'] > 0:
                worst.append((p['check'], fp['ref'], p['num'], p['shape']))
    worst.sort(reverse=True)
    print('footprints %d (%s), pads %d, by kind %s, POLY %d; %.1f s' % (
        st['footprints'], mob, st['pads'], kinds, st['poly_pads'], time.time() - t))
    print('worst primitive/polygon disagreements (sub-cells): %s' % worst[:8])
    print('wrote %s' % a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
