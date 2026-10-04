#!/usr/bin/env python3
"""Reachability from a point and wall analysis: which nets hold the corridor shut.

On failure the router only prints "no path (N cells explored)". A small N means a locked pad;
a large N means the pad is free but the corridor is locked, and then the question is not
"where is it crowded" but WHOSE copper forms the boundary of the reachable area towards the
target. This tool floods the same `maze_route.Field` over all routing layers (layer changes
only where a via fits), then names the nearest foreign copper for every boundary cell that
faces the target. Nets are ranked by the number of cells they hold - that is the list of
what to rip up or move.

    python3 tools/reach.py --board B --net NET --from 111.9,72.86 \
        --to 116.3,99.3 [--wall-r 3.0] [--top 15]
"""
import argparse
import collections
import math
import os
import sys

import numpy as np
import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import maze_route as M
import raster


def flood(field, g, starts, parent=None):
    """Flood over layers: 4-neighbours within a layer, layer changes only where a via fits.

    `parent` (a dict) is filled with predecessors so the path and its extent can be
    recovered: if the path leaves the A* search window, the router will legitimately fail
    to find it even though the target is reachable.
    """
    L = field.layers
    seen = {name: np.zeros((g.h, g.w), dtype=bool) for name in L}
    dq = collections.deque()
    for name, r, c in starts:
        if field.free[name][r, c] and not seen[name][r, c]:
            seen[name][r, c] = True
            if parent is not None:
                parent[(name, r, c)] = None
            dq.append((name, r, c))
    while dq:
        name, r, c = dq.popleft()
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < g.h and 0 <= cc < g.w and field.free[name][rr, cc] and not seen[name][rr, cc]:
                seen[name][rr, cc] = True
                if parent is not None:
                    parent[(name, rr, cc)] = (name, r, c)
                dq.append((name, rr, cc))
        if field.free_via[r, c]:
            for other in L:
                if other != name and field.free[other][r, c] and not seen[other][r, c]:
                    seen[other][r, c] = True
                    if parent is not None:
                        parent[(other, r, c)] = (name, r, c)
                    dq.append((other, r, c))
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--net', required=True)
    ap.add_argument('--from', dest='src', required=True, help='x,y - start (end of already laid copper)')
    ap.add_argument('--to', dest='dst', required=True, help='x,y - target (target pad)')
    ap.add_argument('--wall-r', type=float, default=3.0, help='radius to look for the blocking object around a boundary cell, mm')
    ap.add_argument('--top', type=int, default=15)
    ap.add_argument('--path', action='store_true',
                    help='recover the flood path and print its extent, length and layer changes; '
                         'tells "no geometry" apart from "path outside the A* search window"')
    a = ap.parse_args()

    M.set_layers(a.board)
    b = pcbnew.LoadBoard(a.board)
    w, clr = M.net_class_width(a.board, a.net)
    M.Field._strict = M.strict_nets(a.board, b)
    g = raster.Grid(b, step=M.STEP)
    F = M.Field(b, g, a.net, w, clr)
    sx, sy = (float(v) for v in a.src.split(','))
    tx, ty = (float(v) for v in a.dst.split(','))
    starts = M.endpoint_cells(F, sx, sy)
    goals = M.endpoint_cells(F, tx, ty)
    print('net %s: track %.2f clearance %.2f; endpoint cells at start %d, at target %d'
          % (a.net, w, clr, len(starts), len(goals)))
    if not starts:
        print('no free cells at the start - the copper end itself is locked')
        return 1
    par = {} if a.path else None
    seen = flood(F, g, starts, par)
    total = sum(int(m.sum()) for m in seen.values())
    print('reachable cells: %d per layer %s'
          % (total, {L: int(seen[L].sum()) for L in F.layers}))
    hit = [(L, r, c) for L, r, c in goals if seen[L][r, c]]
    if hit:
        print('TARGET REACHABLE - a router "no path" means search window or cost, not geometry')
        if par is not None:
            node = hit[0]
            pts = []
            while node is not None:
                L, r, c = node
                pts.append((L, g.x(c), g.y(r)))
                node = par[node]
            pts.reverse()
            xs = [p[1] for p in pts]; ys = [p[2] for p in pts]
            length = sum(math.hypot(pts[i+1][1]-pts[i][1], pts[i+1][2]-pts[i][2])
                         for i in range(len(pts)-1))
            vias = sum(1 for i in range(len(pts)-1) if pts[i][0] != pts[i+1][0])
            print('flood path: %d cells, length %.1f mm, layer changes %d' % (len(pts), length, vias))
            print('path extent: X %.1f..%.1f  Y %.1f..%.1f' % (min(xs), max(xs), min(ys), max(ys)))
            seq = [pts[0][0]]
            for p in pts[1:]:
                if p[0] != seq[-1]:
                    seq.append(p[0])
            print('layers in order: %s' % ' -> '.join(seq))
            wx = 25.0
            need = (min(min(xs), sx, tx) - wx, max(max(xs), sx, tx) + wx)
            print('A* window x: %.1f..%.1f with MARGIN 25 - path %s'
                  % (min(sx, tx) - wx, max(sx, tx) + wx,
                     'inside' if min(xs) >= min(sx, tx) - wx and max(xs) <= max(sx, tx) + wx else 'LEAVES THE WINDOW'))
            print('A* window y: %.1f..%.1f - path %s'
                  % (min(sy, ty) - wx, max(sy, ty) + wx,
                     'inside' if min(ys) >= min(sy, ty) - wx and max(ys) <= max(sy, ty) + wx else 'LEAVES THE WINDOW'))
        return 0
    # the reachable cell nearest to the target - that is where the wall must be broken
    best = None
    for L in F.layers:
        idx = np.argwhere(seen[L])
        if not len(idx):
            continue
        d = np.hypot(g.x(idx[:, 1]) - tx, g.y(idx[:, 0]) - ty)
        k = int(np.argmin(d))
        if best is None or d[k] < best[0]:
            best = (float(d[k]), L, int(idx[k, 0]), int(idx[k, 1]))
    print('nearest reachable cell to the target: %s (%.2f, %.2f), %.2f mm to the target'
          % (best[1], g.x(best[3]), g.y(best[2]), best[0]))
    # wall: unreachable cells adjacent to reachable ones, and closer to the target
    lid = {L: b.GetLayerID(M.CANON.get(L, L)) for L in F.layers}
    objs = collections.defaultdict(list)   # layer -> [(net, kind, geometry)]
    for t in b.GetTracks():
        n = t.GetNetname().split('/')[-1]
        if n == a.net.split('/')[-1]:
            continue
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition()
            for L in F.layers:
                objs[L].append((n, 'via', p.x / 1e6, p.y / 1e6, p.x / 1e6, p.y / 1e6,
                                t.GetWidth(lid[L]) / 2e6))
        else:
            for L in F.layers:
                if t.GetLayer() == lid[L]:
                    s, e = t.GetStart(), t.GetEnd()
                    objs[L].append((n, 'track', s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6,
                                    t.GetWidth() / 2e6))
    for fp in b.GetFootprints():
        for p in fp.Pads():
            n = p.GetNetname().split('/')[-1]
            if n == a.net.split('/')[-1]:
                continue
            q = p.GetPosition()
            for L in F.layers:
                if p.IsOnLayer(lid[L]):
                    sz = p.GetSize()
                    objs[L].append((n or fp.GetReference(), 'pad %s' % fp.GetReference(),
                                    q.x / 1e6, q.y / 1e6, q.x / 1e6, q.y / 1e6,
                                    max(sz.x, sz.y) / 2e6))

    def dseg(px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    hold = collections.Counter()
    who = {}
    for L in F.layers:
        m = seen[L]
        nb = np.zeros_like(m)
        nb[1:, :] |= m[:-1, :]; nb[:-1, :] |= m[1:, :]
        nb[:, 1:] |= m[:, :-1]; nb[:, :-1] |= m[:, 1:]
        wall = nb & ~m & ~F.free[L]
        for r, c in np.argwhere(wall):
            x, y = g.x(int(c)), g.y(int(r))
            # only the part of the boundary that faces the target
            if math.hypot(x - tx, y - ty) > best[0]:
                continue
            near = None
            for n, kind, x1, y1, x2, y2, hw in objs[L]:
                d = dseg(x, y, x1, y1, x2, y2) - hw
                if d > a.wall_r:
                    continue
                if near is None or d < near[0]:
                    near = (d, n, kind)
            if near:
                hold[near[1]] += 1
                who.setdefault(near[1], set()).add('%s %s' % (L, near[2]))
    print('\nnets holding the boundary towards the target (cells / where):')
    for n, k in hold.most_common(a.top):
        print('   %-22s %4d   %s' % (n, k, ', '.join(sorted(who[n])[:4])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
