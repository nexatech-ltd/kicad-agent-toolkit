"""Board model for the GPU router: obstacles per net class, own copper of the routed nets,
fixed copper of the negotiation neighbours - all as GPU tensors.

The rules are the same as in the maze router (maze_route.Field): clearance by the class of the
copper ITSELF (e.g. 0.25 for a power class, 0.30 for an RF class; see the board's .kicad_dru),
a relaxed escape clearance inside the courtyard of fine-pitch parts, edge inset, keepouts,
hole-to-hole for vias. Plane fills are not obstacles (they pull back on refill), but the net's
own fill is a goal.

Precision. A 0.1 mm raster with square "round-up" dilation locked 0.5 mm-pitch LQFP pins: between
a pin axis and the edge of the neighbouring pad there is 0.325 mm, a 0.2 track at 0.15 clearance
needs 0.25, the 0.15 mm band was lost by the raster (about half of all break ends locked). So
foreign copper is rasterised on a 0.025 mm SUB-RASTER, a Euclidean distance transform (EDT) is
computed from it, and a 0.1 mm routing cell is free if its distance to copper is
>= w/2 + clearance + safety margin. Copper of negotiation participants (which has to be
subtracted per net) is kept on the 0.1 mm grid "by coverage": a cell is occupied if any of its
sub-cells has copper.

Free-space masks are built in build_free(): the same thresholds are applied to distance maps
computed on the CPU (this model) and analytically on the GPU (place/sdf.py, placement
candidates), so the rules are guaranteed identical.

Environment:
    KAT_SAFE_MM          safety margin for copper edges inside a sub-cell, mm (default 0.02)
    KAT_PAD_SAFE_MM      safety margin to pads, mm (default max(KAT_SAFE_MM, 0.03))
    KAT_PLANE_VIA_GUARD  keep-clear belt around plane vias on their plane layers, mm (default 0.40)
    KAT_PAR              threaded CPU stages and the board-item basket (see par.py)
"""
import math
import os

import numpy as np
import pcbnew
import torch
import torch.nn.functional as F
from scipy import ndimage

import raster
import maze_route as MR
import connectivity as CN
import par
import project

LAYERS = raster.SIGNAL
MM = 1e6
INF = float('inf')
FINE = 4                 # sub-raster: 0.1 / 4 = 0.025 mm
SAFE_MM = float(os.environ.get('KAT_SAFE_MM', '0.02'))   # margin for the copper edge position inside a sub-cell: half-diagonal of 0.025 mm = 0.018
PAD_SAFE_MM = float(os.environ.get('KAT_PAD_SAFE_MM', str(max(SAFE_MM, 0.03))))  # KAT_PAD_SAFE_MM: margin to pads (default max(SAFE, 0.03))
SAFE_CELLS = 0.25        # margin (cells) for dilating TRACKS whose axes lie on the grid
W_FINE = 0.15            # track width inside the courtyard of fine-pitch parts (project.fine_pitch_refs)
PAD_CLEARANCE = project.rule_clearance('Track', 'Pad', default=0.15)  # track/via <-> PAD of any class: the track_to_pad rule (.kicad_dru)
PLANE_VIA_GUARD = float(os.environ.get('KAT_PLANE_VIA_GUARD', '0.40'))   # belt around a plane via on ITS layer: zone clearance 0.2 + min fill width 0.2 -
                         # otherwise a foreign track on the plane layer cuts an island around the via (~1 mm2)


def to_t(mask, dev):
    return torch.from_numpy(np.ascontiguousarray(mask)).to(dev)


_KER = {}


def disc_kernel(r, dev):
    """Disc kernel of radius r cells (r fractional): offsets with i^2+j^2 <= r^2."""
    key = (round(r, 3), str(dev))
    if key not in _KER:
        k = int(math.ceil(r))
        ii, jj = torch.meshgrid(torch.arange(-k, k + 1, device=dev),
                                torch.arange(-k, k + 1, device=dev), indexing='ij')
        _KER[key] = ((ii * ii + jj * jj).float() <= r * r + 1e-6).float()[None, None]
    return _KER[key]


def dilate_t(x, r):
    """Dilation by a disc of radius r cells for bool (L,H,W) or (H,W)."""
    if r <= 0:
        return x
    ker = disc_kernel(r, x.device)
    k = ker.shape[-1] // 2
    f = x.float()
    if f.dim() == 2:
        return F.conv2d(f[None, None], ker, padding=k)[0, 0] > 0
    return F.conv2d(f[:, None], ker, padding=k)[:, 0] > 0


def box_count(x, k):
    """Sum of float (L,H,W) over a (2k+1)^2 square around each cell."""
    k = int(math.ceil(k))
    if k <= 0:
        return x
    n = 2 * k + 1
    return F.avg_pool2d(x[:, None], n, 1, k, count_include_pad=True)[:, 0] * (n * n)


def cells(mm, step):
    """Radius in cells (fractional) with a margin for copper whose axis lies on the grid."""
    return mm / step + SAFE_CELLS


def fine_grid(g):
    """Sub-raster with the same origin: routing cell (r, c) = sub-cell (FINE*r, FINE*c)."""
    gf = raster.Grid.__new__(raster.Grid)
    gf.step = g.step / FINE
    gf.x0, gf.y0 = g.x0, g.y0
    gf.w = FINE * (g.w - 1) + 1
    gf.h = FINE * (g.h - 1) + 1
    return gf


def dist_map(mask_f, step_f, H, W):
    """Distance (mm) from a routing cell centre to the nearest copper sub-cell."""
    if not mask_f.any():
        return np.full((H, W), INF, dtype=np.float32)
    d = ndimage.distance_transform_edt(~mask_f, sampling=step_f)
    return d[::FINE, ::FINE][:H, :W].astype(np.float32)


def block_cover(mask_f, dev):
    """(Hf,Wf) bool -> (H,W) bool: a cell is occupied if any of its sub-cells (+-2) has copper."""
    if par.enabled():
        return torch.from_numpy(block_cover_np(mask_f)).to(dev)
    f = torch.from_numpy(np.ascontiguousarray(mask_f)).to(dev).float()[None, None]
    return F.max_pool2d(f, kernel_size=FINE + 1, stride=FINE, padding=FINE // 2)[0, 0] > 0


def block_cover_np(mask_f):
    """Same window as max_pool2d(kernel FINE+1, stride FINE, padding FINE//2), but in numpy on the CPU:
    OR over 5x5 sub-cells around a grid node. With a busy GPU, hundreds of sub-raster transfers cost ~40 s."""
    k, pd = FINE + 1, FINE // 2
    Hf, Wf = mask_f.shape
    H, W = (Hf + 2 * pd - k) // FINE + 1, (Wf + 2 * pd - k) // FINE + 1
    P = np.zeros((Hf + 2 * pd, Wf + 2 * pd), dtype=bool)
    P[pd:pd + Hf, pd:pd + Wf] = mask_f
    out = np.zeros((H, W), dtype=bool)
    for dy in range(k):
        rows = P[dy:dy + FINE * (H - 1) + 1:FINE]
        for dx in range(k):
            out |= rows[:, dx:dx + FINE * (W - 1) + 1:FINE]
    return out


def plane_layers(board):
    """{plane net: {layer name, ...}} from the board zones (inner layers of LAYERS only)."""
    import drcinfo as D
    out = {}
    for z in board.Zones():
        nn = z.GetNetname()
        if not nn or nn.split('/')[-1] not in D.PLANE_NETS or z.GetIsRuleArea():
            continue
        for lid in z.GetLayerSet().Seq():
            L = board.GetLayerName(lid)
            if L in LAYERS and L not in ('F.Cu', 'B.Cu'):
                out.setdefault(nn, set()).add(L)
    return out


def build_free(dP, dT, dK, dS, dH, inb_for, inb_via, fineT, groups):
    """Free-space masks per (width, clearance) group from distance maps.

    Clearances as in .kicad_dru: to PADS every class keeps PAD_CLEARANCE (the track_to_pad rule);
    the class clearance clr (e.g. 0.25 / 0.30) applies only to tracks and vias; strict nets carry
    their clearance need on tracks only as well (dS = distance maps to their tracks/vias). Applying
    clr and need to pads too locked the outputs of a 0.5 mm-pitch driver IC (neighbouring supply pad
    0.25 mm away) that DRC allows.

    dP, dT, dK, dH: (L,H,W) distances to pads, tracks/vias, keepouts, hole edges (mm);
    dS: {need: (L,H,W)}; inb_for(w) -> (H,W) bool "inside the board with inset EDGE_INSET + w/2";
    inb_via: the same for a via; fineT: (H,W) courtyard of fine-pitch parts.
    Returns (free {(w,clr): (L,H,W) bool}, free_via {(w,clr): (H,W) bool})."""
    free, free_via = {}, {}
    dPm, dTm = dP.min(dim=0).values, dT.min(dim=0).values
    for (w, clr) in sorted(groups):
        inb = inb_for(w)
        m = (inb[None] & (dP >= w / 2 + PAD_CLEARANCE + PAD_SAFE_MM) & (dT >= w / 2 + clr + SAFE_MM)
             & (dK >= w / 2 + SAFE_MM) & (dH >= clr + w / 2))
        for need, d in dS.items():
            m &= d >= max(need, clr) + w / 2 + SAFE_MM
        if bool(fineT.any()):
            # Inside the courtyard of fine-pitch parts the fine_pitch_escape rule applies
            # (last in .kicad_dru, it overrides the class rules too): a small clearance for any
            # track. The track there is laid W_FINE = 0.15 wide (see emit_fine): between pads of
            # 0.4 mm pitch about 0.1 mm remains on each side; a 0.2 track did not fit at all.
            rf = W_FINE / 2 + MR.FINE_PITCH_CLEARANCE + SAFE_MM
            m_f = (inb[None] & (dP >= rf) & (dT >= rf) & (dK >= W_FINE / 2 + SAFE_MM) & (dH >= clr + w / 2))
            for need, d in dS.items():
                m_f &= d >= rf
            m = (m & ~fineT[None]) | (m_f & fineT[None])
        free[(w, clr)] = m
        fv = (inb_via & (dPm >= MR.VIA_SIZE / 2 + PAD_CLEARANCE + PAD_SAFE_MM)
              & (dTm >= MR.VIA_SIZE / 2 + clr + SAFE_MM))
        fv &= dK.min(dim=0).values >= MR.VIA_SIZE / 2 + SAFE_MM
        for need, d in dS.items():
            fv &= d.min(dim=0).values >= MR.VIA_SIZE / 2 + max(need, clr) + SAFE_MM
        fv &= dH.min(dim=0).values >= MR.VIA_DRILL / 2 + MR.HOLE_TO_HOLE
        free_via[(w, clr)] = fv
    return free, free_via


class ModelBase:
    """Shared by the board model (CPU raster) and the placement-candidate model (GPU): cost and
    path footprint. A subclass sets g, dev, cls, need, free, free_via, own, cover_pad, cover_trk,
    fixed_pad, fixed, fine, k_via; free_for(net) may be overridden (the candidate model builds a
    per-net mask)."""

    fixed_pad = None         # int16 (L,H,W): pad coverage of all participants (None = ignore)

    def free_for(self, net):
        """(free (L,H,W) bool, free_via (H,W) bool) for a net - by its (width, clearance) group."""
        key = self.cls[net]
        return self.free[key], self.free_via[key]

    def cost_for(self, net, usage_bare_o, usage_dil_o, history, pres, hist_w, own_extra=None,
                 hard=False):
        """(cost (L,H,W) float32 with inf in forbidden cells, free_via (H,W) bool).

        usage_*_o: cell usage by OTHER nets' paths of the current negotiation (without this net):
        bare = the copper itself, dil = copper with its clearance. history = accumulated overuse."""
        w, clr = self.cls[net]
        step = self.g.step
        kw, kd = cells(w / 2, step), cells(w / 2 + clr, step)
        own = self.own[net] if own_extra is None else (self.own[net] | own_extra)
        # Own copper is always passable: start and goal lie IN it. Own-copper cells that are not
        # free by the group mask (foreign copper nearby) cost double: the path enters a pad from
        # the edge and does not run along its border.
        free_grp, fv = self.free_for(net)
        free = free_grp | own
        fv = fv.clone()
        fine = self.fine[None]
        r_fine = cells(W_FINE / 2 + MR.FINE_PITCH_CLEARANCE, step)
        # Fixed copper of other negotiation participants. Pads: track_to_pad clearance for any
        # class; tracks/vias: by the clearance they carry (need).
        ps = getattr(self, 'pad_sparse', None)
        if ps is not None and net in ps['nid']:
            # Exact: distance to copper of OTHER participants - pads (track_to_pad clearance) and tracks/vias
            # (class clearance of the net carrying the copper) - from sparse EDT patches. Coarse coverage +
            # dilation locked adjacent 0.5 mm-pitch pins of one batch (neighbouring MCU pins, driver outputs
            # next to a neighbour's stub): "NO PATH after 1 iteration" with free corridors.
            L_, H_, W_ = self.free[(w, clr)].shape
            n = ps['nid'][net]
            oth = ps['net'] != n
            is_pad = ps['pad']
            thr_pad = w / 2 + PAD_CLEARANCE + PAD_SAFE_MM
            thr_trk = w / 2 + torch.maximum(ps['need'], torch.full_like(ps['need'], clr)) + SAFE_MM
            thr_in = W_FINE / 2 + MR.FINE_PITCH_CLEARANCE + SAFE_MM
            hit_out = oth & torch.where(is_pad, ps['val'] < thr_pad, ps['val'] < thr_trk)
            hit_in = oth & (ps['val'] < thr_in)
            blk_out = torch.zeros(L_ * H_ * W_, dtype=torch.bool, device=self.dev)
            blk_out[ps['idx'][hit_out]] = True
            blk_in = torch.zeros_like(blk_out)
            blk_in[ps['idx'][hit_in]] = True
            free &= ~torch.where(fine, blk_in.view(L_, H_, W_), blk_out.view(L_, H_, W_))
            thr_v = MR.VIA_SIZE / 2 + torch.where(is_pad, torch.full_like(ps['need'], PAD_CLEARANCE),
                                                 torch.maximum(ps['need'], torch.full_like(ps['need'], clr))) + SAFE_MM
            fvb = torch.zeros(H_ * W_, dtype=torch.bool, device=self.dev)
            fvb[ps['idx'][oth & (ps['val'] < thr_v)] % (H_ * W_)] = True
            fv &= ~fvb.view(H_, W_)
        elif self.fixed_pad is not None:
            others = (self.fixed_pad - self.cover_pad[net].to(torch.int16)) > 0
            blk_out = dilate_t(others, cells(w / 2 + PAD_CLEARANCE + (PAD_SAFE_MM - SAFE_MM), step))
            blk_in = dilate_t(others, r_fine)
            free &= ~torch.where(fine, blk_in, blk_out)
            fv &= ~dilate_t(others, cells(MR.VIA_SIZE / 2 + PAD_CLEARANCE, step)).any(dim=0)
        for need, acc in (self.fixed.items() if ps is None or net not in ps['nid'] else ()):
            own16 = self.cover_trk[net].to(torch.int16)
            others = (acc - own16 if self.need[net] == need else acc) > 0
            # inside fine-pitch courtyards, against neighbours' fixed copper, fine_pitch_escape applies
            # (small clearance, 0.15 track); with the full power-class clearance (0.25) escapes from
            # fine-pitch power ICs were locked by neighbouring stubs and vias of the same batch
            blk_out = dilate_t(others, cells(w / 2 + max(clr, need), step))
            blk_in = dilate_t(others, r_fine)
            free &= ~torch.where(fine, blk_in, blk_out)
            fv &= ~dilate_t(others, cells(MR.VIA_SIZE / 2 + max(clr, need), step)).any(dim=0)
        # a via next to other nets' negotiated paths is a hard ban (only tracks negotiate)
        fv &= ~dilate_t(usage_dil_o > 0, self.k_via).any(dim=0)
        fv &= ~dilate_t(usage_bare_o > 0, cells(MR.VIA_SIZE / 2 + clr, step)).any(dim=0)
        if hard:
            # Sequential laying after a non-converged negotiation: other paths of the round are
            # a wall, not a surcharge. A path found then is conflict-free by construction.
            blk = dilate_t(usage_bare_o > 0, kd) | dilate_t(usage_dil_o > 0, kw)
            blk_f = (dilate_t(usage_bare_o > 0, r_fine)
                     | dilate_t(usage_dil_o > 0, cells(W_FINE / 2, step)))
            free &= ~torch.where(fine, blk_f, blk)
        # belt around vias of OTHER planes on their layers: the fill needs a corridor to the via
        for pnet, d in getattr(self, 'dV', {}).items():
            if pnet == net:
                continue
            free &= d >= w / 2 + PLANE_VIA_GUARD + SAFE_MM
            fv &= d.min(dim=0).values >= MR.VIA_SIZE / 2 + PLANE_VIA_GUARD + SAFE_MM
        conf = box_count(usage_dil_o, kw) + box_count(usage_bare_o, kd)
        cost = (1.0 + hist_w * history) * (1.0 + pres * conf)
        cost = torch.where(free_grp, cost, torch.where(own, 2.0 * cost, torch.full_like(cost, INF)))
        cost = torch.where(free, cost, torch.full_like(cost, INF))
        return cost, fv

    def footprint(self, path, net):
        """Copper of a path: (bare, dil) bool (L,H,W); vias on all layers."""
        w, clr = self.cls[net]
        step = self.g.step
        L, H, W = len(LAYERS), self.g.h, self.g.w
        cellmask = torch.zeros((L, H, W), dtype=torch.bool, device=self.dev)
        if path:
            idx = torch.tensor(path, dtype=torch.long, device=self.dev).T
            cellmask[idx[0], idx[1], idx[2]] = True
        vias = torch.zeros((H, W), dtype=torch.bool, device=self.dev)
        for a, b in zip(path, path[1:]):
            if a[0] != b[0]:
                vias[a[1], a[2]] = True
        fine = self.fine[None]
        inside, outside = cellmask & fine, cellmask & ~fine
        bare = dilate_t(outside, cells(w / 2, step)) | dilate_t(inside, cells(W_FINE / 2, step))
        dil = (dilate_t(outside, cells(w / 2 + clr, step))
               | dilate_t(inside, cells(W_FINE / 2 + MR.FINE_PITCH_CLEARANCE, step)))
        if bool(vias.any()):
            bare |= dilate_t(vias, cells(MR.VIA_SIZE / 2, step))[None]
            dil |= dilate_t(vias, cells(MR.VIA_SIZE / 2 + clr, step))[None]
        return bare, dil


class Model(ModelBase):
    def __init__(self, board, board_path, g, nets, device='cuda', log=print):
        self.board, self.g, self.dev = board, g, device
        self.nets = list(nets)
        routed = set(self.nets)
        step = g.step
        H, W = g.h, g.w
        gf = fine_grid(g)
        strict = MR.strict_nets(board_path, board)
        self.cls = {net: MR.net_class_width(board_path, net) for net in self.nets}
        # clearance carried by the net's OWN copper (for others)
        self.need = {net: max(self.cls[net][1], strict.get(net, 0.0)) for net in self.nets}

        # 1. Static copper on the sub-raster -> distance maps on the routing grid.
        # KAT_PAR: board items come from the basket (one SWIG pass), all masks are built first,
        # then the EDTs run as a batch in threads (scipy releases the GIL); assembly order is
        # unchanged, the result is bit-exact (par.py, raster.Basket).
        bk = raster.Basket(board) if par.enabled() else None      # always fresh: the board may have changed (ripup)
        strict_other = {n: v for n, v in strict.items() if n not in routed}
        skip = routed | set(strict_other)
        d_strict_needs = sorted(set(strict_other.values()))
        jobs = []                                   # (key, function -> map)

        def edt_job(key, mask):
            jobs.append((key, lambda mk=mask: dist_map(mk, gf.step, H, W)))
        for L in LAYERS:
            # pads: all except routed nets (strictness is carried by tracks only);
            # tracks/vias: without routed and without strict nets (those are in d_strict)
            cu, _ = raster.copper_mask(board, gf, L, nets_skip=routed, kinds=('pads',), basket=bk)
            edt_job(('pad', L), cu)
            cu, _ = raster.copper_mask(board, gf, L, nets_skip=skip, kinds=('tracks', 'vias'), basket=bk)
            edt_job(('trk', L), cu)
            ko, _ = raster.keepout_mask(board, gf, L)
            edt_job(('ko', L), ko)
            for need in d_strict_needs:
                ns = {n for n, v in strict_other.items() if v == need}
                cus, _ = raster.copper_mask(board, gf, L, nets_only=ns, kinds=('tracks', 'vias'), basket=bk)
                edt_job(('strict', need, L), cus)
        holes = bk.holes(routed) if bk is not None else self._holes(board, routed)
        hm = gf.empty()
        for x, y, r in holes:
            raster.fill_disc(gf, hm, x, y, r)
        edt_job(('hole',), hm)
        # 1b. Plane vias on their own plane layers (by zones) - maps for the PLANE_VIA_GUARD belt
        pl = plane_layers(board)
        pvias = {}
        for pnet in pl:
            if bk is not None:
                pvias[pnet] = [(x, y) for (x, y, d, wl) in bk.vias.get(pnet, [])]
            else:
                pvias[pnet] = [(t.Cast().GetPosition().x / MM, t.Cast().GetPosition().y / MM)
                               for t in board.GetTracks() if t.GetClass() == 'PCB_VIA' and t.GetNetname() == pnet]
        for pnet, lays in pl.items():
            for L in LAYERS:
                m = gf.empty()
                if L in lays:
                    for x, y in pvias[pnet]:
                        raster.fill_disc(gf, m, x, y, MR.VIA_SIZE / 2)
                if m.any():
                    edt_job(('dV', pnet, L), m)
                else:
                    jobs.append((('dV', pnet, L), lambda: np.full((H, W), 99.0, dtype=np.float32)))
        maps = dict(zip([k for k, _ in jobs], par.map_threads(lambda f: f(), [f for _, f in jobs])))
        self.dP = to_t(np.stack([maps[('pad', L)] for L in LAYERS]), device)
        self.dT = to_t(np.stack([maps[('trk', L)] for L in LAYERS]), device)
        self.dB = torch.minimum(self.dP, self.dT)      # compatibility (check_sdf, probe)
        self.dK = to_t(np.stack([maps[('ko', L)] for L in LAYERS]), device)
        self.dS = {need: to_t(np.stack([maps[('strict', need, L)] for L in LAYERS]), device) for need in d_strict_needs}
        dH1 = to_t(maps[('hole',)], device)
        self.dH = dH1[None].expand(len(LAYERS), H, W).contiguous()
        fineT = to_t(self._fine(board, g), device)
        self.fine = fineT
        self.dV = {pnet: to_t(np.stack([maps[('dV', pnet, L)] for L in LAYERS]), device) for pnet in pl}
        log('model: sub-raster %.3f mm %dx%d, strict groups %d, foreign holes %d'
            % (gf.step, gf.w, gf.h, len(self.dS), len(holes)))

        # 2. Free space per (width, clearance) group - for a track and for a via
        self._inb_cache = {}

        def inb_for(w):
            key = round(w, 4)
            if key not in self._inb_cache:
                self._inb_cache[key] = to_t(raster.inside_board(board, g, inset=MR.EDGE_INSET + w / 2), device)
            return self._inb_cache[key]
        self.inb_via = to_t(raster.inside_board(board, g, inset=MR.EDGE_INSET + MR.VIA_SIZE / 2), device)
        self.inb_for = inb_for
        groups = set(self.cls.values())
        self.free, self.free_via = build_free(self.dP, self.dT, self.dK, self.dS, self.dH, inb_for, self.inb_via,
                                              fineT, groups)
        for (w, clr) in sorted(groups):
            log('  group w=%.3f clr=%.2f: free %.1f%% of cells, via-free %.1f%%'
                % (w, clr, 100.0 * self.free[(w, clr)].float().mean(),
                   100.0 * self.free_via[(w, clr)].float().mean()))

        # 3. Own copper of the routed nets: components (by cell centres) and coverage (for others).
        # Independent per net: raster from the basket + label (GIL released) in threads; GPU in the main thread.
        self.own, self.labels, self.ncomp = {}, {}, {}
        self.cover, self.cover_pad, self.cover_trk = {}, {}, {}

        def per_net(net):
            lab, n = CN.components(board, g, net, basket=bk)
            cov_p = [raster.copper_mask(board, gf, L, nets_only={net}, kinds=('pads',), basket=bk)[0] for L in LAYERS]
            cov_t = [raster.copper_mask(board, gf, L, nets_only={net}, kinds=('tracks', 'vias'), basket=bk)[0] for L in LAYERS]
            if par.enabled():                      # coverage on the CPU in the thread, sent to the GPU ready-made
                cov_p = np.stack([block_cover_np(cu) for cu in cov_p])
                cov_t = np.stack([block_cover_np(cu) for cu in cov_t])
            return lab, n, cov_p, cov_t
        for net, (lab, n, cov_p, cov_t) in zip(self.nets, par.map_threads(per_net, self.nets)):
            self.labels[net] = lab
            self.ncomp[net] = n
            self.own[net] = to_t(lab > 0, device)
            if isinstance(cov_p, np.ndarray):
                self.cover_pad[net] = torch.from_numpy(cov_p).to(device)
                self.cover_trk[net] = torch.from_numpy(cov_t).to(device)
            else:
                self.cover_pad[net] = torch.stack([block_cover(cu, device) for cu in cov_p])
                self.cover_trk[net] = torch.stack([block_cover(cu, device) for cu in cov_t])
            self.cover[net] = self.cover_pad[net] | self.cover_trk[net]
        # 3b. KAT_PAR: exact distances to the pads of each net of the batch - EDT on a patch around the pad
        # (raster from the basket, sub-raster), sampled at routing-grid nodes, sparse (d < CAP_PAD)
        self.pad_sparse = None
        if bk is not None:
            self.pad_sparse = self._pad_sparse(bk, gf, g, device)
        # 4. Fixed copper of participants (coverage): pads in one counter (track_to_pad clearance
        # to them for everyone), tracks/vias by the clearance they carry
        self.fixed_pad = torch.zeros((len(LAYERS), H, W), dtype=torch.int16, device=device)
        for net in self.nets:
            self.fixed_pad += self.cover_pad[net].to(torch.int16)
        self.fixed = {}
        for need in sorted(set(self.need.values())):
            acc = torch.zeros((len(LAYERS), H, W), dtype=torch.int16, device=device)
            for net in self.nets:
                if self.need[net] == need:
                    acc += self.cover_trk[net].to(torch.int16)
            self.fixed[need] = acc
        self.k_via = cells(MR.VIA_SIZE / 2, step)

    CAP_PAD = 0.9    # mm: beyond this a pad affects no threshold (max threshold ~0.63)

    def _pad_sparse(self, bk, gf, g, device):
        """{'idx': LongTensor (K,), 'val': FloatTensor (K,), 'net': LongTensor (K,), 'nid': {net: i}}:
        for each net of the batch and each of its pads, the distance from routing-grid nodes within
        CAP_PAD to the pad copper (EDT of a sub-raster patch, same raster as the static maps)."""
        H, W = g.h, g.w
        margin = int(math.ceil(self.CAP_PAD / gf.step)) + FINE
        lay_idx = {L: i for i, L in enumerate(LAYERS)}

        def patch(li, x0m, y0m, x1m, y1m, draw, out_idx, out_val, out_pad, is_pad):
            """EDT on a sub-raster patch around the bbox (x0m..x1m, y0m..y1m) mm; draw(sub, m) draws the copper."""
            c0 = max(0, (gf.col(x0m) - margin) // FINE * FINE)
            c1 = min(gf.w - 1, gf.col(x1m) + margin)
            r0 = max(0, (gf.row(y0m) - margin) // FINE * FINE)
            r1 = min(gf.h - 1, gf.row(y1m) + margin)
            if r1 <= r0 or c1 <= c0:
                return
            sub = raster.Grid.__new__(raster.Grid)
            sub.step, sub.x0, sub.y0 = gf.step, gf.x(c0), gf.y(r0)
            sub.w, sub.h = c1 - c0 + 1, r1 - r0 + 1
            m = np.zeros((sub.h, sub.w), dtype=bool)
            draw(sub, m)
            if not m.any():
                return
            d = ndimage.distance_transform_edt(~m, sampling=gf.step)
            dd = d[::FINE, ::FINE].astype(np.float32)
            rr, cc = np.nonzero(dd < self.CAP_PAD)
            rows = r0 // FINE + rr
            cols = c0 // FINE + cc
            ok = (rows < H) & (cols < W)
            out_idx.append(((li * H + rows[ok]) * W + cols[ok]).astype(np.int64))
            out_val.append(dd[rr[ok], cc[ok]])
            out_pad.append(np.full(int(ok.sum()), is_pad, dtype=bool))

        def one_net(item):
            i, net = item
            out_idx, out_val, out_pad = [], [], []
            for (x, y, drill, polys) in bk.pads.get(net, []):
                for L, pl in polys.items():
                    li = lay_idx.get(L)
                    if li is None:
                        continue
                    for pts in pl:
                        if len(pts) < 3:
                            continue
                        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                        patch(li, min(xs), min(ys), max(xs), max(ys),
                              lambda sub, m, pts=pts: raster.poly_fill(sub, m, pts), out_idx, out_val, out_pad, True)
            for L, segs in bk.tracks.get(net, {}).items():
                li = lay_idx.get(L)
                if li is None:
                    continue
                for (x1, y1, x2, y2, w) in segs:
                    patch(li, min(x1, x2) - w / 2, min(y1, y2) - w / 2, max(x1, x2) + w / 2, max(y1, y2) + w / 2,
                          lambda sub, m, a=(x1, y1, x2, y2, w): raster.fill_segment(sub, m, *a), out_idx, out_val, out_pad, False)
            for (x, y, drill, wl) in bk.vias.get(net, []):
                for L, w in wl.items():
                    li = lay_idx.get(L)
                    if li is None:
                        continue
                    patch(li, x - w / 2, y - w / 2, x + w / 2, y + w / 2,
                          lambda sub, m, a=(x, y, w / 2.0): raster.fill_disc(sub, m, *a), out_idx, out_val, out_pad, False)
            if not out_idx:
                return np.zeros(0, np.int64), np.zeros(0, np.float32), np.zeros(0, bool), i
            return np.concatenate(out_idx), np.concatenate(out_val), np.concatenate(out_pad), i
        res = par.map_threads(one_net, list(enumerate(self.nets)))
        idx = np.concatenate([r[0] for r in res]) if res else np.zeros(0, np.int64)
        val = np.concatenate([r[1] for r in res]) if res else np.zeros(0, np.float32)
        pad = np.concatenate([r[2] for r in res]) if res else np.zeros(0, bool)
        nid = np.concatenate([np.full(len(r[0]), r[3], np.int64) for r in res]) if res else np.zeros(0, np.int64)
        needs = np.array([self.need[n] for n in self.nets], dtype=np.float32)
        return {'idx': torch.from_numpy(idx).to(device), 'val': torch.from_numpy(val).to(device),
                'net': torch.from_numpy(nid).to(device), 'pad': torch.from_numpy(pad).to(device),
                'need': torch.from_numpy(needs[nid] if len(nid) else needs[:0]).to(device),
                'nid': {net: i for i, net in enumerate(self.nets)}}

    # --- helper rasters ---------------------------------------------------------
    @staticmethod
    def _holes(board, routed):
        """(x, y, drill_radius) of all holes except those of the routed nets."""
        out = []
        for t in board.GetTracks():
            if t.GetClass() != 'PCB_VIA' or t.GetNetname() in routed:
                continue
            v = t.Cast()
            out.append((v.GetPosition().x / MM, v.GetPosition().y / MM, v.GetDrill() / MM / 2.0))
        for fp in board.GetFootprints():
            for p in fp.Pads():
                d = p.GetDrillSizeX() / MM
                if d <= 0 or p.GetNetname() in routed:
                    continue
                out.append((p.GetPosition().x / MM, p.GetPosition().y / MM, d / 2.0))
        return out

    @staticmethod
    def _hole_mask(g, holes, extra):
        m = g.empty()
        for x, y, r in holes:
            raster.fill_disc(g, m, x, y, r + extra)
        return m

    @staticmethod
    def _fine(board, g):
        fine = g.empty()
        for fp in board.GetFootprints():
            if fp.GetReference() not in MR.FINE_PITCH_REFS:
                continue
            ps = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
            for pts in raster._polyset_outlines(ps):
                raster.fill_poly(g, fine, pts)
        return fine


def emit_fine(g, net, w, path, fine):
    """Cell path -> .kicad_pcb blocks; inside the fine mask (H,W) segments are W_FINE wide.

    A straight run crossing the courtyard border is split at the border: class width outside,
    W_FINE inside. Vias as usual."""
    from wave import simplify
    fine_np = fine.cpu().numpy() if hasattr(fine, 'cpu') else fine
    out = []
    for s in simplify(path):
        if s[0] != 'seg':
            out += MR.emit(g, net, w, [s])
            continue
        _, li, r1, c1, r2, c2 = s
        n = max(abs(r2 - r1), abs(c2 - c1))
        dr, dc = (r2 - r1) // n, (c2 - c1) // n
        cells_ = [(r1 + i * dr, c1 + i * dc) for i in range(n + 1)]
        # split by the cells' fine flag; the border cell belongs to both pieces
        i = 0
        while i < n:
            f = bool(fine_np[cells_[i][0], cells_[i][1]]) and bool(fine_np[cells_[i + 1][0], cells_[i + 1][1]])
            j = i + 1
            while j < n and (bool(fine_np[cells_[j][0], cells_[j][1]])
                             and bool(fine_np[cells_[j + 1][0], cells_[j + 1][1]])) == f:
                j += 1
            ra, ca = cells_[i]
            rb, cb = cells_[j]
            out += MR.emit(g, net, W_FINE if f else w,
                           [('seg', LAYERS[li], ra, ca, rb, cb)])
            i = j
    return out
