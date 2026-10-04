"""Raster connectivity of a net's copper: which pieces are already connected and which are not.

Why. A maze router that builds starts and goals from ALL copper of a net does not know what is
already connected. That gives degenerate paths (two pieces meeting at one point on different
layers), false "routed" results and starts from the wrong piece. Here the net copper is labelled
into components, and a DRC break becomes a pair "component A -> component B".

How. The net copper raster on every signal layer (true pad shapes, tracks by width, vias by
diameter) is stacked into 2*L-1 planes: even planes are copper layers, odd planes are via
"pillars" between adjacent layers. scipy.ndimage.label runs with a structure of 8 neighbours
in-plane and strictly vertical between planes. So copper on F.Cu does not "connect" to copper
on In1.Cu below it without a via, but does through one. Two pieces of one net at a gap
>= 0.1 mm never merge: a gap longer than the grid step always contains a cell centre.
"""
import math

import os
import numpy as np
import pcbnew
from scipy import ndimage

import raster

MM = 1e6
LAYERS = raster.SIGNAL


def _structure():
    s = np.zeros((3, 3, 3), dtype=bool)
    s[1, :, :] = True          # in-plane: 8 neighbours
    s[0, 1, 1] = s[2, 1, 1] = True   # between planes: vertical only
    return s


def own_stack(board, g, net, basket=None):
    """Stack (2*L-1, h, w): net copper per layer and the pillars of its holes.

    basket: raster.Basket - net copper and holes taken from the basket without a SWIG walk (KAT_PAR)."""
    st = np.zeros((2 * len(LAYERS) - 1, g.h, g.w), dtype=bool)
    has_zone = any(z.GetNetname() == net and not z.GetIsRuleArea() for z in board.Zones())
    # KAT_PLANE_AS_SIGNAL=1: do NOT count the zone fill as own copper. Components of a plane net are then
    # its pads/tracks/vias, and a "pad <-> track" break is routed as a track to the nearest copper of the
    # same net without a new via (what a human would do in the GUI; otherwise the goal is the fill, which
    # needs room for a via pillar that may not exist)
    if os.environ.get('KAT_PLANE_AS_SIGNAL', '0') == '1':
        has_zone = False
    for i, L in enumerate(LAYERS):
        m, _ = raster.copper_mask(board, g, L, nets_only={net}, basket=basket)
        if has_zone:
            # Pieces of a plane net are joined by the FILL: without it a power plane showed ~25x more
            # components than DRC breaks. The fill is own copper (a goal), not an obstacle to others.
            zf, _ = raster.zone_fill_mask(board, g, L, net=net)
            m = m | zf
        st[2 * i] = m
    if basket is not None:
        for (x, y, drill, wl) in basket.vias.get(net, []):
            on = [i for i, L in enumerate(LAYERS) if L in wl]
            if len(on) < 2:
                continue
            col = g.empty()
            raster.fill_disc(g, col, x, y, max(0.1, drill / 2.0))
            for i in range(min(on), max(on)):
                st[2 * i + 1] |= col
        for (x, y, drill, polys) in basket.pads.get(net, []):
            if drill <= 0:
                continue
            col = g.empty()
            raster.fill_disc(g, col, x, y, max(0.1, drill / 2.0))
            for i in range(len(LAYERS) - 1):
                st[2 * i + 1] |= col
        return st
    # pillars: a via of the net (through or blind) joins the planes it pierces
    for t in board.GetTracks():
        if t.GetClass() != 'PCB_VIA' or t.GetNetname() != net:
            continue
        v = t.Cast()
        on = [i for i, L in enumerate(LAYERS) if v.IsOnLayer(board.GetLayerID(L))]
        if len(on) < 2:
            continue
        col = g.empty()
        raster.fill_disc(g, col, v.GetPosition().x / MM, v.GetPosition().y / MM,
                         max(0.1, v.GetDrill() / MM / 2.0))
        for i in range(min(on), max(on)):
            st[2 * i + 1] |= col
    # through-hole (THT) pads also join all layers
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetNetname() != net or p.GetDrillSizeX() <= 0:
                continue
            col = g.empty()
            raster.fill_disc(g, col, p.GetPosition().x / MM, p.GetPosition().y / MM,
                             max(0.1, p.GetDrillSizeX() / MM / 2.0))
            for i in range(len(LAYERS) - 1):
                st[2 * i + 1] |= col
    return st


def components(board, g, net, basket=None):
    """(labels (L,h,w) int32 - component id on copper layers, 0 = no copper; number of components)."""
    st = own_stack(board, g, net, basket=basket)
    lab, n = ndimage.label(st, structure=_structure())
    return lab[0::2], n


def label_at(labels, g, x, y, reach=0.6):
    """Component id at a point (mm): nearest labelled cell within reach on any layer."""
    r0, c0 = g.row(y), g.col(x)
    k = max(1, int(round(reach / g.step)))
    best, bd = 0, 1e9
    for l in range(labels.shape[0]):
        for r in range(max(0, r0 - k), min(g.h, r0 + k + 1)):
            for c in range(max(0, c0 - k), min(g.w, c0 + k + 1)):
                v = labels[l, r, c]
                if v:
                    d = (r - r0) ** 2 + (c - c0) ** 2
                    if d < bd:
                        bd, best = d, int(v)
    return best


if __name__ == '__main__':
    # Self-check: components per net - 1 == DRC breaks.
    import argparse
    import collections
    import os
    import sys
    import time
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import drcinfo as D
    import paths
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.work('gpu', paths.NAME + '.kicad_pcb'))
    ap.add_argument('--drc', default=paths.work('gpu', 'drc.json'))
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    drc = D.load(a.drc)
    br = [b for b in D.signal_breaks(drc) if b[4] and 'Zone' not in b[1]]
    per_net = collections.Counter(b[0] for b in br)
    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=0.1)
    print(g, 'nets with breaks:', len(per_net))
    ok = bad = 0
    t0 = time.time()
    rows = sorted(per_net.items(), key=lambda kv: -kv[1])
    if a.limit:
        rows = rows[:a.limit]
    for net, nb in rows:
        lab, n = components(board, g, net)
        # DRC counts breaks over a minimum spanning tree: components - 1.
        # Plane nets have pieces joined by the fill; signal_breaks leaves them out, so they are not compared
        same_ends = 0
        for b in [x for x in br if x[0] == net]:
            (ax, ay), (bx, by) = b[4]
            la, lb = label_at(lab, g, ax, ay), label_at(lab, g, bx, by)
            if la and lb and la == lb:
                same_ends += 1
        flag = 'ok' if n - 1 == nb and same_ends == 0 else 'MISMATCH'
        if flag == 'ok':
            ok += 1
        else:
            bad += 1
        if flag != 'ok' or nb >= 3:
            print('  %-36s components %2d, breaks %2d, ends in the same component: %d  %s'
                  % (net.split('/')[-1][:36], n, nb, same_ends, flag))
    print('matched %d, mismatched %d of %d nets, %.0f s' % (ok, bad, ok + bad, time.time() - t0))
