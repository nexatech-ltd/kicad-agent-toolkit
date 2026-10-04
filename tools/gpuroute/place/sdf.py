#!/usr/bin/env python3
"""Distance-to-copper maps on the GPU — analytic, instead of a 0.025 mm raster + EDT on the CPU.

Per 0.1 mm grid cell and layer the router needs one number: the distance to the nearest foreign
copper of a given group (normal / strict-clearance net class, e.g. 0.25 or 0.30 mm / keepout /
drill), which is then compared with the threshold w/2 + clearance + SAFE_MM. Rounded rectangle,
capsule and circle have closed-form signed distances; each primitive computes a KxK cell patch
around its centre, and the patches are merged into the map with scatter_reduce(amin). Anything
beyond CAP_MM is irrelevant to the router.

Measured on a single GPU: 2000 pads — 2.5 ms, 8000 primitives — 16.7 ms; a candidate delta
(<= 60 moved pads) costs about one kernel launch, ~3 ms, so batching candidates is nearly free.

Primitives are stored as a table of tensors (PrimTable): kind, layer, cx, cy (mm, board), hw, hh,
r, cos(theta), sin(theta), group. Polygons (POLY) are computed separately edge by edge — there are
few of them (custom-shape pads).
"""
import math

import torch
import torch.nn.functional as F

import project
import raster

INF = float('inf')
CAP_MM = 0.9
KIND_RRECT, KIND_CIRCLE, KIND_CAPSULE, KIND_POLY = 0, 1, 2, 3
LAYERS = raster.SIGNAL          # the same list (alias) as the raster: 6 or 8 layers


class Window:
    """Routing-grid window: rows r0..r1, columns c0..c1 (inclusive), pitch step, board origin x0/y0."""

    def __init__(self, g, r0, r1, c0, c1):
        self.step = g.step
        self.x0, self.y0 = g.x0, g.y0
        self.r0, self.r1, self.c0, self.c1 = r0, r1, c0, c1
        self.h, self.w = r1 - r0 + 1, c1 - c0 + 1

    @classmethod
    def full(cls, g):
        return cls(g, 0, g.h - 1, 0, g.w - 1)

    @classmethod
    def around(cls, g, x, y, half_mm):
        k = int(round(half_mm / g.step))
        r, c = g.row(y), g.col(x)
        return cls(g, max(0, r - k), min(g.h - 1, r + k), max(0, c - k), min(g.w - 1, c + k))

    def xy(self, dev):
        """Cell-centre coordinates of the window (h,w) in mm."""
        ys = self.y0 + (self.r0 + torch.arange(self.h, device=dev, dtype=torch.float32)) * self.step
        xs = self.x0 + (self.c0 + torch.arange(self.w, device=dev, dtype=torch.float32)) * self.step
        return xs, ys


class PrimTable:
    """Flat primitive table on the device. One primitive = one row per layer."""

    FIELDS = ('kind', 'layer', 'cx', 'cy', 'hw', 'hh', 'r', 'cs', 'sn', 'group', 'plane_via', 'x1', 'y1', 'x2', 'y2')

    def __init__(self, dev):
        self.dev = dev
        self.rows = []          # python list until finalize()
        self.polys = []         # (layer, group, tensor (N,2)) for POLY
        self.t = None

    def add(self, kind, layers, cx, cy, hw, hh, r, theta_deg=0.0, group=0, poly=None, plane_via=0, ends=None):
        """ends — (x1, y1, x2, y2) of a track segment (for the "rip if both ends are in the window" rule);
        for pads/vias — the centre."""
        c, s = math.cos(math.radians(theta_deg)), math.sin(math.radians(theta_deg))
        x1, y1, x2, y2 = ends if ends is not None else (cx, cy, cx, cy)
        for L in layers:
            if kind == KIND_POLY and poly is not None:
                self.polys.append((L, group, poly))
            self.rows.append((kind, L, cx, cy, hw, hh, r, c, s, group, plane_via, x1, y1, x2, y2))

    def finalize(self):
        if not self.rows:
            self.t = {f: torch.zeros(0, device=self.dev) for f in self.FIELDS}
            return self
        a = torch.tensor(self.rows, dtype=torch.float32, device=self.dev)
        self.t = {f: a[:, i] for i, f in enumerate(self.FIELDS)}
        self.t['kind'] = self.t['kind'].long()
        self.t['layer'] = self.t['layer'].long()
        self.t['group'] = self.t['group'].long()
        self.t['plane_via'] = self.t['plane_via'].long()
        return self

    def __len__(self):
        return 0 if self.t is None else int(self.t['cx'].numel())

    def select(self, mask, poly_keep=None):
        """Sub-table by row mask; poly_keep(group) -> bool selects polygons (all of them otherwise)."""
        out = PrimTable(self.dev)
        out.t = {f: v[mask] for f, v in self.t.items()}
        out.polys = [p for p in self.polys if poly_keep is None or poly_keep(p[1])]
        return out

    def shifted(self, dx, dy):
        """Copy with all primitives shifted (for a candidate: moved pads)."""
        out = PrimTable(self.dev)
        out.t = dict(self.t)
        out.t['cx'] = self.t['cx'] + dx
        out.t['cy'] = self.t['cy'] + dy
        out.polys = list(self.polys)
        return out


def cat(a: PrimTable, b: PrimTable):
    """Union of two tables (same device)."""
    out = PrimTable(a.dev)
    if a.t is None or len(a) == 0:
        out.t = dict(b.t) if b.t is not None else None
    elif b.t is None or len(b) == 0:
        out.t = dict(a.t)
    else:
        out.t = {f: torch.cat([a.t[f], b.t[f]]) for f in PrimTable.FIELDS}
    out.polys = list(a.polys) + list(b.polys)
    return out


def _sdf_local(kind, px, py, hw, hh, r):
    """Signed distance in the local frame of the primitive (px, py already rotated). All tensors are (N,K,K)."""
    d_rr = torch.hypot(torch.clamp(px.abs() - (hw - r), min=0), torch.clamp(py.abs() - (hh - r), min=0)) \
        + torch.clamp(torch.maximum(px.abs() - (hw - r), py.abs() - (hh - r)), max=0) - r
    d_circ = torch.hypot(px, py) - r
    horiz = hw >= hh
    rc = torch.minimum(hw, hh)
    Lx = torch.clamp(hw - rc, min=0)
    Ly = torch.clamp(hh - rc, min=0)
    cx = torch.clamp(px, -Lx, Lx)
    cy = torch.clamp(py, -Ly, Ly)
    d_cap = torch.where(horiz, torch.hypot(px - cx, py) - rc, torch.hypot(px, py - cy) - rc)
    d = torch.where(kind == KIND_RRECT, d_rr, torch.where(kind == KIND_CIRCLE, d_circ, d_cap))
    return d


def dist_map(prims: PrimTable, win: Window, group=None, cap=CAP_MM, out=None):
    """Map (layers, h, w) of distances to the primitives of a group (all groups if None); values >= cap are not exact.

    out — an existing map to merge into by minimum (e.g. the static base map)."""
    dev = prims.dev
    if out is None:
        out = torch.full((len(LAYERS), win.h, win.w), INF, device=dev)
    if len(prims) == 0:
        return out
    t = prims.t
    sel = torch.ones_like(t['cx'], dtype=torch.bool) if group is None else (t['group'] == group)
    sel &= t['kind'] != KIND_POLY
    if bool(sel.any()):
        kind, layer = t['kind'][sel], t['layer'][sel]
        cx, cy, hw, hh, r, cs, sn = (t[k][sel] for k in ('cx', 'cy', 'hw', 'hh', 'r', 'cs', 'sn'))
        # patch radius: half-diagonal + cap, in cells; primitives are grouped by K (ceil)
        reach = torch.sqrt(hw * hw + hh * hh) + cap
        kcells = torch.ceil(reach / win.step).long()
        for K in torch.unique(kcells).tolist():
            m = kcells == K
            n = int(m.sum())
            ccol = torch.round((cx[m] - win.x0) / win.step).long() - win.c0     # centre column in the window
            crow = torch.round((cy[m] - win.y0) / win.step).long() - win.r0
            off = torch.arange(-K, K + 1, device=dev)
            rows = crow[:, None, None] + off[None, :, None]        # (n, 2K+1, 1)
            cols = ccol[:, None, None] + off[None, None, :]        # (n, 1, 2K+1)
            rows = rows.expand(n, 2 * K + 1, 2 * K + 1)
            cols = cols.expand(n, 2 * K + 1, 2 * K + 1)
            X = win.x0 + (cols + win.c0).float() * win.step
            Y = win.y0 + (rows + win.r0).float() * win.step
            dx, dy = X - cx[m][:, None, None], Y - cy[m][:, None, None]
            c_, s_ = cs[m][:, None, None], sn[m][:, None, None]
            # into the local frame of the primitive: inverse of board = pos + R(theta)*local (see geom.to_local)
            px = dx * c_ - dy * s_
            py = dx * s_ + dy * c_
            d = _sdf_local(kind[m][:, None, None], px, py, hw[m][:, None, None], hh[m][:, None, None], r[m][:, None, None])
            inside = (rows >= 0) & (rows < win.h) & (cols >= 0) & (cols < win.w)
            lay = layer[m][:, None, None].expand_as(rows)
            idx = (lay * win.h + rows) * win.w + cols
            flat = out.view(-1)
            flat.scatter_reduce_(0, idx[inside], d[inside], reduce='amin')
    # polygons: distance to the edges, sign by the even-odd rule, only within their extent + cap
    for (L, grp, poly) in prims.polys:
        if group is not None and grp != group:
            continue
        P = poly.to(dev)
        x0, y0 = float(P[:, 0].min()) - cap, float(P[:, 1].min()) - cap
        x1, y1 = float(P[:, 0].max()) + cap, float(P[:, 1].max()) + cap
        c0 = max(0, int(math.floor((x0 - win.x0) / win.step)) - win.c0)
        c1 = min(win.w - 1, int(math.ceil((x1 - win.x0) / win.step)) - win.c0)
        r0 = max(0, int(math.floor((y0 - win.y0) / win.step)) - win.r0)
        r1 = min(win.h - 1, int(math.ceil((y1 - win.y0) / win.step)) - win.r0)
        if c1 < c0 or r1 < r0:
            continue
        xs = win.x0 + (torch.arange(c0, c1 + 1, device=dev) + win.c0).float() * win.step
        ys = win.y0 + (torch.arange(r0, r1 + 1, device=dev) + win.r0).float() * win.step
        X, Y = torch.meshgrid(xs, ys, indexing='xy')
        A, B = P, torch.roll(P, -1, 0)
        ex, ey = (B - A)[:, 0], (B - A)[:, 1]
        L2 = ex * ex + ey * ey
        dxp = X[..., None] - A[:, 0]
        dyp = Y[..., None] - A[:, 1]
        tt = torch.clamp((dxp * ex + dyp * ey) / L2, 0, 1)
        dist = torch.hypot(dxp - tt * ex, dyp - tt * ey).min(dim=-1).values
        cond = ((A[:, 1] <= Y[..., None]) & (Y[..., None] < B[:, 1])) | ((B[:, 1] <= Y[..., None]) & (Y[..., None] < A[:, 1]))
        xi = A[:, 0] + (Y[..., None] - A[:, 1]) * ex / torch.where(ey == 0, torch.ones_like(ey), ey)
        inside = ((cond & (X[..., None] < xi)).sum(dim=-1) % 2) == 1
        d = torch.where(inside, -dist, dist)
        out[L, r0:r1 + 1, c0:c1 + 1] = torch.minimum(out[L, r0:r1 + 1, c0:c1 + 1], d)
    # inside copper — 0, as with the EDT of the CPU model (the router has no use for negative values)
    return out.clamp_(min=0)


def dist_map_grouped(prims: PrimTable, win: Window, gids, cap=CAP_MM):
    """(G, layers, h, w): distance map to the primitives of EACH group gids[g] separately, in one patch pass
    (same geometry as dist_map; scatter_reduce(amin) over an index with a group axis). Groups absent
    from the table are INF. Values >= 0 (clamped), as in dist_map."""
    dev = prims.dev
    G = len(gids)
    out = torch.full((G, len(LAYERS), win.h, win.w), INF, device=dev)
    if len(prims) == 0 and not prims.polys:
        return out
    gpos = {int(g): i for i, g in enumerate(gids)}
    t = prims.t
    if len(prims):
        grp_idx = torch.tensor([gpos.get(int(g), -1) for g in t['group'].tolist()], device=dev, dtype=torch.long)
        sel = (t['kind'] != KIND_POLY) & (grp_idx >= 0)
        if bool(sel.any()):
            kind, layer, gi = t['kind'][sel], t['layer'][sel], grp_idx[sel]
            cx, cy, hw, hh, r, cs, sn = (t[k][sel] for k in ('cx', 'cy', 'hw', 'hh', 'r', 'cs', 'sn'))
            reach = torch.sqrt(hw * hw + hh * hh) + cap
            kcells = torch.ceil(reach / win.step).long()
            flat = out.view(-1)
            for K in torch.unique(kcells).tolist():
                m = kcells == K
                n = int(m.sum())
                ccol = torch.round((cx[m] - win.x0) / win.step).long() - win.c0
                crow = torch.round((cy[m] - win.y0) / win.step).long() - win.r0
                off = torch.arange(-K, K + 1, device=dev)
                rows = (crow[:, None, None] + off[None, :, None]).expand(n, 2 * K + 1, 2 * K + 1)
                cols = (ccol[:, None, None] + off[None, None, :]).expand(n, 2 * K + 1, 2 * K + 1)
                X = win.x0 + (cols + win.c0).float() * win.step
                Y = win.y0 + (rows + win.r0).float() * win.step
                dx, dy = X - cx[m][:, None, None], Y - cy[m][:, None, None]
                c_, s_ = cs[m][:, None, None], sn[m][:, None, None]
                px = dx * c_ - dy * s_
                py = dx * s_ + dy * c_
                d = _sdf_local(kind[m][:, None, None], px, py, hw[m][:, None, None], hh[m][:, None, None], r[m][:, None, None])
                inside = (rows >= 0) & (rows < win.h) & (cols >= 0) & (cols < win.w)
                lay = layer[m][:, None, None].expand_as(rows)
                g_ = gi[m][:, None, None].expand_as(rows)
                idx = ((g_ * len(LAYERS) + lay) * win.h + rows) * win.w + cols
                flat.scatter_reduce_(0, idx[inside], d[inside], reduce='amin')
    for (L, grp, poly) in prims.polys:
        gi = gpos.get(int(grp))
        if gi is None:
            continue
        one = PrimTable(dev)
        one.t = {f: torch.zeros(0, device=dev) for f in PrimTable.FIELDS}
        one.polys = [(L, grp, poly)]
        out[gi] = dist_map(one, win, group=grp, cap=cap, out=out[gi])
    return out.clamp_(min=0)


def excl_min(D):
    """(G, ...) -> (G, ...): excl[g] = min over all groups except g (prefix and suffix minima)."""
    G = D.shape[0]
    if G == 1:
        return torch.full_like(D, INF)
    pre = torch.cummin(D, dim=0).values
    suf = torch.flip(torch.cummin(torch.flip(D, [0]), dim=0).values, [0])
    inf = torch.full_like(D[:1], INF)
    return torch.minimum(torch.cat([inf, pre[:-1]]), torch.cat([suf[1:], inf]))


def dist_maps_delta(base: dict, cand: PrimTable, win: Window, groups):
    """min(base[g], candidate map) per group — for moved primitives."""
    return {g: dist_map(cand, win, group=g, out=base[g].clone()) for g in groups}


def cover(prims: PrimTable, win: Window, cap=0.2):
    """(layers,h,w) bool: a cell is occupied if copper is in any of its sub-cells — like model.block_cover."""
    d = dist_map(prims, win, cap=cap)
    return d <= 0.5 * win.step * math.sqrt(2)


def prims_from_geom(geom, dev, refs=None, placement=None, group_of=None):
    """PrimTable from geom.json: refs — which footprints to take; placement — pose overrides
    {ref: (x, y, rot, flipped)}; group_of(net) -> group id (0 by default)."""
    tab = PrimTable(dev)
    lay_idx = {L: i for i, L in enumerate(LAYERS)}
    for fp in geom['footprints']:
        ref = fp['ref']
        if refs is not None and ref not in refs:
            continue
        x, y, rot, flipped = fp['x'], fp['y'], fp['rot'], fp['flipped']
        if placement and ref in placement:
            x, y, rot, flipped = placement[ref]
        th = math.radians(rot)
        c, s = math.cos(th), math.sin(th)
        for p in fp['pads']:
            lx, ly = p['cx'], p['cy']
            if flipped:
                lx = -lx
            bx = x + lx * c + ly * s
            by = y - lx * s + ly * c
            lay = p['layers']
            NL = len(LAYERS)
            if lay == 'THT':
                layers = list(range(NL))
            elif lay == 'F':
                layers = [NL - 1] if flipped else [0]
            elif lay == 'B':
                layers = [0] if flipped else [NL - 1]
            else:
                layers = [lay_idx[L] for L in lay.split(',') if L in lay_idx]
            grp = group_of(p['net']) if group_of else 0
            kind = {'RRECT': KIND_RRECT, 'CIRCLE': KIND_CIRCLE, 'CAPSULE': KIND_CAPSULE, 'POLY': KIND_POLY}[p['kind']]
            poly = None
            if kind == KIND_POLY:
                pts = []
                for (qx, qy) in p['poly']:
                    if flipped:
                        qx = -qx
                    pts.append((x + qx * c + qy * s, y - qx * s + qy * c))
                poly = torch.tensor(pts, dtype=torch.float32)
            # the primitive rotates with the footprint; mirroring flips the sign of the angle
            tab.add(kind, layers, bx, by, p['hw'], p['hh'], p['r'], -rot if flipped else rot, grp, poly)
    return tab.finalize()


PLANE_NETS = project.plane_nets()


def prims_from_tracks(board, dev, group_of=None, skip_nets=(), mark_plane_via=False, basket=None):
    """Board tracks (capsules) and vias (circles) as primitives; plane_via=1 on vias of plane nets
    (they are kept when the window copper is ripped). basket (raster.Basket) avoids walking SWIG."""
    import pcbnew
    import par
    import raster
    MM = 1e6
    tab = PrimTable(dev)
    lay_idx = {L: i for i, L in enumerate(LAYERS)}
    if basket is None and par.enabled():
        basket = raster.index_for(board)
    if basket is not None:
        for net, lst in basket.vias.items():
            if net in skip_nets:
                continue
            grp = group_of(net) if group_of else 0
            pv = 1 if (mark_plane_via and net.split('/')[-1] in PLANE_NETS) else 0
            for (x, y, drill, wl) in lst:
                layers = [lay_idx[L] for L in LAYERS if L in wl]
                w = wl.get('F.Cu', next(iter(wl.values())) if wl else 0.0)
                tab.add(KIND_CIRCLE, layers, x, y, w / 2, w / 2, w / 2, 0.0, grp, plane_via=pv)
        for net, byL in basket.tracks.items():
            if net in skip_nets:
                continue
            grp = group_of(net) if group_of else 0
            for L, segs in byL.items():
                if L not in lay_idx:
                    continue
                for (x1, y1, x2, y2, w) in segs:
                    length = math.hypot(x2 - x1, y2 - y1)
                    ang = math.degrees(math.atan2(-(y2 - y1), x2 - x1))
                    tab.add(KIND_CAPSULE, [lay_idx[L]], (x1 + x2) / 2, (y1 + y2) / 2, length / 2 + w / 2, w / 2, w / 2, ang, grp,
                            ends=(x1, y1, x2, y2))
        return tab.finalize()
    for t in board.GetTracks():
        net = t.GetNetname()
        if net in skip_nets:
            continue
        grp = group_of(net) if group_of else 0
        if t.GetClass() == 'PCB_VIA':
            v = t.Cast()
            layers = [i for i, L in enumerate(LAYERS) if v.IsOnLayer(board.GetLayerID(L))]
            try:
                w = v.GetWidth(board.GetLayerID('F.Cu')) / MM
            except TypeError:
                w = v.GetWidth() / MM
            p = v.GetPosition()
            pv = 1 if (mark_plane_via and net.split('/')[-1] in PLANE_NETS) else 0
            tab.add(KIND_CIRCLE, layers, p.x / MM, p.y / MM, w / 2, w / 2, w / 2, 0.0, grp, plane_via=pv)
            continue
        L = board.GetLayerName(t.GetLayer())
        if L not in lay_idx:
            continue
        s, e = t.GetStart(), t.GetEnd()
        x1, y1, x2, y2 = s.x / MM, s.y / MM, e.x / MM, e.y / MM
        w = t.GetWidth() / MM
        length = math.hypot(x2 - x1, y2 - y1)
        ang = math.degrees(math.atan2(-(y2 - y1), x2 - x1))   # y axis down: angle in the KiCad convention
        tab.add(KIND_CAPSULE, [lay_idx[L]], (x1 + x2) / 2, (y1 + y2) / 2, length / 2 + w / 2, w / 2, w / 2, ang, grp,
                ends=(x1, y1, x2, y2))
    return tab.finalize()
