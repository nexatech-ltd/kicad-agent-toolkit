#!/usr/bin/env python3
"""KAT_PAR check for the pocket model: Pocket + model_for + L0/L1 via the old and the new path —
own/cover/free_for must be bit-exact, the L0/L1 numbers equal; also reports timings.

    KAT_ROOT=$PWD python3 tools/gpuroute/place/check_par_pocket.py --board B --refs U1,U2 [--placement best.json] [--window ...]
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


def build(flag, board, bp, g, geom, refs, bbox, st):
    os.environ['KAT_PAR'] = flag
    import importlib
    import escape as E
    import fitness as FT
    t = time.time()
    pk = E.Pocket(board, bp, g, geom, refs, log=lambda *a: None, bbox=bbox)
    t_pk = time.time() - t
    t = time.time()
    cm, _ = pk.model_for(st)
    ends = pk.ends_of(st)
    nets = sorted({e[0] for e in ends})
    frees = {n: cm.free_for(n) for n in nets}
    n0, locked, tight = FT.l0_score(pk, cm, st)
    torch.cuda.synchronize()
    t_l0 = time.time() - t
    t = time.time()
    res = FT.l1_score(pk, cm, st)
    torch.cuda.synchronize()
    t_l1 = time.time() - t
    return dict(pk=pk, cm=cm, frees=frees, locked=sorted((l[3], l[4], l[5]) for l in locked), tight=tight,
                l1={k: res[k] for k in ('unrouted', 'escapes', 'escapes_failed', 'via_breaks', 'via_failed', 'vias', 'nets', 'breaks')},
                length=res['length_mm'], t=(t_pk, t_l0, t_l1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--geom', default=paths.work('place', 'geom.json'))
    ap.add_argument('--refs', required=True, help='comma-separated ICs of the pocket, e.g. U1,U2')
    ap.add_argument('--placement', default=None)
    ap.add_argument('--window', default=None)
    a = ap.parse_args()
    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=0.1)
    geom = json.load(open(a.geom))
    bbox = tuple(float(v) for v in a.window.split(',')) if a.window else None
    st = {}
    if a.placement:
        st = {k: tuple(v) for k, v in json.load(open(a.placement))['placement'].items()}
    refs = a.refs.split(',')
    A = build('0', board, a.board, g, geom, refs, bbox, {k: v for k, v in st.items()})
    B = build('1', board, a.board, g, geom, refs, bbox, {k: v for k, v in st.items()})
    bad = 0
    for net in A['cm'].own:
        for name, x, y in (('own', A['cm'].own[net], B['cm'].own[net]), ('cover', A['cm'].cover[net], B['cm'].cover[net])):
            if not torch.equal(x, y):
                bad += 1; print('  MISMATCH %s[%s] cells %d' % (name, net.split('/')[-1], int((x != y).sum())))
    for net in A['frees']:
        fa, fb = A['frees'][net], B['frees'][net]
        if not torch.equal(fa[0], fb[0]) or not torch.equal(fa[1], fb[1]):
            bad += 1; print('  MISMATCH free_for[%s]: cells %d / via %d' % (net.split('/')[-1], int((fa[0] != fb[0]).sum()), int((fa[1] != fb[1]).sum())))
    for name in ('dT', 'dH', 'dK'):
        if not torch.equal(getattr(A['cm'], name), getattr(B['cm'], name)):
            bad += 1; print('  MISMATCH %s' % name)
    for k in A['cm'].dS:
        if not torch.equal(A['cm'].dS[k], B['cm'].dS[k]):
            bad += 1; print('  MISMATCH dS[%s]' % k)
    if A['locked'] != B['locked']:
        bad += 1; print('  MISMATCH L0:', A['locked'], B['locked'])
    if A['l1'] != B['l1'] or abs(A['length'] - B['length']) > 1e-6:
        bad += 1; print('  MISMATCH L1:', A['l1'], A['length'], '|', B['l1'], B['length'])
    print('L0 locked %d, L1 %s, length %.1f' % (len(A['locked']), A['l1'], A['length']))
    print('time (Pocket, model+free+L0, L1): old %.1f / %.1f / %.1f s; KAT_PAR %.1f / %.1f / %.1f s' % (*A['t'], *B['t']))
    print('RESULT: %d mismatches' % bad if bad else 'RESULT: pocket is bit-exact')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
