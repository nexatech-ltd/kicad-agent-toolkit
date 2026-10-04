#!/usr/bin/env python3
"""Checkpoint C: L1 (negotiated routing of the pocket nets to the window ring) at the current placement.

    python3 tools/gpuroute/place/check_l1.py --refs U1 [--iters 4] [--verbose]
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import pcbnew
import torch

import paths
import raster
import escape as E
import fitness as FT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--geom', default=paths.work('place', 'geom.json'))
    ap.add_argument('--refs', required=True, help='comma-separated ICs of the pocket, e.g. U1,U2')
    ap.add_argument('--iters', type=int, default=4)
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--window', default=None, help='manual window x0,y0,x1,y1 (mm)')
    ap.add_argument('--write', default=None, help='write the L1 paths as .kicad_pcb copper blocks (for apply_place --paths)')
    ap.add_argument('--best', default=None, help='write best.json of the base placement (for apply_place without moves)')
    ap.add_argument('--placement', default=None, help='evaluate the placement from a best.json (otherwise the base one)')
    a = ap.parse_args()
    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=0.1)
    geom = json.load(open(a.geom))
    bbox = tuple(float(v) for v in a.window.split(',')) if a.window else None
    pk = E.Pocket(board, a.board, g, geom, a.refs.split(','), bbox=bbox)
    print('nets leaving the window: %d of %d' % (len(pk.outside_nets), len(pk.nets)))
    st = {}
    if a.placement:
        st = {k: tuple(v) for k, v in json.load(open(a.placement))['placement'].items() if k in pk.base_placement}
        moved = [k for k, v in st.items() if v != pk.base_placement[k]]
        print('placement %s: moved footprints %d %s' % (a.placement, len(moved), moved[:10]))
    t = time.time()
    cm, pads = pk.model_for(st)
    n0, locked, tight = FT.l0_score(pk, cm, st)
    torch.cuda.synchronize()
    print('L0: locked %d, tightness %.1f in %.2f s' % (n0, tight, time.time() - t))
    t = time.time()
    res = FT.l1_score(pk, cm, st, iters=a.iters, log=print if a.verbose else (lambda *x: None))
    torch.cuda.synchronize()
    # place_campaign.py picks up the line starting with 'L1:' — keep the prefix
    print('L1: nets %d, breaks %d, unrouted %d; ring escapes %d, failed %d; '
          'plane via jobs %d, failed %d; length %.1f mm, vias %d; %.1f s'
          % (res['nets'], res['breaks'], res['unrouted'], res['escapes'], res['escapes_failed'],
             res['via_breaks'], res['via_failed'], res['length_mm'], res['vias'], time.time() - t))
    print('  unrouted (net, failures, breaks):', res['failed'])
    # self-check: path footprints of different nets do not overlap in the model (bare_i & dil_j)
    fps_ = {}
    for net, ps in res['paths'].items():
        bare = dil = None
        for path in ps:
            b, d_ = cm.footprint(path, net)
            bare = b if bare is None else bare | b
            dil = d_ if dil is None else dil | d_
        fps_[net] = (bare, dil)
    nets_ = list(fps_)
    clashes = []
    for i in range(len(nets_)):
        for j in range(len(nets_)):
            if i != j and bool((fps_[nets_[i]][0] & fps_[nets_[j]][1]).any()):
                clashes.append((nets_[i].split('/')[-1], nets_[j].split('/')[-1]))
    # place_campaign.py also picks up this line by its 'L1 path clashes' prefix
    print('L1 path clashes in the model: %d %s' % (len(clashes), clashes[:8]))
    if a.write:
        import model as M
        fine_full = M.Model._fine(board, g)
        blocks = []
        for net, ps in res['paths'].items():
            w = cm.cls[net][0]
            for path in ps:
                full = [(l, r + pk.win.r0, c + pk.win.c0) for (l, r, c) in path]
                blocks += M.emit_fine(g, net, w, full, fine_full)
        os.makedirs(os.path.dirname(os.path.abspath(a.write)), exist_ok=True)
        open(a.write, 'w').write('\n'.join(blocks) + '\n')
        print('wrote L1 path copper blocks: %d -> %s' % (len(blocks), a.write))
    if a.best:
        json.dump({'F': None, 'placement': {k: list(v) for k, v in pk.base_placement.items()}, 'info': {'locked': n0},
                   'refs': a.refs.split(','), 'window': list(pk.bbox)}, open(a.best, 'w'), ensure_ascii=False, indent=1)
        print('wrote best.json of the base placement -> %s' % a.best)
    F = FT.total(pk, cm, st, n0, res, tight=tight)
    print('F = %.1f (plane pads without room for a via: %d)' % (F, FT.plane_pads_without_via(pk, cm, st)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
