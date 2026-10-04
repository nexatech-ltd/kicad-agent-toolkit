"""Scoring of a placement candidate: L0 — locked ends (cheap), L1 — negotiated routing of the pocket
escapes to the window ring (more expensive, for the best candidates).

F = 1000*unrouted(L1) + 300*locked(L0) + 50*overuse(L1) + 200*plane pads without room for a via
    + 20*sum(max(0, d_decoupling - 3)) + 8*vias + 1*length_mm + 5*sum(|move|)
"""
import math

import numpy as np
import torch

import maze_route as MR
import drcinfo as D
import negotiate as NG
from locked import LOCKED_CELLS
import numpy as np  # noqa: F811 (explicit: used in l1_score)
from wave import Wave

W_UNROUTED, W_LOCKED, W_OVERUSE, W_PLANE, W_LEASH, W_VIA, W_LEN, W_MOVE = 1000, 300, 50, 200, 20, 8, 1.0, 5
# "Tightness" of an end: shortfall of reachability below REACH_TARGET cells (within 4 mm) — a soft
# F0 term that gives the annealer a gradient once no pins are locked (L0 = 0) but L1 still fails
W_TIGHT, REACH_TARGET = 50.0, 400


def ring_mask(win, width=2):
    """(h,w) bool — a ring of `width` cells along the window border."""
    m = torch.zeros((win.h, win.w), dtype=torch.bool)
    m[:width, :] = True; m[-width:, :] = True; m[:, :width] = True; m[:, -width:] = True
    return m


def l0_score(pocket, cm, placement, ends=None):
    """(number locked, list of locked, tightness sum(max(0, 1 - reach/REACH_TARGET)))."""
    ends = ends if ends is not None else pocket.ends_of(placement)
    allr = pocket.l0_reach(cm, ends)
    locked = [e for e in allr if e[5] < LOCKED_CELLS]
    tight = sum(max(0.0, 1.0 - e[5] / REACH_TARGET) for e in allr)
    return len(locked), locked, tight


PLANE_VIA_NEAR_MM = 1.0   # a kept same-net plane via (exposed pad) within this distance = connected
PLANE_VIA_R = 1.0         # L1 via job: a via into the plane no further than this from the plane pad


def plane_pads_without_via(pocket, cm, placement):
    """Plane-net pads on ICs/movable parts with no connection to the plane: neither a kept same-net
    via within PLANE_VIA_NEAR_MM (exposed pad with thermal vias, decoupling cap at its own via)
    nor free room for a new via within 0.6 mm."""
    full = dict(pocket.base_placement)
    full.update(placement or {})
    k = int(round(0.6 / pocket.win.step))
    n = 0
    for r in pocket.ic_refs + pocket.movable:
        fp = pocket.fps[r]
        x, y, rot, fl = full.get(r, (fp['x'], fp['y'], fp['rot'], fp['flipped']))
        th = math.radians(rot)
        c, s = math.cos(th), math.sin(th)
        for p in fp['pads']:
            short = p['net'].split('/')[-1]
            if short not in D.PLANE_NETS or p['layers'] == 'THT':
                continue
            lx = -p['cx'] if fl else p['cx']
            bx, by = x + lx * c + p['cy'] * s, y - lx * s + p['cy'] * c
            rr, cc = pocket.win_row(by), pocket.win_col(bx)
            if not (0 <= rr < pocket.win.h and 0 <= cc < pocket.win.w):
                continue
            if any(math.hypot(vx - bx, vy - by) <= PLANE_VIA_NEAR_MM for (vx, vy) in cm.plane_vias.get(p['net'], ())):
                continue
            fv = cm.free_for(p['net'])[1]
            sub = fv[max(0, rr - k):rr + k + 1, max(0, cc - k):cc + k + 1]
            if not bool(sub.any()):
                n += 1
    return n


def _relabel_border(lab_np, ncomp, border_label):
    """Own-copper components touching the window border (slices of fixed-footprint pads from the
    band around the window) are not chain links: they get border_label (the ring for signal nets,
    0 for planes), the rest are renumbered 1..m. Returns (lab_np, m)."""
    border = set(np.unique(lab_np[:, 0, :])) | set(np.unique(lab_np[:, -1, :])) \
        | set(np.unique(lab_np[:, :, 0])) | set(np.unique(lab_np[:, :, -1]))
    border.discard(0)
    if not border:
        return lab_np, ncomp
    # renumbering table: k -> border_label or a new number 1..m (one pass over the array)
    table = np.zeros(ncomp + 1, dtype=lab_np.dtype)
    m = 0
    for k in range(1, ncomp + 1):
        if k in border:
            table[k] = border_label
        else:
            m += 1
            table[k] = m
    return table[lab_np], m


def l1_score(pocket, cm, placement, iters=4, log=lambda *a: None):
    """Negotiated routing of the pocket nets (negotiate on the candidate model).

    For every signal net with ends in the window: the components of its own copper (net pads in
    the window, 26-neighbour connectivity across layers) are joined by a chain of breaks; a net
    that leaves the window (pocket.outside_nets) is additionally escaped to the window ring (label
    RING = free ring cells for its class). Local nets (e.g. a pin to its own capacitor) are not
    pulled to the ring.
    Returns dict(unrouted, escapes, escapes_failed, overuse, length_mm, vias, paths{net: [path]})."""
    import scipy.ndimage as ndi
    win, dev = pocket.win, pocket.dev
    ring = ring_mask(win).to(dev)
    nets = []
    for (net, r, c, ref, num) in pocket.ends_of(placement):
        if net not in nets:
            nets.append(net)
    struct = np.ones((3, 3, 3), dtype=bool)
    jobs, esc_jobs = [], set()
    for net in nets:
        own = cm.own[net]
        lab_np, ncomp = ndi.label(own.cpu().numpy(), structure=struct)
        if ncomp == 0:
            continue
        # pad slices on the window border belong to the "outside world": ring label
        RING = ncomp + 1
        lab_np, ncomp = _relabel_border(lab_np, ncomp, RING)
        if ncomp == 0:
            continue
        lab = torch.from_numpy(lab_np.astype(np.int32)).to(dev)
        free_ring = ring[None].expand_as(own) & cm.free_for(net)[0] & ~own
        lab[free_ring] = RING
        cm.labels[net] = lab.cpu().numpy()
        cent = {}
        for k in range(1, ncomp + 1):
            idx = np.argwhere(lab_np == k)
            cent[k] = (cm.g.x(float(idx[:, 2].mean())), cm.g.y(float(idx[:, 1].mean())))
        breaks = []
        for k in range(1, ncomp):
            (ax, ay), (bx, by) = cent[k], cent[k + 1]
            breaks.append((k, k + 1, math.hypot(ax - bx, ay - by), (cent[k], cent[k + 1])))
        if net in pocket.outside_nets:
            (ax, ay) = cent[1]
            r, c = cm.g.row(ay), cm.g.col(ax)
            d_top, d_bot, d_left, d_right = r, win.h - 1 - r, c, win.w - 1 - c
            dmin = min(d_top, d_bot, d_left, d_right)
            if dmin == d_top:
                rr, cc = 0, c
            elif dmin == d_bot:
                rr, cc = win.h - 1, c
            elif dmin == d_left:
                rr, cc = r, 0
            else:
                rr, cc = r, win.w - 1
            breaks.append((1, RING, dmin * win.step, ((ax, ay), (cm.g.x(cc), cm.g.y(rr)))))
            esc_jobs.add(net)
        if breaks:
            jobs.append(NG.Job(net, breaks))
    # Plane pads (of ICs and movable parts) without a kept same-net via nearby become via jobs:
    # from the pad to a cell of its net's plane layer within PLANE_VIA_R where a via fits (the
    # path must change layer, i.e. place a via). Otherwise ripped plane vias of the window would be free.
    full = dict(pocket.base_placement)
    full.update(placement or {})
    plane_pts = {}
    for r in pocket.ic_refs + pocket.movable:
        fp = pocket.fps[r]
        x, y, rot, fl = full.get(r, (fp['x'], fp['y'], fp['rot'], fp['flipped']))
        th = math.radians(rot)
        c, s = math.cos(th), math.sin(th)
        for p in fp['pads']:
            if p['net'].split('/')[-1] not in D.PLANE_NETS or p['layers'] == 'THT' or p['net'] not in pocket.plane_layers:
                continue
            lx = -p['cx'] if fl else p['cx']
            bx, by = x + lx * c + p['cy'] * s, y - lx * s + p['cy'] * c
            rr, cc = pocket.win_row(by), pocket.win_col(bx)
            if not (0 <= rr < win.h and 0 <= cc < win.w):
                continue
            if any(math.hypot(vx - bx, vy - by) <= PLANE_VIA_NEAR_MM for (vx, vy) in cm.plane_vias.get(p['net'], ())):
                continue
            plane_pts.setdefault(p['net'], []).append((bx, by, rr, cc))
    via_jobs = set()
    k = int(round(PLANE_VIA_R / win.step))
    for net, pts in plane_pts.items():
        own = cm.own[net]
        lab_np, ncomp = ndi.label(own.cpu().numpy(), structure=struct)
        lab_np, ncomp = _relabel_border(lab_np, ncomp, 0)
        if ncomp == 0:
            continue
        lab = torch.from_numpy(lab_np.astype(np.int32)).to(dev)
        G = ncomp + 1
        free_t, fv = cm.free_for(net)
        goal = torch.zeros_like(own)
        breaks = []
        for (bx, by, rr, cc) in pts:
            la = int(lab_np[:, rr, cc].max())
            if la == 0:
                sub = lab_np[:, max(0, rr - 1):rr + 2, max(0, cc - 1):cc + 2]
                la = int(sub.max())
            if la == 0:
                continue
            r0, r1 = max(0, rr - k), min(win.h - 1, rr + k)
            c0, c1 = max(0, cc - k), min(win.w - 1, cc + k)
            for li in pocket.plane_layers[net]:
                goal[li, r0:r1 + 1, c0:c1 + 1] |= fv[r0:r1 + 1, c0:c1 + 1] & free_t[li, r0:r1 + 1, c0:c1 + 1]
            breaks.append((la, G, PLANE_VIA_R, ((bx, by), (bx, by))))
        goal &= ~own
        lab[goal] = G
        cm.labels[net] = lab.cpu().numpy()
        if breaks:
            job = NG.Job(net, breaks)
            job.must = True
            jobs.append(job)
            via_jobs.add(net)
    ok, conflicted = NG.negotiate(cm, jobs, iters=iters, log=log)
    unrouted = sum(j.fail for j in jobs)
    esc_failed = sum(1 for j in jobs if j.net in esc_jobs and j.fail)
    via_breaks = sum(len(j.breaks) for j in jobs if j.net in via_jobs)
    via_failed = sum(j.fail for j in jobs if j.net in via_jobs)
    length = vias = 0.0
    paths, failed = {}, []
    for j in jobs:
        if j.fail:
            failed.append((j.net.split('/')[-1], j.fail, len(j.breaks)))
        for p in j.paths:
            length += sum(1.0 if (a[1] == b[1] or a[2] == b[2]) else math.sqrt(2) for a, b in zip(p, p[1:]) if a[0] == b[0]) * win.step
            vias += sum(1 for a, b in zip(p, p[1:]) if a[0] != b[0])
        if j.paths:
            paths[j.net] = j.paths
    return dict(unrouted=unrouted, escapes=len(esc_jobs), escapes_failed=esc_failed, overuse=0,
                length_mm=length, vias=vias, paths=paths, failed=failed, nets=len(jobs),
                breaks=sum(len(j.breaks) for j in jobs), via_breaks=via_breaks, via_failed=via_failed)


def total(pocket, cm, placement, l0_locked, l1=None, leash_excess=0.0, tight=0.0):
    move = 0.0
    for r, (x, y, rot, fl) in (placement or {}).items():
        bx, by = pocket.base_placement[r][0], pocket.base_placement[r][1]
        move += math.hypot(x - bx, y - by)
    F = (W_LOCKED * l0_locked + W_TIGHT * tight + W_PLANE * plane_pads_without_via(pocket, cm, placement)
         + W_LEASH * leash_excess + W_MOVE * move)
    if l1:
        F += W_UNROUTED * l1['unrouted'] + W_OVERUSE * l1['overuse'] + W_VIA * l1['vias'] + W_LEN * l1['length_mm']
    return F
