#!/usr/bin/env python3
"""Verify KAT_PAR: build the router model by the serial and the parallel path in one process
and require every tensor to match bit-exact.

    KAT_ROOT=$PWD python3 tools/gpuroute/check_par.py --board B --drc D --limit 40 --planes
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import numpy as np
import pcbnew
import torch

import drcinfo as D
import paths
import raster
import maze_route as MR
from model import Model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--limit', type=int, default=40)
    ap.add_argument('--planes', action='store_true')
    ap.add_argument('--check-fill', action='store_true', help='also set KAT_PAR_CHECK=1 (fill every polygon both ways)')
    a = ap.parse_args()
    drc = D.load(a.drc)
    breaks = [b for b in D.signal_breaks(drc) if b[4] and 'Zone' not in b[1] and b[0].split('/')[-1] not in D.PLANE_NETS]
    if a.planes:
        breaks += [b for b in D.breaks(drc) if b[4] and b[0].split('/')[-1] in D.PLANE_NETS and b[1] != 'Zone+Zone']
    nets = []
    for b in breaks[:a.limit]:
        if b[0] not in nets:
            nets.append(b[0])
    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=MR.STEP)
    print('nets %d (plane nets %d)' % (len(nets), sum(1 for n in nets if n.split('/')[-1] in D.PLANE_NETS)))
    os.environ['KAT_PAR'] = '0'
    t = time.time(); A = Model(board, a.board, g, nets, 'cuda', log=lambda *x: None); torch.cuda.synchronize()
    tA = time.time() - t
    os.environ['KAT_PAR'] = '1'
    if a.check_fill:
        os.environ['KAT_PAR_CHECK'] = '1'
    t = time.time(); B = Model(board, a.board, g, nets, 'cuda', log=lambda *x: None); torch.cuda.synchronize()
    tB = time.time() - t
    print('Model: serial path %.1f s, KAT_PAR %.1f s (threads %d)' % (tA, tB, __import__('par').workers()))
    bad = 0

    def cmp(name, x, y):
        nonlocal bad
        if isinstance(x, np.ndarray):
            eq = np.array_equal(x, y); nd = int((x != y).sum()) if not eq else 0
        elif torch.is_tensor(x):
            eq = torch.equal(x, y); nd = int((x != y).sum()) if not eq else 0
        else:
            eq = x == y; nd = 0 if eq else 1
        if not eq:
            bad += 1
            print('  MISMATCH %-28s cells %d' % (name, nd))
    for name in ('dP', 'dT', 'dK', 'dH', 'fine', 'inb_via', 'fixed_pad'):
        cmp(name, getattr(A, name), getattr(B, name))
    for k in A.dS: cmp('dS[%s]' % k, A.dS[k], B.dS[k])
    for k in A.dV: cmp('dV[%s]' % k, A.dV[k], B.dV[k])
    for k in A.free: cmp('free%s' % (k,), A.free[k], B.free[k]); cmp('free_via%s' % (k,), A.free_via[k], B.free_via[k])
    for k in A.fixed: cmp('fixed[%s]' % k, A.fixed[k], B.fixed[k])
    for n in nets:
        s = n.split('/')[-1][:16]
        cmp('own[%s]' % s, A.own[n], B.own[n]); cmp('labels[%s]' % s, A.labels[n], B.labels[n]); cmp('ncomp[%s]' % s, A.ncomp[n], B.ncomp[n])
        cmp('cover_pad[%s]' % s, A.cover_pad[n], B.cover_pad[n]); cmp('cover_trk[%s]' % s, A.cover_trk[n], B.cover_trk[n])
    print('RESULT: %d tensors differ' % bad if bad else 'RESULT: all tensors match bit-exact')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
