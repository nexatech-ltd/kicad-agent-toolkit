"""Map of the router model's free cells for one net on one layer inside a window.

Usage: python3 tools/util/diag_free.py DIR NET LAYER x0,y0,x1,y1

DIR must contain drc0.json (a DRC report) and a copy of the board named like the project
board; NET is the short net name of an unrouted connection in that report. Each cell is
printed as '.' free, 'v' free and usable for a via, or the reason it is blocked: P pads,
T tracks, S strict (per-class) clearances, K zones/keepouts, H holes, # other. Use it to
decide which nets form the wall around a stuck connection before ripping anything up.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import os, sys, torch
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(_HERE, '..', 'gpuroute'), os.path.join(_HERE, '..')]
import pcbnew, paths, raster, maze_route as MR, drcinfo as D
import model as M
from model import Model
d, net_short, layer = sys.argv[1], sys.argv[2], sys.argv[3]; x0, y0, x1, y1 = (float(v) for v in sys.argv[4].split(','))
drc = D.load(d + '/drc0.json'); nets = sorted({b[0] for b in D.signal_breaks(drc) if b[0].split('/')[-1] == net_short})
bp = os.path.join(d, os.path.basename(paths.BOARD))
board = pcbnew.LoadBoard(bp); g = raster.Grid(board, step=MR.STEP)
m = Model(board, bp, g, nets, 'cuda', log=lambda *a: None)
net = nets[0]; w, clr = m.cls[net]; L = raster.SIGNAL.index(layer)
z = torch.zeros((len(raster.SIGNAL), g.h, g.w), device='cuda'); cost, fv = m.cost_for(net, z, z, z, 0.0, 0.0)
free = torch.isfinite(cost[L]) & (cost[L] < 1e6) if cost.dtype.is_floating_point else cost[L] < 1e6
r0, r1, c0, c1 = g.row(y0), g.row(y1), g.col(x0), g.col(x1)
print('net %s (w %.2f clr %.2f), layer %s, window x %.1f..%.1f y %.1f..%.1f; . free  # blocked  v free and fits a via  P pads  T tracks  S strict  K zones  H holes' % (net_short, w, clr, layer, x0, x1, y0, y1))
dP, dT, dK, dH = m.dP[L], m.dT[L], m.dK[L], m.dH[L]
thrP = w/2 + M.PAD_CLEARANCE + M.PAD_SAFE_MM; thrT = w/2 + clr + M.SAFE_MM; thrK = w/2 + M.SAFE_MM; thrH = clr + w/2
for r in range(r0, r1 + 1):
    row = ''
    for c in range(c0, c1 + 1):
        if bool(free[r, c]): row += 'v' if bool(fv[r, c]) else '.'
        else:
            why = 'P' if dP[r, c] < thrP else 'T' if dT[r, c] < thrT else 'K' if dK[r, c] < thrK else 'H' if dH[r, c] < thrH else '#'
            if why == '#':
                for need, dd in m.dS.items():
                    if dd[L][r, c] < max(need, clr) + w/2 + M.SAFE_MM: why = 'S'
            row += why
    print('%7.2f %s' % (g.y(r), row))
print('        ' + ''.join(str(int(g.x(c)) % 10) if abs(g.x(c) - round(g.x(c))) < 0.05 else ' ' for c in range(c0, c1 + 1)))
