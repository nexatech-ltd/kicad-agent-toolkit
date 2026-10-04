#!/usr/bin/env python3
"""Checkpoint A: GPU distance maps (place/sdf) against the CPU model (model.Model).

Builds a Model for the nets with breaks (as route.py does), then the same maps analytically from
geom.json and the track/via primitives, runs both through model.build_free and compares the masks.
Acceptance criterion: < 0.05 % of cells differ, none of them further than one cell from a copper edge.

    python3 tools/gpuroute/place/check_sdf.py [--board B] [--drc D] [--limit 40]
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

import drcinfo as D
import paths
import raster
import maze_route as MR
import model as M
import sdf
import geom as G


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--geom', default=paths.work('place', 'geom.json'))
    ap.add_argument('--limit', type=int, default=40)
    a = ap.parse_args()
    dev = 'cuda'
    drc = D.load(a.drc)
    breaks = [b for b in D.signal_breaks(drc) if b[4] and 'Zone' not in b[1]
              and b[0].split('/')[-1] not in D.PLANE_NETS]
    breaks.sort(key=lambda b: b[2])
    nets = []
    for b in breaks[:a.limit]:
        if b[0] not in nets:
            nets.append(b[0])
    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=MR.STEP)
    t = time.time()
    model = M.Model(board, a.board, g, nets, dev, log=lambda *x: None)
    print('CPU model: %d nets, %.1f s' % (len(nets), time.time() - t))

    # --- GPU: the same primitive groups
    strict = MR.strict_nets(a.board, board)
    routed = set(nets)
    strict_other = {n: v for n, v in strict.items() if n not in routed}

    def group_of(net):
        if net in routed:
            return -1
        if net in strict_other:
            return int(round(strict_other[net] * 100))
        return 0
    gj = json.load(open(a.geom))
    t = time.time()
    # pads carry no strict (class) clearance (track-to-pad rule applies to all): all foreign pads are group 0
    pads = sdf.prims_from_geom(gj, dev, group_of=lambda n: -1 if n in routed else 0)
    tracks = sdf.prims_from_tracks(board, dev, group_of=group_of, skip_nets=routed)
    keep = pads.t['group'] >= 0
    pads = pads.select(keep)
    keep_t = tracks.t['group'] >= 0
    tracks = tracks.select(keep_t)
    # drill holes as circles of group 999 — from all non-routed pads/vias
    holes = sdf.PrimTable(dev)
    for x, y, r in M.Model._holes(board, routed):
        holes.add(sdf.KIND_CIRCLE, list(range(len(sdf.LAYERS))), x, y, r, r, r, 0.0, 999)
    holes.finalize()
    t_build = time.time() - t
    win = sdf.Window.full(g)
    torch.cuda.synchronize()
    t = time.time()
    dP = sdf.dist_map(pads, win, group=0)
    dT = sdf.dist_map(tracks, win, group=0)
    dB = torch.minimum(dP, dT)
    dS = {}
    for need in sorted(set(strict_other.values())):
        gid = int(round(need * 100))
        dS[need] = sdf.dist_map(tracks, win, group=gid)
    dH = sdf.dist_map(holes, win, group=999)
    torch.cuda.synchronize()
    t_maps = time.time() - t
    print('GPU: pad primitives %d, tracks/vias %d, drills %d; tables %.1f s, maps %.3f s'
          % (len(pads), len(tracks), len(holes), t_build, t_maps))

    groups = set(model.cls.values())
    free_g, fv_g = M.build_free(dP, dT, model.dK, dS, dH, model.inb_for, model.inb_via, model.fine, groups)
    tot = tot_diff = 0
    for key in sorted(groups):
        a_, b_ = model.free[key], free_g[key]
        diff = a_ != b_
        n = int(diff.sum())
        tot += a_.numel(); tot_diff += n
        # mismatches further than one cell from a copper edge: |dB_cpu - dB_gpu| > 0.1 at those cells
        far = int(((model.dB - dB).abs() > 0.1)[diff].sum()) if n else 0
        fvd = int((model.free_via[key] != fv_g[key]).sum())
        print('  group w=%.3f clr=%.2f: cells %d, differ %d (%.4f %%), of them further than a cell %d; free_via differ %d'
              % (key[0], key[1], a_.numel(), n, 100.0 * n / a_.numel(), far, fvd))
    # where the distances disagree most
    dd = (model.dB - dB).abs()
    finite = torch.isfinite(model.dB) & torch.isfinite(dB) & (model.dB < 0.6)
    print('|dB_cpu - dB_gpu| where dB<0.6: mean %.4f, 99%% %.4f, max %.4f mm'
          % (float(dd[finite].mean()), float(torch.quantile(dd[finite][:2000000], 0.99)), float(dd[finite].max())))
    print('TOTAL: %.4f %% of free-mask cells differ' % (100.0 * tot_diff / max(tot, 1)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
