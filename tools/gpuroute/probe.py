#!/usr/bin/env python3
"""Raster around a point for one net: where its own copper is, what is free, where the wall is and why.

    python3 tools/gpuroute/probe.py --board ... --net 'Net-(U1-PA0)' --x 123.4 --y 56.7 --r 1.5
Symbols: '#' own copper, '.' free, 'x' foreign copper (raster without dilation), '+' clearance
of foreign copper, 'o' hole / hole clearance, 'k' keepout, 'e' board edge, 'F' fixed copper of a
participant.
"""
import argparse, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import numpy as np, pcbnew, torch
import paths, raster, maze_route as MR
from model import Model

ap = argparse.ArgumentParser()
ap.add_argument('--board', default=paths.work('gpu', 'base.kicad_pcb'))
ap.add_argument('--net', required=True)
ap.add_argument('--x', type=float, required=True)
ap.add_argument('--y', type=float, required=True)
ap.add_argument('--r', type=float, default=1.5)
ap.add_argument('--layers', default='F.Cu,B.Cu')
a = ap.parse_args()
board = pcbnew.LoadBoard(a.board)
g = raster.Grid(board, step=MR.STEP)
nets = [n for n in {p.GetNetname() for fp in board.GetFootprints() for p in fp.Pads()} if n.split('/')[-1] == a.net or n == a.net]
assert nets, 'net not found'
net = nets[0]
m = Model(board, a.board, g, [net], 'cuda', log=lambda *x: None)
w, clr = m.cls[net]
print('net %s: w=%.3f clr=%.2f need=%.2f components %d' % (net, w, clr, m.need[net], m.ncomp[net]))
cost, fv = m.cost_for(net, torch.zeros_like(m.free[(w, clr)], dtype=torch.float32),
                      torch.zeros_like(m.free[(w, clr)], dtype=torch.float32),
                      torch.zeros_like(m.free[(w, clr)], dtype=torch.float32), 0.5, 0.5)
k = int(a.r / g.step)
r0, c0 = g.row(a.y), g.col(a.x)
own = m.own[net].cpu().numpy()
free = torch.isfinite(cost).cpu().numpy()
for L in a.layers.split(','):
    li = raster.SIGNAL.index(L)
    cu, _ = raster.copper_mask(board, g, L, nets_skip={net})
    ko, _ = raster.keepout_mask(board, g, L)
    print('--- %s: rows %d..%d, cols %d..%d (x %.1f..%.1f, y %.1f..%.1f)' % (
        L, r0 - k, r0 + k, c0 - k, c0 + k, g.x(c0 - k), g.x(c0 + k), g.y(r0 - k), g.y(r0 + k)))
    for r in range(r0 - k, r0 + k + 1):
        row = ''
        for c in range(c0 - k, c0 + k + 1):
            if own[li, r, c]: ch = '#'
            elif free[li, r, c]: ch = '.'
            elif cu[r, c]: ch = 'x'
            elif ko[r, c]: ch = 'k'
            else: ch = '+'
            row += ch
        print(row)
print('via-free cells in the window: %d' % int(fv[r0-k:r0+k+1, c0-k:c0+k+1].sum()))
