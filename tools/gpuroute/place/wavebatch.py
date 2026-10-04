"""Batched waves: reachability and routing over many windows at once (leading dimension B).

The numbers are the same as wave.Wave (Bellman-Ford relaxation, cost of entering a cell = layer
cost x cell cost (x sqrt2 diagonally), layer change = via_cost + cell cost on the new layer, only
where free_via). A batch in plain torch is not faster per cell than single windows (memory bound),
but it removes the Python overhead of hundreds of small windows — for L0 (51x51 x 30 iterations)
that is exactly the gain.
"""
import math

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


class BatchWave:
    def __init__(self, layer_cost, via_cost, device='cuda'):
        self.lc = torch.tensor(list(layer_cost), dtype=torch.float32, device=device).view(1, -1, 1, 1)
        self.via_cost = float(via_cost)
        self.dev = device

    def reach(self, cost, free_via, starts, iters=30):
        """cost (B,L,h,w) float with inf where blocked; free_via (B,h,w) bool; starts — LongTensor (4,N) [b,l,r,c].
        Returns (B,) the number of reachable cells (finite dist > 0) within iters iterations."""
        B, L, h, w = cost.shape
        dist = torch.full((B, L, h, w), INF, device=self.dev)
        if starts.numel():
            dist[starts[0], starts[1], starts[2], starts[3]] = 0.0
        lc_cost = self.lc * cost
        via_c = torch.where(free_via, torch.full_like(free_via, self.via_cost, dtype=torch.float32),
                            torch.full_like(free_via, INF, dtype=torch.float32)).unsqueeze(1) + cost
        for _ in range(iters):
            new = dist
            for (dr, dc), sc in zip(DIRS, STEP_COST):
                new = torch.minimum(new, shift(dist, dr, dc) + sc * lc_cost)
            new = torch.minimum(new, dist.min(dim=1, keepdim=True).values + via_c)
            if not bool((new < dist - 1e-4).any()):
                dist = new
                break
            dist = new
        return (torch.isfinite(dist) & (dist > 0)).flatten(1).sum(dim=1)

    def run(self, cost, free_via, starts, goals, max_iter=4000, eps=1e-4):
        """Batched Dijkstra-style relaxation with early exit per batch element.
        starts/goals — LongTensor (4,N) [b,l,r,c]. Returns (dist (B,L,h,w), reached (B,) bool)."""
        B, L, h, w = cost.shape
        dist = torch.full((B, L, h, w), INF, device=self.dev)
        dist[starts[0], starts[1], starts[2], starts[3]] = 0.0
        lc_cost = self.lc * cost
        via_c = torch.where(free_via, torch.full_like(free_via, self.via_cost, dtype=torch.float32),
                            torch.full_like(free_via, INF, dtype=torch.float32)).unsqueeze(1) + cost
        gmask = torch.zeros((B, L, h, w), dtype=torch.bool, device=self.dev)
        gmask[goals[0], goals[1], goals[2], goals[3]] = True
        active = torch.ones(B, dtype=torch.bool, device=self.dev)
        for it in range(max_iter):
            new = dist
            for (dr, dc), sc in zip(DIRS, STEP_COST):
                new = torch.minimum(new, shift(dist, dr, dc) + sc * lc_cost)
            new = torch.minimum(new, dist.min(dim=1, keepdim=True).values + via_c)
            changed = new < dist - eps
            dist = torch.where(active.view(B, 1, 1, 1), new, dist)
            anyc = changed.flatten(1).any(dim=1)
            gd = torch.where(gmask, dist, torch.full_like(dist, INF)).flatten(1).min(dim=1).values
            minc = torch.where(changed, dist, torch.full_like(dist, INF)).flatten(1).min(dim=1).values
            done = (~anyc) | (torch.isfinite(gd) & (minc >= gd))
            active &= ~done
            if not bool(active.any()):
                break
        gd = torch.where(gmask, dist, torch.full_like(dist, INF)).flatten(1).min(dim=1).values
        return dist, torch.isfinite(gd)
