#!/usr/bin/env python3
"""GPU router: pick breaks, negotiate board space between nets, write the copper.

    python3 tools/gpuroute/route.py --drc work/cur_drc.json --limit 30
    python3 tools/gpuroute/route.py --drc work/cur_drc.json --limit 30 --apply

Acceptance is the same as for the whole pipeline: KiCad DRC has the last word (gpu_campaign.sh).

Environment:
    KAT_PLANE_AS_SIGNAL=1  plane nets are routed as signals to the nearest own copper (read in connectivity.py)
    KAT_ANY_SIGNAL=1       with --any-target, signal nets also target any own component in the window
    KAT_MULTI_R=<mm>       window radius for --any-target (default 6.0)
"""
import argparse
import collections
import json
import math
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pcbnew
import numpy as np
import torch

import drcinfo as D
import paths
import raster
import maze_route as MR
import connectivity as CN
from model import Model, emit_fine
from negotiate import Job, negotiate
from wave import simplify


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=None)
    ap.add_argument('--limit', type=int, default=20, help='how many breaks to take into the negotiation')
    ap.add_argument('--longest-first', action='store_true')
    ap.add_argument('--min-len', type=float, default=None)
    ap.add_argument('--max-len', type=float, default=None)
    ap.add_argument('--exclude-net', default=None)
    ap.add_argument('--only-net', default=None)
    ap.add_argument('--iters', type=int, default=10)
    ap.add_argument('--pres', type=float, default=0.5)
    ap.add_argument('--growth', type=float, default=1.7)
    ap.add_argument('--hist', type=float, default=0.5)
    ap.add_argument('--plan', default=None, help='json with the paths (for analysis)')
    ap.add_argument('--nopath', default=None,
                    help='file of nets without a path: they are excluded from selection and new '
                         'failures are appended. Otherwise the batch of "shortest" breaks always '
                         'consists of the same locked breaks and the long ones are never reached.')
    ap.add_argument('--any-target', action='store_true',
                    help='for plane nets (with KAT_PLANE_AS_SIGNAL=1): the goal of a break is any other component of the net within KAT_MULTI_R (6 mm), not only the one DRC named')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--planes-all', action='store_true',
                    help='with --planes: also plane zone<->zone slivers (island >= 12 mm2 without vias): path = track + vias to the main plane')
    ap.add_argument('--planes', action='store_true',
                    help='also plane-net breaks with a pad/via (cluster to the plane: the fill is own copper), not mandatory')
    a = ap.parse_args()

    lck = os.path.join(os.path.dirname(os.path.abspath(a.board)),
                       '~' + os.path.basename(a.board) + '.lck')
    if a.apply and os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')

    t_all = time.time()
    drc = D.load(a.drc) if a.drc else D.run_drc(a.board, paths.work('gpu_drc.json'))
    # Plane nets (project.plane_nets(), via drcinfo) are not the router's job: the fill joins their
    # pieces, and pads without a via into their plane are handled by fix_plane_pads.py.
    breaks = [b for b in D.signal_breaks(drc)
              if b[4] and 'Zone' not in b[1] and b[0].split('/')[-1] not in D.PLANE_NETS]
    if a.planes:
        # Fill islands with a pad/via (after a window re-route, tracks on plane layers cut an island
        # around a fresh via): path = track + via into the main polygon; the plane fill is own copper
        # of the net (connectivity.components). Zone+Zone slivers are not handled here.
        planes = [b for b in D.breaks(drc)
                  if b[4] and b[0].split('/')[-1] in D.PLANE_NETS
                  and (a.planes_all or (b[1] != 'Zone+Zone' and ('Pad' in b[1] or 'Via' in b[1] or 'Track' in b[1])))]
        print('plane-net breaks with a pad/via: %d' % len(planes))
        breaks += planes
    breaks.sort(key=lambda b: b[2], reverse=a.longest_first)
    if a.max_len:
        breaks = [b for b in breaks if b[2] <= a.max_len]
    if a.min_len:
        breaks = [b for b in breaks if b[2] >= a.min_len]
    if a.exclude_net and os.path.exists(a.exclude_net):
        bad = {l.strip() for l in open(a.exclude_net) if l.strip()}
        breaks = [b for b in breaks if b[0] not in bad and b[0].split('/')[-1] not in bad]
    if a.nopath and os.path.exists(a.nopath):
        bad = {l.strip() for l in open(a.nopath) if l.strip()}
        n0 = len(breaks)
        breaks = [b for b in breaks if b[0] not in bad and b[0].split('/')[-1] not in bad]
        print('excluded nets without a path (previous rounds): %d breaks' % (n0 - len(breaks)))
    if a.only_net and os.path.exists(a.only_net):
        want = {l.strip() for l in open(a.only_net) if l.strip()}
        breaks = [b for b in breaks if b[0] in want or b[0].split('/')[-1] in want]
    chosen = breaks[:a.limit]
    by_net = collections.OrderedDict()
    for b in chosen:
        by_net.setdefault(b[0], []).append(b)
    print('signal breaks %d, into negotiation %d breaks of %d nets'
          % (len(breaks), len(chosen), len(by_net)))
    if not chosen:
        return 0

    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=MR.STEP)
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    t = time.time()
    if dev == 'cuda':
        torch.zeros(1, device=dev); torch.cuda.synchronize()
        print('cuda init %.1f s' % (time.time() - t))
    t = time.time()
    model = Model(board, a.board, g, list(by_net), dev, log=print)
    print('model built in %.0f s (%s)' % (time.time() - t, dev))

    jobs = []
    for net, brs in by_net.items():
        lab = model.labels[net]
        pairs = []
        for (n, kinds, dist, refs, pos) in brs:
            (ax, ay), (bx, by) = pos
            la, lb = CN.label_at(lab, g, ax, ay), CN.label_at(lab, g, bx, by)
            short = net.split('/')[-1][:24]
            if not la or not lb:
                print('  %-24s break end outside the net copper - skipped' % short)
                continue
            # --any-target: the goal is any other component of the own net within R (always for planes; for signals
            # with KAT_ANY_SIGNAL=1 - so an end goes to a pre-placed via or a nearby piece instead of a 50 mm detour)
            if a.any_target and (net.split('/')[-1] in D.PLANE_NETS or os.environ.get('KAT_ANY_SIGNAL', '0') == '1'):
                R = float(os.environ.get('KAT_MULTI_R', '6.0'))
                r0, r1 = max(0, g.row(ay - R)), min(g.h - 1, g.row(ay + R))
                c0, c1 = max(0, g.col(ax - R)), min(g.w - 1, g.col(ax + R))
                others = [int(k) for k in np.unique(np.asarray(lab)[:, r0:r1 + 1, c0:c1 + 1]).tolist() if k and k != la]
                if others:
                    print('  %-24s goal: any of %d components within %.0f mm' % (short, len(others), R))
                    pairs.append((la, tuple(others), dist, (pos[0], pos[0])))
                    continue
            if la == lb:
                print('  %-24s ends already in one raster component - skipped' % short)
                continue
            pairs.append((la, lb, dist, pos))
        if pairs:
            jobs.append(Job(net, pairs))
    print('nets in negotiation %d, breaks %d' % (len(jobs), sum(len(j.breaks) for j in jobs)))
    if not jobs:
        return 0

    ok, conflicted = negotiate(model, jobs, iters=a.iters, pres0=a.pres, growth=a.growth,
                               hist_w=a.hist, log=print)
    bad = {j.net for j in conflicted}
    if not ok:
        print('negotiation did not converge: excluding %d conflicting nets: %s'
              % (len(bad), ', '.join(sorted(n.split('/')[-1] for n in bad))))

    if a.nopath:
        dead = [j.net for j in jobs if not j.paths and j.fail]
        if dead:
            with open(a.nopath, 'a') as f:
                f.write(''.join(n + '\n' for n in dead))
            print('nets without a path written to %s: %d' % (a.nopath, len(dead)))
    blocks, done, plan = [], 0, {}
    for j in jobs:
        if j.net in bad or not j.paths:
            continue
        w, clr = model.cls[j.net]
        for p in j.paths:
            if len(p) < 2:
                continue
            blocks += emit_fine(g, j.net, w, p, model.fine)
            done += 1
        plan[j.net] = [[list(c) for c in p] for p in j.paths]
    print('\nto write: breaks %d of %d, copper blocks %d; total %.0f s'
          % (done, sum(len(j.breaks) for j in jobs), len(blocks), time.time() - t_all))
    if a.plan:
        with open(a.plan, 'w') as f:
            json.dump(plan, f)
    if not a.apply or not blocks:
        if blocks:
            print('this is a plan; add --apply to write it')
        return 0
    txt = open(a.board).read()
    shutil.copy2(a.board, a.board + '.bakGpu')
    head = txt.rstrip()
    assert head.endswith(')')
    head = head[:-1].rstrip('\n')
    open(a.board, 'w').write(head + '\n' + '\n'.join(blocks) + '\n)\n')
    print('copper blocks written: %d, backup .bakGpu' % len(blocks))
    return 0


if __name__ == '__main__':
    sys.exit(main())
