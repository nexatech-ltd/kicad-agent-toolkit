#!/usr/bin/env python3
"""Locked ends of breaks: copper components from which the wave cannot escape at all.

A break with one locked end (every cell around it is clearance of foreign pads) cannot be closed
by any routing: it is a placement defect. The tool lists such ends with footprint and pad so
they are fixed by moving a part, not by the hundredth router round.

    python3 tools/gpuroute/locked.py --board work/gpu/base.kicad_pcb --drc work/gpu/drc.json
"""
import argparse
import collections
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pcbnew
import torch

import drcinfo as D
import paths
import raster
import maze_route as MR
import connectivity as CN
from model import Model
from wave import Wave

REACH_MM = 4.0
LOCKED_CELLS = 40      # fewer reachable cells than this within REACH_MM = locked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.work('gpu', 'base.kicad_pcb'))
    ap.add_argument('--drc', default=paths.work('gpu', 'drc.json'))
    ap.add_argument('--out', default=paths.work('gpu', 'locked.json'))
    a = ap.parse_args()
    drc = D.load(a.drc)
    items = []
    for x in drc['unconnected_items']:
        its = x.get('items', [])
        if len(its) < 2:
            continue
        net = D._net_of(its[0]) or '?'
        if net.split('/')[-1] in D.PLANE_NETS or any(i['description'].startswith('Zone') for i in its):
            continue
        items.append((net, its))
    nets = sorted({n for n, _ in items})
    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=MR.STEP)
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    t = time.time()
    model = Model(board, a.board, g, nets, dev, log=lambda *x: None)
    print('model: %d nets, %.0f s' % (len(nets), time.time() - t))
    wave = Wave([MR.LAYER_COST[l] for l in raster.SIGNAL], MR.VIA_COST, dev)
    zeros = torch.zeros((len(raster.SIGNAL), g.h, g.w), device=dev)
    k = int(REACH_MM / g.step)
    locked = []
    per_ref = collections.Counter()
    cost_cache = {}
    for net, its in items:
        if net not in cost_cache:
            cost_cache[net] = model.cost_for(net, zeros, zeros, zeros, 0.0, 0.0)
        cost, fv = cost_cache[net]
        lab = torch.from_numpy(model.labels[net]).to(dev)
        ends = []
        for it in its:
            x, y = it['pos']['x'], it['pos']['y']
            la = CN.label_at(model.labels[net], g, x, y)
            r0, c0 = g.row(y), g.col(x)
            rr = (max(0, r0 - k), min(g.h - 1, r0 + k))
            cc = (max(0, c0 - k), min(g.w - 1, c0 + k))
            lab_w = lab[:, rr[0]:rr[1] + 1, cc[0]:cc[1] + 1]
            st = torch.nonzero(lab_w == la, as_tuple=False).T if la else torch.zeros((3, 0), dtype=torch.long)
            dist = wave.reach(cost[:, rr[0]:rr[1] + 1, cc[0]:cc[1] + 1], fv[rr[0]:rr[1] + 1, cc[0]:cc[1] + 1], st, iters=k)
            reach = int((torch.isfinite(dist) & (dist > 0)).sum())
            m = re.search(r'^(\S+ \S+) \[.*\] of (\S+)', it['description'])
            what = (m.group(1) + ' ' + m.group(2)) if m else it['description'][:40]
            ends.append((what, x, y, la, reach))
        bad = [e for e in ends if e[4] < LOCKED_CELLS]
        if bad:
            for e in bad:
                ref = e[0].split()[-1]
                per_ref[ref] += 1
            locked.append({'net': net, 'ends': ends})
            print('  %-26s %s' % (net.split('/')[-1][:26],
                                  ' | '.join('%s (%.2f,%.2f) reachable %d%s' % (e[0], e[1], e[2], e[4], ' LOCKED' if e[4] < LOCKED_CELLS else '') for e in ends)))
    print('\nbreaks with a locked end: %d of %d; per footprint: %s'
          % (len(locked), len(items), ', '.join('%sx%d' % kv for kv in per_ref.most_common())))
    with open(a.out, 'w') as f:
        json.dump(locked, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
