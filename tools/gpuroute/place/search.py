#!/usr/bin/env python3
"""Simulated annealing over a pocket: moves of support parts and ICs, L0 scored on the GPU, L1 for the best.

    python3 tools/gpuroute/place/search.py --refs U1,U2 --seconds 600
    python3 tools/gpuroute/place/search.py --refs U1 --eval work/place/U1/best.json

State — {ref: (x, y, rot, flipped)} of the movable footprints of the window. Moves (probabilities):
shift a small part (0.45), rotate 90 deg (0.10), swap two identical parts (0.10), mirror to B.Cu
(0.05), shift the row on one side (0.15), shift an IC with its support parts (0.10), spread a row
of ICs (0.05). Hard constraints — constraints.Constraints.check before scoring.
Best result — work/place/<pocket>/best.json (resumable), log in the same directory.
"""
import argparse
import json
import math
import os
import random
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
from constraints import Constraints

SHIFTS = [0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5]
DIRS8 = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


def say(logf, msg):
    line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    logf.write(line + '\n'); logf.flush()


def propose(state, pocket, rng, allow_flip=True, allow_ic=True):
    """One random move -> new state (a copy) and a description."""
    st = dict(state)
    small = [r for r in pocket.movable if pocket.fps[r]['mobility'] == 'small']
    ics = [r for r in pocket.movable if pocket.fps[r]['mobility'] == 'ic']
    u = rng.random()
    if u < 0.45 or not small and not ics:
        r = rng.choice(small or list(st))
        d = rng.choice(SHIFTS); dx, dy = rng.choice(DIRS8)
        x, y, rot, fl = st[r]
        st[r] = (round(x + dx * d, 3), round(y + dy * d, 3), rot, fl)
        return st, 'shift %s %.1f' % (r, d)
    if u < 0.55 and small:
        r = rng.choice(small)
        x, y, rot, fl = st[r]
        st[r] = (x, y, (rot + 90) % 360, fl)
        return st, 'rotate %s' % r
    if u < 0.65 and len(small) >= 2:
        a, b = rng.sample(small, 2)
        fa, fb = pocket.fps[a], pocket.fps[b]
        if fa['courtyard'] and fb['courtyard'] and abs((fa['courtyard'][2] - fa['courtyard'][0]) - (fb['courtyard'][2] - fb['courtyard'][0])) < 0.05:
            st[a], st[b] = (st[b][0], st[b][1], st[a][2], st[a][3]), (st[a][0], st[a][1], st[b][2], st[b][3])
            return st, 'swap %s<->%s' % (a, b)
    if u < 0.70 and allow_flip and small:
        r = rng.choice(small)
        x, y, rot, fl = st[r]
        st[r] = (x, y, rot, not fl)
        return st, 'side %s -> %s' % (r, 'B' if not fl else 'F')
    if u < 0.85 and small:
        # row shift: all small parts on one side (by the sign of the offset from the pocket IC centre) together
        ic = rng.choice(pocket.ic_refs)
        fp = pocket.fps[ic]
        dx, dy = rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
        d = rng.choice(SHIFTS[:5])
        moved = []
        for r in small:
            x, y, rot, fl = st[r]
            side = (x - fp['x']) * dx + (y - fp['y']) * dy
            if side > 0 and math.hypot(x - fp['x'], y - fp['y']) < 5.0:
                st[r] = (round(x + dx * d, 3), round(y + dy * d, 3), rot, fl)
                moved.append(r)
        return st, 'row at %s %s %.1f (%d parts)' % (ic, (dx, dy), d, len(moved))
    if allow_ic and ics:
        r = rng.choice(ics)
        d = rng.choice(SHIFTS[:6]); dx, dy = rng.choice(DIRS8)
        x, y, rot, fl = st[r]
        st[r] = (round(x + dx * d, 3), round(y + dy * d, 3), rot, fl)
        # support parts travel with the IC
        for s in small:
            sx, sy, srot, sfl = st[s]
            if math.hypot(sx - x, sy - y) < 4.5:
                st[s] = (round(sx + dx * d, 3), round(sy + dy * d, 3), srot, sfl)
        return st, 'IC %s %.1f' % (r, d)
    r = rng.choice(list(st))
    x, y, rot, fl = st[r]
    st[r] = (round(x + 0.1, 3), y, rot, fl)
    return st, 'shift %s 0.1' % r


def evaluate(pocket, cons, state, l1=False, log=lambda *a: None):
    ok, why = cons.check(state)
    if not ok:
        return None, why, None
    cm, pads = pocket.model_for(state)
    n_locked, locked, tight = FT.l0_score(pocket, cm, state)
    l1res = FT.l1_score(pocket, cm, state, log=log) if l1 else None
    F = FT.total(pocket, cm, state, n_locked, l1res, tight=tight)
    return F, dict(locked=n_locked, tight=round(tight, 1), l1=l1res), cm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--geom', default=paths.work('place', 'geom.json'))
    ap.add_argument('--refs', required=True, help='comma-separated ICs of the pocket, e.g. U1,U2')
    ap.add_argument('--seconds', type=float, default=600)
    ap.add_argument('--chains', type=int, default=4)
    ap.add_argument('--l1-every', type=int, default=100)
    ap.add_argument('--movable-ics', action='store_true', help='also move the pocket ICs')
    ap.add_argument('--no-flip', action='store_true')
    ap.add_argument('--l1-always', action='store_true',
                    help='L1 at every step (pockets with L0 = 0 and zero tightness: limited by corridors, not locked pads)')
    ap.add_argument('--eval', default=None, help='only evaluate the placement from a json')
    ap.add_argument('--seed', type=int, default=1)
    a = ap.parse_args()
    refs = a.refs.split(',')
    out_dir = paths.work('place', '_'.join(refs))
    os.makedirs(out_dir, exist_ok=True)
    logf = open(os.path.join(out_dir, 'search.log'), 'a')
    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=0.1)
    geom = json.load(open(a.geom))
    kinds = ('small', 'ic') if a.movable_ics else ('small',)
    pocket = E.Pocket(board, a.board, g, geom, refs, movable_kinds=kinds, log=lambda m: say(logf, m))
    cons = Constraints(geom, pocket, geom['board_bbox'], geom['rule_areas'])
    base = dict(pocket.base_placement)
    if a.eval:
        st = {k: tuple(v) for k, v in json.load(open(a.eval))['placement'].items()}
        F, info, cm = evaluate(pocket, cons, st, l1=True, log=lambda m: say(logf, '    ' + m))
        say(logf, 'evaluation %s: F=%s %s' % (a.eval, F, {k: (v if k != 'l1' else {kk: vv for kk, vv in v.items() if kk != 'paths'}) for k, v in info.items()} if info else info))
        return 0
    F0, info0, _ = evaluate(pocket, cons, base, l1=True, log=lambda m: say(logf, '    ' + m))
    say(logf, 'start: F=%.1f locked %d, L1 unrouted %s' % (F0 if F0 is not None else -1, info0['locked'] if info0 else -1,
                                                           info0['l1']['unrouted'] if info0 and info0['l1'] else '?'))
    best_path = os.path.join(out_dir, 'best.json')
    best = {'F': F0 if F0 is not None else 1e9, 'placement': base, 'info': {'locked': info0['locked'] if info0 else None}}
    # F0 (without L1) and F1 (with L1) are different scales: L1 runs when F0 is no worse than the
    # best F0, and the best placement is chosen by F1
    bestF0, _, _ = evaluate(pocket, cons, base, l1=a.l1_always)
    bestF0 = bestF0 if bestF0 is not None else 1e9
    if os.path.exists(best_path):
        prev = json.load(open(best_path))
        if prev['F'] < best['F']:
            best = prev; best['placement'] = {k: tuple(v) for k, v in prev['placement'].items()}
            Fp, _, _ = evaluate(pocket, cons, best['placement'], l1=a.l1_always)
            bestF0 = min(bestF0, Fp if Fp is not None else 1e9)
            say(logf, 'resuming from F=%.1f (F0=%.1f)' % (best['F'], bestF0))
    rng = random.Random(a.seed)
    chains = [dict(best['placement']) for _ in range(a.chains)]
    chainF = [bestF0] * a.chains          # chains live on the F0 scale (without L1)
    T0, T1 = 300.0, 5.0
    t_start = time.time(); step = 0; rejected = 0
    while time.time() - t_start < a.seconds:
        frac = (time.time() - t_start) / a.seconds
        T = T0 * (T1 / T0) ** frac
        for i in range(a.chains):
            cand, desc = propose(chains[i], pocket, rng, allow_flip=not a.no_flip, allow_ic=a.movable_ics)
            F, info, _ = evaluate(pocket, cons, cand, l1=a.l1_always)
            step += 1
            if F is None:
                rejected += 1
                continue
            dF = F - chainF[i]
            if dF <= 0 or rng.random() < math.exp(-dF / T):
                chains[i], chainF[i] = cand, F
                if F <= bestF0 + 1e-9:
                    # candidate no worse than the best by L0 — confirm with L1; the best is chosen by F1
                    bestF0 = F
                    F1, info1 = (F, info) if a.l1_always else evaluate(pocket, cons, cand, l1=True)[:2]
                    unr = info1['l1']['unrouted'] if info1 and info1['l1'] else '?'
                    say(logf, 'step %d: F0=%.1f (locked %d) %s; L1: F=%.1f unrouted %s%s'
                        % (step, F, info['locked'], desc, F1, unr, '  <- NEW BEST' if F1 is not None and F1 < best['F'] - 1e-9 else ''))
                    if F1 is None or F1 >= best['F'] - 1e-9:
                        continue
                    best = {'F': F1, 'placement': cand,
                            'info': {'locked': info['locked'], 'locked0': info0['locked'] if info0 else None,
                                     'l1_unrouted': unr}}
                    with open(best_path, 'w') as f:
                        # apply_place/place_campaign need the window: which copper to rip and where to re-route
                        json.dump({'F': best['F'], 'placement': {k: list(v) for k, v in best['placement'].items()},
                                   'info': best['info'], 'refs': refs, 'window': list(pocket.bbox)},
                                  f, ensure_ascii=False, indent=1)
        if step % 200 < a.chains:
            say(logf, 'step %d, T=%.1f, rejected by constraints %d, best F=%.1f (locked %s)'
                % (step, T, rejected, best['F'], best['info'].get('locked')))
    say(logf, 'done: steps %d, best F=%.1f, %s' % (step, best['F'], best['info']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
