#!/usr/bin/env python3
"""Regional rip-up campaign: find locked break ends, remove foreign copper around them and
re-route everything together; DRC judges, round after round, monotone only.

Why separate from gpu_campaign.sh. A normal round does not move foreign copper, so a pin locked
by a ring of neighbours' escapes never opens, however many rounds. Here, for each locked end
(reachable < LOCKED_CELLS cells within 4 mm) ripup.py runs centred on it; only a round with
fewer breaks and zero violations is accepted.

    python3 tools/gpuroute/rip_campaign.py --rounds 20
"""
import argparse
import collections
import math
import os
import re
import shutil
import subprocess
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
LOCKED_CELLS = 40
LOG = paths.work('rip.log')


def say(msg):
    line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def locked_ends(board_path, drc):
    """[(net, x, y, reach, description)] - signal break ends the wave cannot escape from."""
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
    if not nets:
        return []
    board = pcbnew.LoadBoard(board_path)
    g = raster.Grid(board, step=MR.STEP)
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = Model(board, board_path, g, nets, dev, log=lambda *x: None)
    wave = Wave([MR.LAYER_COST[l] for l in raster.SIGNAL], MR.VIA_COST, dev)
    zeros = torch.zeros((len(raster.SIGNAL), g.h, g.w), device=dev)
    k = int(REACH_MM / g.step)
    out = []
    cache = {}
    for net, its in items:
        if net not in cache:
            cache[net] = model.cost_for(net, zeros, zeros, zeros, 0.0, 0.0)
        cost, fv = cache[net]
        lab = torch.from_numpy(model.labels[net]).to(dev)
        for it in its:
            x, y = it['pos']['x'], it['pos']['y']
            la = CN.label_at(model.labels[net], g, x, y)
            r0, c0 = g.row(y), g.col(x)
            rr = (max(0, r0 - k), min(g.h - 1, r0 + k))
            cc = (max(0, c0 - k), min(g.w - 1, c0 + k))
            lab_w = lab[:, rr[0]:rr[1] + 1, cc[0]:cc[1] + 1]
            st = torch.nonzero(lab_w == la, as_tuple=False).T if la else torch.zeros((3, 0), dtype=torch.long)
            dist = wave.reach(cost[:, rr[0]:rr[1] + 1, cc[0]:cc[1] + 1],
                              fv[rr[0]:rr[1] + 1, cc[0]:cc[1] + 1], st, iters=k)
            reach = int((torch.isfinite(dist) & (dist > 0)).sum())
            if reach < LOCKED_CELLS:
                m = re.search(r'^(\S+ \S+) \[.*\] of (\S+)', it['description'])
                out.append((net, x, y, reach, (m.group(1) + ' ' + m.group(2)) if m else '? ? ?'))
    return out


def clusters(ends, radius=3.0):
    """Greedy grouping of locked ends by proximity; larger groups first."""
    left = list(ends)
    out = []
    while left:
        seed = left.pop(0)
        grp = [seed]
        rest = []
        for e in left:
            if math.hypot(e[1] - seed[1], e[2] - seed[2]) <= radius:
                grp.append(e)
            else:
                rest.append(e)
        left = rest
        cx = sum(e[1] for e in grp) / len(grp)
        cy = sum(e[2] for e in grp) / len(grp)
        out.append((cx, cy, grp))
    out.sort(key=lambda c: -len(c[2]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--rounds', type=int, default=20)
    ap.add_argument('--radius', type=float, default=3.5)
    ap.add_argument('--window', type=float, default=3.0)
    ap.add_argument('--iters', type=int, default=8)
    ap.add_argument('--rip-planes', choices=['none', 'tracks', 'all'], default='tracks',
                    help='also rip plane-net tracks (default tracks: without them a rip-up gave e.g. 62 -> 62 with 3 errors, with them 62 -> 52 with 0)')
    a = ap.parse_args()
    lck = os.path.join(os.path.dirname(os.path.abspath(a.board)),
                       '~' + os.path.basename(a.board) + '.lck')
    if os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')
    cur_path = paths.work('rip_cur.json')
    cur = D.run_drc(a.board, cur_path)
    U, E, _ = D.score(cur)
    say('=== rip-up campaign: breaks %d, errors %d, up to %d rounds ===' % (U, E, a.rounds))
    if E:
        say('REFUSED: the board has violations - clean them first')
        return 1
    skipped = []
    rnd = 0
    while rnd < a.rounds and U > 0:
        t = time.time()
        ends = locked_ends(a.board, cur)
        cl = [c for c in clusters(ends)
              if not any(math.hypot(c[0] - sx, c[1] - sy) < 1.5 for sx, sy in skipped)]
        say('locked ends %d, groups %d (skipped %d), search %.0f s'
            % (len(ends), len(cl), len(skipped), time.time() - t))
        if not cl:
            say('no locked ends left or all groups rejected - campaign finished')
            break
        cx, cy, grp = cl[0]
        rnd += 1
        say('--- round %d: group of %d ends at (%.2f, %.2f): %s' % (
            rnd, len(grp), cx, cy,
            ', '.join(sorted({e[4].split()[-1] + '.' + e[4].split()[1] for e in grp}))))
        bak = a.board + '.bakRipRound'
        shutil.copy2(a.board, bak)
        r = subprocess.run([sys.executable, os.path.join(HERE, 'ripup.py'), '--board', a.board,
                            '--drc', cur_path, '--at', '%.3f,%.3f' % (cx, cy),
                            '--radius', str(a.radius), '--window', str(a.window),
                            '--iters', str(a.iters), '--rip-planes', a.rip_planes, '--apply'],
                           capture_output=True, text=True, timeout=1800)
        tail = [l for l in r.stdout.splitlines()
                if l.strip() and 'swig' not in l and 'property.h' not in l][-3:]
        say('  ripup rc=%d: %s' % (r.returncode, ' | '.join(l.strip()[:90] for l in tail)))
        if r.returncode != 0:
            shutil.copy2(bak, a.board)
            skipped.append((cx, cy))
            say('  round %d cancelled (rc=%d)' % (rnd, r.returncode))
            continue
        new_path = paths.work('rip_new.json')
        new = D.run_drc(a.board, new_path)
        U2, E2, _ = D.score(new)
        if E2 == 0 and U2 < U:
            say('  ACCEPTED: %d -> %d (%+d), errors 0' % (U, U2, U2 - U))
            U, cur = U2, new
            shutil.copy2(new_path, cur_path)
            os.remove(bak)
        else:
            shutil.copy2(bak, a.board)
            skipped.append((cx, cy))
            say('  REJECTED: %d -> %d, errors %d - rolled back' % (U, U2, E2))
            shutil.copy2(new_path, paths.work('rip_rej_%02d.json' % rnd))   # report of the rejected round, for analysis
    say('=== rip-up campaign finished: breaks %d ===' % U)
    return 0


if __name__ == '__main__':
    sys.exit(main())
