"""Multi-layer wavefront on the GPU (torch): Dijkstra as iterated relaxation.

Why relaxation and not a priority queue. A GPU has no cheap heap, but it has cheap tensor
operations over the whole window at once: in one iteration every cell takes the best of its
eight in-layer neighbours and of its via column. This is cell-wise Bellman-Ford: it needs as
many iterations as there are steps in the longest path (a 100 mm run = 1000 steps), each a dozen
operations on an (L, h, w) tensor. Early exit: once the goal is reached and every cell changed in
the iteration costs no less than the goal, nothing can improve any more (costs are positive).

Cost of entering a cell = layer_cost * cell_cost (* sqrt2 diagonally); a layer change =
via_cost + cell cost on the new layer, allowed only where free_via.

Backtrace over the finished distance map runs on the CPU: from the goal cell step to the
neighbour for which dist[neighbour] + step cost == dist[cell]. No parent map is kept on the GPU.
"""
import math

import numpy as np
import torch
import torch.nn.functional as F

INF = float('inf')
DIAG = math.sqrt(2.0)
DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]
STEP_COST = [DIAG if (dr and dc) else 1.0 for dr, dc in DIRS]


def shift(t, dr, dc):
    """out[..., r, c] = t[..., r-dr, c-dc]; infinity outside."""
    h, w = t.shape[-2], t.shape[-1]
    p = F.pad(t, (max(dc, 0), max(-dc, 0), max(dr, 0), max(-dr, 0)), value=INF)
    r0, c0 = max(-dr, 0), max(-dc, 0)
    return p[..., r0:r0 + h, c0:c0 + w]


class Wave:
    def __init__(self, layer_cost, via_cost, device='cuda'):
        self.layer_cost = list(layer_cost)
        self.lc = torch.tensor(self.layer_cost, dtype=torch.float32, device=device).view(-1, 1, 1)
        self.via_cost = float(via_cost)
        self.dev = device

    def run(self, cost, free_via, starts, goals, max_iter=8000, eps=1e-4):
        """cost: (L,h,w) float32 - cell cost (>= 1), inf where forbidden;
        free_via: (h,w) bool; starts/goals: lists of (l, r, c) in window coordinates.
        Returns (dist, best goal, iterations) or (None, None, iterations)."""
        L, h, w = cost.shape
        si = self._idx(starts)
        gi = self._idx(goals)
        if si.numel() == 0 or gi.numel() == 0:
            return None, None, 0
        dist = torch.full((L, h, w), INF, device=self.dev)
        dist[si[0], si[1], si[2]] = 0.0
        lc_cost = self.lc * cost                                   # (L,h,w)
        via_c = torch.where(free_via, torch.full_like(free_via, self.via_cost, dtype=torch.float32),
                            torch.full_like(free_via, INF, dtype=torch.float32))
        via_c = via_c.unsqueeze(0) + cost                          # entering a new layer
        it = 0
        for it in range(1, max_iter + 1):
            new = dist
            for (dr, dc), sc in zip(DIRS, STEP_COST):
                new = torch.minimum(new, shift(dist, dr, dc) + sc * lc_cost)
            mn = dist.min(dim=0, keepdim=True).values
            new = torch.minimum(new, mn + via_c)
            changed = new < dist - eps
            dist = new
            if not bool(changed.any()):
                break
            gd = dist[gi[0], gi[1], gi[2]].min()
            if torch.isfinite(gd) and float(dist[changed].min()) >= float(gd):
                break
        gd_all = dist[gi[0], gi[1], gi[2]]
        k = int(gd_all.argmin())
        if not torch.isfinite(gd_all[k]):
            return None, None, it
        return dist, (int(gi[0, k]), int(gi[1, k]), int(gi[2, k])), it

    def _idx(self, pts):
        """List of (l, r, c) or a (3, N) tensor -> LongTensor (3, N) on the device."""
        if isinstance(pts, torch.Tensor):
            return pts.to(self.dev, torch.long).reshape(3, -1)
        if not pts:
            return torch.zeros((3, 0), dtype=torch.long, device=self.dev)
        return torch.tensor(pts, device=self.dev, dtype=torch.long).T

    def reach(self, cost, free_via, starts, iters=40):
        """Distance map from the starts after a fixed number of iterations (no goal): estimates
        where one can get to from a component at all."""
        L, h, w = cost.shape
        si = self._idx(starts)
        dist = torch.full((L, h, w), INF, device=self.dev)
        if si.numel() == 0:
            return dist
        dist[si[0], si[1], si[2]] = 0.0
        lc_cost = self.lc * cost
        via_c = torch.where(free_via, torch.full_like(free_via, self.via_cost, dtype=torch.float32),
                            torch.full_like(free_via, INF, dtype=torch.float32)).unsqueeze(0) + cost
        for _ in range(iters):
            new = dist
            for (dr, dc), sc in zip(DIRS, STEP_COST):
                new = torch.minimum(new, shift(dist, dr, dc) + sc * lc_cost)
            new = torch.minimum(new, dist.min(dim=0, keepdim=True).values + via_c)
            if not bool((new < dist - 1e-4).any()):
                dist = new
                break
            dist = new
        return dist

    def backtrace(self, dist, cost, free_via, goal, tol=1e-3):
        """Path from the goal back to a start (dist == 0) over the distance map. List of (l, r, c) from the start."""
        d = dist.detach().cpu().numpy()
        cst = cost.detach().cpu().numpy()
        fv = free_via.detach().cpu().numpy()
        L, h, w = d.shape
        l, r, c = goal
        path = [(l, r, c)]
        guard = 0
        while d[l, r, c] > 0.0:
            guard += 1
            if guard > 400000:
                return None
            here = d[l, r, c]
            enter = self.layer_cost[l] * cst[l, r, c]
            best, bres = None, INF
            for (dr, dc), sc in zip(DIRS, STEP_COST):
                pr, pc = r - dr, c - dc
                if 0 <= pr < h and 0 <= pc < w:
                    res = abs(d[l, pr, pc] + sc * enter - here)
                    if res < bres:
                        bres, best = res, (l, pr, pc)
            if fv[r, c]:
                for l2 in range(L):
                    if l2 != l:
                        res = abs(d[l2, r, c] + self.via_cost + cst[l, r, c] - here)
                        if res < bres:
                            bres, best = res, (l2, r, c)
            if best is None or bres > tol * max(1.0, here) or d[best] >= here:
                return None
            path.append(best)
            l, r, c = best
        path.reverse()
        return path


def simplify(path):
    """Cell path -> steps ('via', r, c) and ('seg', l, r1, c1, r2, c2); same as maze_route."""
    out = []
    i = 0
    while i < len(path) - 1:
        ln, r, c = path[i]
        j = i + 1
        if path[j][0] != ln:
            out.append(('via', r, c))
            i = j
            continue
        dr = path[j][1] - r
        dc = path[j][2] - c
        while (j + 1 < len(path) and path[j + 1][0] == ln
               and path[j + 1][1] - path[j][1] == dr
               and path[j + 1][2] - path[j][2] == dc):
            j += 1
        out.append(('seg', ln, r, c, path[j][1], path[j][2]))
        i = j
    return out


if __name__ == '__main__':
    # Self-test on a toy field: a wall with gaps, the second layer is expensive.
    import time
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    L, h, w = 2, 200, 300
    cost = torch.ones((L, h, w), device=dev)
    cost[0, 50:150, 150] = INF           # wall on layer 0 with gaps above and below
    free_via = torch.zeros((h, w), dtype=torch.bool, device=dev)
    free_via[::7, ::7] = True
    wv = Wave([1.0, 1.8], 12.0, dev)
    t = time.time()
    dist, goal, it = wv.run(cost, free_via, [(0, 100, 20)], [(0, 100, 280)])
    p = wv.backtrace(dist, cost, free_via, goal)
    steps = simplify(p)
    print('iterations %d, %.2f s, path %d cells, steps %d, vias %d, cost %.1f'
          % (it, time.time() - t, len(p), len(steps),
             sum(1 for s in steps if s[0] == 'via'), float(dist[goal])))
