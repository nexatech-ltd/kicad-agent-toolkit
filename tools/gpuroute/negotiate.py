"""PathFinder negotiation between nets competing for board space.

Each net is routed by the wavefront over a cost in which other nets' paths of the current round
are not a wall but a surcharge: present = 1 + pres * (number of foreign cells in the conflict
zone). After a round, cells where copper of one net fell into the clearance of another
(overuse) get a history increment; the next round re-routes only conflicting nets, and pres
grows. Converged = no overuse. Fixed copper (foreign nets and copper the participants already
have) stays a wall: this is incremental mode.
"""
import math
import os
import time

import torch

import maze_route as MR
from wave import Wave, simplify

LAYERS = MR.raster.SIGNAL


class Job:
    """One net in the negotiation: its breaks (component pairs) and the paths found."""

    def __init__(self, net, breaks):
        self.net = net
        self.breaks = breaks          # list of (label_a, label_b, length mm, ((ax,ay),(bx,by)))
        self.paths = []               # list of paths (cells of the full grid)
        self.bare = self.dil = None
        self.fail = 0                 # breaks without a path
        self.length = sum(b[2] for b in breaks)
        self.must = False             # ripped-up net: must be reconnected (ripup)


def _window(g, idx_lists, margin_cells):
    rs = torch.cat([i[1] for i in idx_lists])
    cs = torch.cat([i[2] for i in idx_lists])
    r0 = max(0, int(rs.min()) - margin_cells)
    r1 = min(g.h - 1, int(rs.max()) + margin_cells)
    c0 = max(0, int(cs.min()) - margin_cells)
    c1 = min(g.w - 1, int(cs.max()) + margin_cells)
    return r0, r1, c0, c1


def route_job(model, wave, job, usage_bare, usage_dil, history, pres, hist_w, log, hard=False):
    """Route all breaks of a net over the current usage. Returns the number of failures."""
    g = model.g
    net = job.net
    dev = model.dev
    lab = torch.from_numpy(model.labels[net]).to(dev)
    # union of components as paths are laid
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    # negotiate() has already subtracted this net's previous copper from usage_*; subtracting it
    # a second time here drove usage negative, the cost became negative, and the wave spun for
    # 8000 iterations with a cost of -1.6e6 (seen during a rip-up).
    ub_o = usage_bare.clamp_min(0.0)
    ud_o = usage_dil.clamp_min(0.0)
    own_extra = torch.zeros_like(model.own[net])
    job.paths, job.fail = [], 0
    margin = int(MR.MARGIN / g.step)
    for (la, lb, dist_mm, pos) in job.breaks:
        # lb may be a set of labels: "any other component of the net in the window" (planes as
        # signals, route.py --any-target) - the wave goes to the nearest one, not the one DRC named
        multi = isinstance(lb, (tuple, list, set, frozenset))
        lbs = list(lb) if multi else [lb]
        ra = find(la)
        rbs = {find(x) for x in lbs} - {ra}
        if not rbs:
            continue
        cost, fv = model.cost_for(net, ub_o, ud_o, history, pres, hist_w, own_extra, hard=hard)
        allk = set(parent) | {la} | set(lbs)
        grp_a = [k for k in allk if find(k) == ra] or [la]
        grp_b = [k for k in allk if find(k) in rbs] or lbs
        # The window is the BREAK ends plus a margin, not the components' extent: a component with
        # a long track blew the window up to half the board (504x1135 cells for a 1 mm break).
        # Component cells outside the window simply do not take part as starts and goals.
        # KAT_MULTI_R: window margin, mm, for multi-target (any-target) breaks (default 6.0).
        (ax, ay), (bx, by) = pos
        mrg = int(float(os.environ.get('KAT_MULTI_R', '6.0')) / g.step) if multi else margin
        rs = (g.row(ay), g.row(by))
        cs = (g.col(ax), g.col(bx))
        r0, r1 = max(0, min(rs) - mrg), min(g.h - 1, max(rs) + mrg)
        c0, c1 = max(0, min(cs) - mrg), min(g.w - 1, max(cs) + mrg)
        lab_w = lab[:, r0:r1 + 1, c0:c1 + 1]
        sa = torch.nonzero(torch.isin(lab_w, torch.tensor(grp_a, device=dev)), as_tuple=False).T
        sb = torch.nonzero(torch.isin(lab_w, torch.tensor(grp_b, device=dev)), as_tuple=False).T
        if sa.shape[1] == 0 or sb.shape[1] == 0:
            job.fail += 1
            continue
        t = time.time()
        dist, goal, it = wave.run(cost[:, r0:r1 + 1, c0:c1 + 1], fv[r0:r1 + 1, c0:c1 + 1], sa, sb)
        if dist is None:
            log('    %-24s %6.1f mm  NO PATH (%d iterations, window %dx%d, %.1f s)'
                % (net.split('/')[-1][:24], dist_mm, it, r1 - r0 + 1, c1 - c0 + 1, time.time() - t))
            job.fail += 1
            continue
        p = wave.backtrace(dist, cost[:, r0:r1 + 1, c0:c1 + 1], fv[r0:r1 + 1, c0:c1 + 1], goal)
        if p is None:
            log('    %-24s backtrace did not converge' % net.split('/')[-1][:24])
            job.fail += 1
            continue
        path = [(l, r + r0, c + c0) for (l, r, c) in p]
        if len(path) < 2:
            log('    %-24s degenerate path (1 cell) - skipped' % net.split('/')[-1][:24])
            job.fail += 1
            continue
        job.paths.append(path)
        # which target component was reached - by the label of the goal cell
        hit = int(lab_w[goal[0], goal[1], goal[2]]) if multi else lbs[0]
        parent[find(hit) if hit in allk or hit else lbs[0]] = ra
        idx = torch.tensor(path, dtype=torch.long, device=dev).T
        own_extra[idx[0], idx[1], idx[2]] = True
        nvia = sum(1 for a, b in zip(path, path[1:]) if a[0] != b[0])
        log('    %-24s %6.1f mm -> %4d cells, vias %d, cost %.0f, %d iterations, %.1f s'
            % (net.split('/')[-1][:24], dist_mm, len(path), nvia, float(dist[goal]), it,
               time.time() - t))
    # new usage of the net
    allp = [c for p in job.paths for c in p]
    # vias are counted per path (a layer change between two paths is not a via)
    bare = dil = None
    for p in job.paths:
        b, d = model.footprint(p, net)
        bare = b if bare is None else (bare | b)
        dil = d if dil is None else (dil | d)
    if bare is None:
        bare, dil = model.footprint([], net)
    job.bare, job.dil = bare, dil
    return job.fail


def negotiate(model, jobs, iters=10, pres0=0.5, growth=1.7, hist_w=0.5, log=print):
    """PathFinder iterations. Returns (converged, list of conflicting nets)."""
    L, H, W = len(LAYERS), model.g.h, model.g.w
    dev = model.dev
    wave = Wave([MR.LAYER_COST[l] for l in LAYERS], MR.VIA_COST, dev)
    usage_bare = torch.zeros((L, H, W), device=dev)
    usage_dil = torch.zeros((L, H, W), device=dev)
    history = torch.zeros((L, H, W), device=dev)
    todo = sorted(jobs, key=lambda j: j.length)
    conflicted = []
    for it in range(iters):
        pres = pres0 * (growth ** it)
        t0 = time.time()
        log('  negotiation round %d/%d: nets %d, pres %.2f' % (it + 1, iters, len(todo), pres))
        for job in todo:
            if job.bare is not None:
                usage_bare -= job.bare.float()
                usage_dil -= job.dil.float()
            route_job(model, wave, job, usage_bare, usage_dil, history, pres, hist_w, log)
            usage_bare += job.bare.float()
            usage_dil += job.dil.float()
        over = (usage_bare >= 1) & (usage_dil >= 2)
        history += over.float()
        conflicted = []
        for job in jobs:
            if job.bare is None:
                continue
            others_bare = usage_bare - job.bare.float()
            hit = bool((job.bare & (usage_dil >= 2)).any()) or bool((job.dil & (others_bare > 0)).any())
            if hit:
                conflicted.append(job)
        routed = sum(1 for j in jobs if j.paths)
        log('  round %d result: nets routed %d/%d, overused cells %d, conflicting nets %d, %.0f s'
            % (it + 1, routed, len(jobs), int(over.sum()), len(conflicted), time.time() - t0))
        if not conflicted:
            return True, []
        todo = sorted(conflicted, key=lambda j: -j.length)

    # Negotiation did not converge. Finish sequentially: conflicting nets one by one, other
    # paths are a wall (hard). Whatever does not fit is dropped: a net without a path beats a violation.
    log('  negotiation did not converge (%d conflicting) - sequential laying' % len(conflicted))
    # First remove ALL conflicting paths, then lay them one by one: mandatory ones (ripped up)
    # first, short before long; what is already laid is a wall.
    for job in conflicted:
        usage_bare -= job.bare.float()
        usage_dil -= job.dil.float()
        job.paths = []
        job.bare, job.dil = model.footprint([], job.net)
    order = sorted(conflicted, key=lambda j: (0 if j.must else 1, j.length))
    for job in order:
        route_job(model, wave, job, usage_bare, usage_dil, history, 0.0, 0.0, log, hard=True)
        usage_bare += job.bare.float()
        usage_dil += job.dil.float()
    dropped = []
    for job in sorted(jobs, key=lambda j: (1 if j.must else 0, -j.length)):
        if job.bare is None or not job.paths:
            continue
        others_bare = usage_bare - job.bare.float()
        hit = bool((job.bare & (usage_dil >= 2)).any()) or bool((job.dil & (others_bare > 0)).any())
        if hit:
            usage_bare -= job.bare.float()
            usage_dil -= job.dil.float()
            job.paths, job.fail = [], len(job.breaks)
            job.bare, job.dil = model.footprint([], job.net)
            dropped.append(job)
    routed = sum(1 for j in jobs if j.paths)
    log('  after laying: routed %d/%d, conflicting dropped %d' % (routed, len(jobs), len(dropped)))
    return True, []
