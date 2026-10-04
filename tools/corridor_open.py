#!/usr/bin/env python3
"""Iteratively clear a corridor along the reachability boundary: a minimal list of wall nets.

`reach.py` names the nets on the boundary of the reachable area. Removing one wall is not
enough: there is another one behind it. Here flooding and removal alternate on a COPY of the
board until the target becomes reachable or the iterations run out, and the accumulated list
of blockers with object counts is printed. This is reconnaissance, not a board edit: the list
then feeds a targeted re-route (rip the walls - route the target - restore the walls), where
DRC is the judge.

    python3 tools/corridor_open.py --board COPY --net NET \
        --from 111.9,72.86 --to 116.3,99.3 [--iters 12] [--r 1.5] [--protect RF_NET]
"""
import argparse
import collections
import math
import os
import subprocess
import sys

import numpy as np
import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import maze_route as M
import project
import raster
from reach import flood

PLANES = project.plane_nets()


def wall_nets(b, tracks, F, g, seen, tx, ty, dmin, layers, r_look):
    """Nets on the reachability boundary towards the target: {net: [(layer, x, y), ...]}."""
    lid = {L: b.GetLayerID(M.CANON.get(L, L)) for L in layers}
    per = collections.defaultdict(list)
    for L in layers:
        m = seen[L]
        nb = np.zeros_like(m)
        nb[1:, :] |= m[:-1, :]; nb[:-1, :] |= m[1:, :]
        nb[:, 1:] |= m[:, :-1]; nb[:, :-1] |= m[:, 1:]
        wall = nb & ~m & ~F.free[L]
        cells = [(g.x(int(c)), g.y(int(r))) for r, c in np.argwhere(wall)
                 if math.hypot(g.x(int(c)) - tx, g.y(int(r)) - ty) <= dmin]
        if not cells:
            continue
        for t in tracks:
            n = t.GetNetname().split('/')[-1]
            if t.GetClass() == 'PCB_VIA':
                p = t.GetPosition()
                px, py, qx, qy, hw = p.x / 1e6, p.y / 1e6, p.x / 1e6, p.y / 1e6, t.GetWidth(lid[L]) / 2e6
            else:
                if t.GetLayer() != lid[L]:
                    continue
                s, e = t.GetStart(), t.GetEnd()
                px, py, qx, qy, hw = s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6, t.GetWidth() / 2e6
            for cx, cy in cells:
                dx, dy = qx - px, qy - py
                l2 = dx * dx + dy * dy
                tt = 0.0 if l2 == 0 else max(0.0, min(1.0, ((cx - px) * dx + (cy - py) * dy) / l2))
                if math.hypot(cx - (px + tt * dx), cy - (py + tt * dy)) - hw <= r_look:
                    per[n].append((L, cx, cy))
                    break
    return per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--net', required=True)
    ap.add_argument('--from', dest='src', required=True)
    ap.add_argument('--to', dest='dst', required=True)
    ap.add_argument('--iters', type=int, default=12)
    ap.add_argument('--r', type=float, default=1.5, help='radius of copper removal around a boundary cell, mm')
    ap.add_argument('--r-look', type=float, default=0.5, help='radius to look for the blocking object around a boundary cell, mm')
    ap.add_argument('--per-iter', type=int, default=2, help='how many nets to remove per round')
    ap.add_argument('--protect', default=','.join(project.get('protect_nets', [])),
                    help='comma-separated nets never to remove (default: protect_nets from the config); '
                         'nets of the no_touch_classes net classes are always protected')
    ap.add_argument('--keep-planes', action='store_true', help='do not remove copper of plane nets')
    ap.add_argument('--round', action='store_true',
                    help='internal mode: ONE round and exit. Needed because pcbnew SWIG survives '
                         'only one LoadBoard per process: from the second round on, footprints '
                         'lose their Pads() method. The outer run spawns subprocesses itself.')
    a = ap.parse_args()

    if not a.round:
        removed_all = collections.Counter()
        for it in range(1, a.iters + 1):
            args = [sys.executable, os.path.abspath(__file__), '--round',
                    '--board', a.board, '--net', a.net, '--from', a.src, '--to', a.dst,
                    '--r', str(a.r), '--r-look', str(a.r_look), '--per-iter', str(a.per_iter),
                    '--protect', a.protect] + (['--keep-planes'] if a.keep_planes else [])
            r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
            for line in r.stdout.splitlines():
                if line.startswith(('round', '        ', 'REMOVED ')):
                    if line.startswith('REMOVED '):
                        n, k = line[len('REMOVED '):].rsplit(' ', 1)
                        removed_all[n] += int(k)
                    else:
                        print(line.replace('round 1', 'round %d' % it), flush=True)
            if r.returncode == 0:
                break
            if r.returncode != 2:
                print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:])
                break
        print('\ntotal removed per net:')
        for n, k in removed_all.most_common():
            print('   %-24s %d' % (n, k))
        return 0

    prot = {s.strip() for s in a.protect.split(',') if s.strip()}
    sx, sy = (float(v) for v in a.src.split(','))
    tx, ty = (float(v) for v in a.dst.split(','))
    removed = collections.Counter()
    for it in range(1, 2):
        M.set_layers(a.board)
        b = pcbnew.LoadBoard(a.board)
        # KiCad 10 SWIG: take the track list ONCE before building the raster - after the
        # traversal inside copper_mask a second GetTracks() no longer iterates.
        TR = list(b.GetTracks())
        no_touch = set(project.get('no_touch_classes', []))
        if no_touch:   # nets of protected net classes (e.g. impedance-controlled RF) are never removed
            prot |= {t.GetNetname().split('/')[-1] for t in TR
                     if no_touch & set(t.GetNetClassName().split(','))}
        w, clr = M.net_class_width(a.board, a.net)
        M.Field._strict = M.strict_nets(a.board, b)
        M.Field._fine = None
        g = raster.Grid(b, step=M.STEP)
        F = M.Field(b, g, a.net, w, clr)
        starts = M.endpoint_cells(F, sx, sy)
        goals = M.endpoint_cells(F, tx, ty)
        if not starts:
            print('round %d: no free cells at the start' % it)
            return 1
        seen = flood(F, g, starts)
        tot = sum(int(m.sum()) for m in seen.values())
        if any(seen[L][r, c] for L, r, c in goals):
            print('round %d: TARGET REACHABLE (%d cells)' % (it, tot))
            return 0
        dmin = min(math.hypot(g.x(int(c)) - tx, g.y(int(r)) - ty)
                   for L in F.layers for r, c in np.argwhere(seen[L]))
        per = wall_nets(b, TR, F, g, seen, tx, ty, dmin, F.layers, a.r_look)
        cand = [(len(v), n, v) for n, v in per.items()
                if n not in prot and not (a.keep_planes and n in PLANES)]
        cand.sort(reverse=True)
        if not cand:
            print('round %d: nothing to remove, %.2f mm to the target' % (it, dmin))
            return 1
        take = cand[:a.per_iter]
        print('round %d: reachable %d, %.2f mm to the target; removing %s'
              % (it, tot, dmin, ', '.join('%s(%d cells)' % (n, k) for k, n, _ in take)))
        gone = 0
        for k, n, cells in take:
            for t in TR:
                if t.GetNetname().split('/')[-1] != n:
                    continue
                if t.GetClass() == 'PCB_VIA':
                    p = t.GetPosition()
                    pts = [(p.x / 1e6, p.y / 1e6)]
                else:
                    s, e = t.GetStart(), t.GetEnd()
                    pts = [(s.x / 1e6, s.y / 1e6), (e.x / 1e6, e.y / 1e6)]
                if any(math.hypot(px - cx, py - cy) <= a.r for px, py in pts for _, cx, cy in cells):
                    b.Remove(t)
                    gone += 1
                    removed[n] += 1
        print('        objects removed: %d' % gone)
        pcbnew.SaveBoard(a.board, b)
        for n, k in removed.items():
            print('REMOVED %s %d' % (n, k))
        return 2
    return 1


if __name__ == '__main__':
    sys.exit(main())
