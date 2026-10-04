"""Pocket: a window around a group of ICs, its movable parts, static copper and the candidate model.

Candidate = {ref: (x, y, rot, flipped)} for the movable footprints of the window. From it the GPU
builds distance maps (statics + moved pads), free-space masks (model.build_free — the same
thresholds as the router), the cover of pocket-net pads, and a model with the Model interface.

L0 score — "is an end locked": for every pad of a pocket net a wave of REACH_ITERS iterations in a
+-REACH_MM window; fewer than LOCKED_CELLS reachable cells -> locked (constants from locked.py).

Pocket ICs and windows are given by the caller (CLI --refs / --window); nothing here is board-specific.
"""
import json
import math

import numpy as np
import torch

import raster
import maze_route as MR
import model as M
import par
import project
import drcinfo as D
import sdf
from cmodel import CandidateModel
from wavebatch import BatchWave

LAYERS = raster.SIGNAL
from locked import REACH_MM, LOCKED_CELLS   # 4.0 mm and 40 cells — the same as the locked.py reference
RING_MM = 3.0            # ring around the courtyards of the pocket ICs
PLANE_RIP_MM = 1.0       # a same-net plane via no further than this from the ORIGINAL pad of a moved part
                         # is ripped together with its copper (candidate model and apply_place share the rule)
PAD_GROUP_OFFSET = 1000  # groups of pocket-net pads: 1000 + net index
# Nets whose class clearance is at least this (RF, controlled impedance) are never ripped;
# must match apply_place.NEVER_RIP_CLEARANCE.
NEVER_RIP_CLEARANCE = float(project.get('never_rip_min_clearance', 0.3))


class Pocket:
    def __init__(self, board, board_path, g, geom, ic_refs, dev='cuda', ring_mm=RING_MM,
                 movable_kinds=('small',), extra_movable=(), log=print, bbox=None):
        """bbox=(x0, y0, x1, y1) — manual window (e.g. one side of a large IC: a whole 33x33 mm
        LQFP window took ~280 s per L1, one side ~7x31 mm is practical); ends are only the IC pads
        inside the window."""
        self.dev = dev
        self.board_path = board_path
        self.g = g
        self.geom = geom
        self.ic_refs = list(ic_refs)
        self.fps = {fp['ref']: fp for fp in geom['footprints']}
        # window: union of the IC courtyards + ring
        xs0, ys0, xs1, ys1 = [], [], [], []
        for r in self.ic_refs:
            fp = self.fps[r]
            bb = self._fp_bbox(fp)
            xs0.append(bb[0]); ys0.append(bb[1]); xs1.append(bb[2]); ys1.append(bb[3])
        x0, y0, x1, y1 = min(xs0) - ring_mm, min(ys0) - ring_mm, max(xs1) + ring_mm, max(ys1) + ring_mm
        if bbox is not None:
            x0, y0, x1, y1 = bbox
        self.bbox = (x0, y0, x1, y1)
        self.win = sdf.Window(g, max(0, g.row(y0)), min(g.h - 1, g.row(y1)), max(0, g.col(x0)), min(g.w - 1, g.col(x1)))
        # movable: footprints of the requested classes centred in the window (pocket ICs — by flag)
        self.movable = []
        for fp in geom['footprints']:
            inside = x0 <= fp['x'] <= x1 and y0 <= fp['y'] <= y1
            if fp['ref'] in extra_movable or (inside and fp['mobility'] in movable_kinds) \
                    or (inside and fp['ref'] in self.ic_refs and 'ic' in movable_kinds):
                self.movable.append(fp['ref'])
        self.movable_set = set(self.movable)
        # pocket nets: all nets of the pads of the ICs and the movable parts
        self.nets = []
        for r in self.ic_refs + self.movable:
            for p in self.fps[r]['pads']:
                if p['net'] and p['net'] not in self.nets:
                    self.nets.append(p['net'])
        self.net_id = {n: i for i, n in enumerate(self.nets)}
        strict = MR.strict_nets(board_path, board)
        self.cls = {n: MR.net_class_width(board_path, n) for n in self.nets}
        self.need = {n: max(self.cls[n][1], strict.get(n, 0.0)) for n in self.nets}
        strict_other = {n: v for n, v in strict.items() if n not in self.net_id}
        self.strict_needs = sorted(set(strict.values()))     # pocket nets too: their tracks outside the window

        def group_of(net):
            if net in self.net_id:
                return PAD_GROUP_OFFSET + self.net_id[net]
            if net in strict_other:
                return int(round(strict_other[net] * 100))
            return 0
        self.group_of = group_of

        def pad_group_of(net):
            # pads carry no strict clearance (one track-to-pad rule for all classes): foreign
            # nets go to group 0 (static dP), pocket nets get their own group (exact per-net handling)
            return PAD_GROUP_OFFSET + self.net_id[net] if net in self.net_id else 0
        self.pad_group_of = pad_group_of
        # static copper: pads of NON-movable footprints (except pocket nets — those are "own"),
        # tracks/vias outside the window and of all non-pocket nets; pocket-net copper inside the
        # window is treated as ripped (the router will re-lay it)
        pads_static = sdf.prims_from_geom(geom, dev, refs={f['ref'] for f in geom['footprints']} - self.movable_set
                                          - set(self.ic_refs), group_of=pad_group_of)
        keep = pads_static.t['group'] < PAD_GROUP_OFFSET
        self.pads_static = pads_static.select(keep, poly_keep=lambda g: g < PAD_GROUP_OFFSET)
        # Pocket-net pads on NON-movable footprints (connectors, neighbouring ICs) in and near the
        # window (beyond CAP_MM + extent they do not affect the window map; plane nets are pocket
        # nets too, and without this cut-off every pad of theirs on the board ended up here —
        # thousands of rows) plus the pads of fixed pocket ICs — pp_fixed: for their own net they
        # are copper/target, for others an obstacle; handled per net (CandidateModel.free_for,
        # cached base map dP_net_base).
        near = 1.5 + sdf.CAP_MM
        ps = pads_static.t
        inw_p = ((~keep) & (ps['cx'] >= x0 - near) & (ps['cx'] <= x1 + near)
                 & (ps['cy'] >= y0 - near) & (ps['cy'] <= y1 + near))
        pp_static = pads_static.select(inw_p, poly_keep=lambda g: g >= PAD_GROUP_OFFSET)
        self.ic_static = [r for r in self.ic_refs if r not in self.movable_set]
        pads_ic0 = sdf.prims_from_geom(geom, dev, refs=set(self.ic_static), group_of=pad_group_of)
        self.pp_fixed = sdf.cat(pp_static, pads_ic0.select(pads_ic0.t['group'] >= PAD_GROUP_OFFSET,
                                                             poly_keep=lambda g: g >= PAD_GROUP_OFFSET))
        self.pads_static = sdf.cat(self.pads_static, pads_ic0.select(pads_ic0.t['group'] < PAD_GROUP_OFFSET,
                                                                       poly_keep=lambda g: g < PAD_GROUP_OFFSET))
        self._dP_base, self._d_own_fixed = {}, {}
        # Tracks/vias inside the window are treated as RIPPED (the router re-lays them on apply),
        # except vias of plane nets — they keep the plane connection. Otherwise L0 reported pins
        # as locked by foreign tracks that get ripped anyway.
        bk = raster.Basket(board) if par.enabled() else None      # always a fresh basket
        tracks = sdf.prims_from_tracks(board, dev, group_of=group_of, mark_plane_via=True, basket=bk)
        tt = tracks.t
        # a track is ripped when BOTH ends are in the window (a via — its centre); same rule as
        # apply_place; a track crossing the window border stays an obstacle (and a link outward)
        def _in(xa, ya):
            return (xa >= x0) & (xa <= x1) & (ya >= y0) & (ya <= y1)
        inwin = _in(tt['x1'], tt['y1']) & _in(tt['x2'], tt['y2'])
        # RF copper (class clearance >= NEVER_RIP_CLEARANCE) is never ripped — as in apply_place
        # (otherwise an L1 path crossed an RF feed on B.Cu that the model considered ripped)
        self.rf_nets = {n for n, v in strict.items() if v >= NEVER_RIP_CLEARANCE}
        is_rf = torch.zeros_like(inwin)
        for n in self.rf_nets:
            gid_rf = PAD_GROUP_OFFSET + self.net_id[n] if n in self.net_id else int(round(strict[n] * 100))
            is_rf |= tt['group'] == gid_rf
        keep_t = (~inwin) | is_rf             # plane vias of the window — separate table pv (below)
        # own fixed copper of pocket nets OUTSIDE the window (target "anchors") — from tracks/vias
        self.anchor_prims = tracks.select((tt['group'] >= PAD_GROUP_OFFSET) & (~inwin))
        # for the window maps, pocket-net tracks outside the window are ordinary obstacles of their strictness
        grp = tt['group'].clone()
        for n, i in self.net_id.items():
            gid_new = int(round(strict[n] * 100)) if n in strict else 0
            grp[tt['group'] == PAD_GROUP_OFFSET + i] = gid_new
        tracks.t['group'] = grp
        self.tracks_static = tracks.select(keep_t)
        # kept plane-net vias in the window, per net — a plane pad next to such a same-net via
        # already has its plane connection (fitness.plane_pads_without_via)
        # Drills: through-hole pads of all footprints (movable ones are SMD only) and those vias
        # that remain after ripping the window copper — outside the window or of plane nets (same
        # rule as tracks_static). Vias of ripped tracks are not obstacles: otherwise a pin's own
        # via at the pad end, or the via of a cut stub, locked the pin with a hole that will not
        # exist after re-routing.
        # Plane vias inside the window: thermal vias inside pads of fixed ICs (exposed pads) are
        # static (tables pv/pv_holes; plane_via_keep removes them only near moved pads of the same
        # net within PLANE_RIP_MM — never the case for an exposed pad). Other plane vias of the
        # window are routing artifacts, not placement (e.g. supply/GND vias of a driver sat right
        # in the escape lane of a signal): they are ripped with the window copper, and connecting
        # plane pads to the plane becomes L1's job (fitness.l1_score: via jobs on plane layers,
        # see plane_layers) and apply_place/fix_plane_pads.
        holes = sdf.PrimTable(dev)
        self.pv, self.pv_holes = sdf.PrimTable(dev), sdf.PrimTable(dev)
        self.pv_info = []                     # (x, y, net, pocket net id or -1)
        pv_row, pvh_row = [], []
        MMu = 1e6
        if bk is not None:
            via_iter = [(nn, x, y, drill / 2.0, wl) for nn, lst in bk.vias.items() for (x, y, drill, wl) in lst]
        else:
            via_iter = []
            for tr in board.GetTracks():
                if tr.GetClass() != 'PCB_VIA':
                    continue
                v = tr.Cast()
                wl = {}
                for L in LAYERS:
                    if v.IsOnLayer(board.GetLayerID(L)):
                        try:
                            wl[L] = v.GetWidth(board.GetLayerID('F.Cu')) / MMu
                        except TypeError:
                            wl[L] = v.GetWidth() / MMu
                via_iter.append((v.GetNetname(), v.GetPosition().x / MMu, v.GetPosition().y / MMu, v.GetDrill() / MMu / 2.0, wl))
        for (nn, vx, vy, r, wl) in via_iter:
            inw = x0 <= vx <= x1 and y0 <= vy <= y1
            plane = nn.split('/')[-1] in D.PLANE_NETS
            if inw and not plane and nn not in self.rf_nets:
                continue                      # ripped window copper — and its via too (RF stays)
            if inw and plane and not self._in_ic_pad(vx, vy, nn):
                continue                      # plane via of the window outside an exposed pad — L1/fix_plane_pads re-place it
            if inw and plane:
                layers = [i for i, L in enumerate(LAYERS) if L in wl]
                w = wl.get('F.Cu', next(iter(wl.values())) if wl else 0.0)
                k = len(self.pv_info)
                self.pv_info.append((vx, vy, nn, self.net_id.get(nn, -1)))
                gv = int(round(strict[nn] * 100)) if nn in strict else 0     # plane copper: normal or strict
                self.pv.add(sdf.KIND_CIRCLE, layers, vx, vy, w / 2, w / 2, w / 2, 0.0, gv, plane_via=1)
                pv_row += [k] * len(layers)
                self.pv_holes.add(sdf.KIND_CIRCLE, list(range(len(LAYERS))), vx, vy, r, r, r, 0.0, 999)
                pvh_row += [k] * len(LAYERS)
                continue
            holes.add(sdf.KIND_CIRCLE, list(range(len(LAYERS))), vx, vy, r, r, r, 0.0, 999)
        if bk is not None:
            for nn, lst in bk.pads.items():
                for (px, py, dd, polys) in lst:
                    if dd > 0:
                        holes.add(sdf.KIND_CIRCLE, list(range(len(LAYERS))), px, py, dd / 2.0, dd / 2.0, dd / 2.0, 0.0, 999)
        else:
            for fp in board.GetFootprints():
                for p in fp.Pads():
                    dd = p.GetDrillSizeX() / MMu
                    if dd <= 0:
                        continue
                    holes.add(sdf.KIND_CIRCLE, list(range(len(LAYERS))), p.GetPosition().x / MMu, p.GetPosition().y / MMu,
                              dd / 2.0, dd / 2.0, dd / 2.0, 0.0, 999)
        self.holes = holes.finalize()
        self.pv.finalize(); self.pv_holes.finalize()
        # plane layers per net (inner layers, from the board zones) — targets of the L1 via jobs
        self.plane_layers = {}
        for z in board.Zones():
            nn = z.GetNetname()
            if nn.split('/')[-1] not in D.PLANE_NETS:
                continue
            for i, L in enumerate(LAYERS):
                if 0 < i < len(LAYERS) - 1 and z.IsOnLayer(board.GetLayerID(L)):
                    self.plane_layers.setdefault(nn, set()).add(i)
        self.pv_row = torch.tensor(pv_row, dtype=torch.long, device=dev)
        self.pvh_row = torch.tensor(pvh_row, dtype=torch.long, device=dev)
        self.pv_xy = torch.tensor([(a, b) for a, b, _, _ in self.pv_info] or [(0.0, 0.0)], dtype=torch.float32, device=dev)
        self.pv_nid = torch.tensor([d for _, _, _, d in self.pv_info] or [-1], dtype=torch.long, device=dev)
        # static maps on the window
        win = self.win
        self.dP0 = sdf.dist_map(self.pads_static, win, group=0)        # pads (track-to-pad clearance)
        self.dT0 = sdf.dist_map(self.tracks_static, win, group=0)      # tracks/vias (class clearance)
        self.dS0 = {}
        for need in self.strict_needs:
            gid = int(round(need * 100))
            self.dS0[need] = sdf.dist_map(self.tracks_static, win, group=gid)   # strict — tracks only
        self.dH0 = sdf.dist_map(self.holes, win, group=999)
        # PLANE_VIA_GUARD belt: plane vias (outside the window and exposed pads) on their own plane layers, per net
        self.dV = {}
        pl = M.plane_layers(board)
        lay_idx = {L: i for i, L in enumerate(LAYERS)}
        for k, pnet in enumerate(pl):
            tab = sdf.PrimTable(dev)
            lays = [lay_idx[L] for L in pl[pnet] if L in lay_idx]
            if bk is not None:
                pv_xy = [(x, y) for (x, y, d_, wl) in bk.vias.get(pnet, [])]
            else:
                pv_xy = [(tr.GetPosition().x / MMu, tr.GetPosition().y / MMu) for tr in board.GetTracks()
                         if tr.GetClass() == 'PCB_VIA' and tr.GetNetname() == pnet]
            for (vx, vy) in pv_xy:
                inw = x0 <= vx <= x1 and y0 <= vy <= y1
                if inw and not self._in_ic_pad(vx, vy, pnet):
                    continue                  # ripped with the window copper
                tab.add(sdf.KIND_CIRCLE, lays, vx, vy, MR.VIA_SIZE / 2, MR.VIA_SIZE / 2, MR.VIA_SIZE / 2, 0.0, 0)
            self.dV[pnet] = sdf.dist_map(tab.finalize(), win, group=0)
        self.dH = sdf.dist_map(self.pv_holes, win, group=999, out=self.dH0.clone())   # base: all plane vias in place
        # keepouts and board edge — from the CPU raster (static)
        gf = M.fine_grid(g)
        H, W = g.h, g.w
        kos = [raster.keepout_mask(board, gf, L)[0] for L in LAYERS]
        dk = [d[win.r0:win.r1 + 1, win.c0:win.c1 + 1] for d in par.edt_many(M.dist_map, [(ko, gf.step, H, W) for ko in kos])]
        self.dK = M.to_t(np.stack(dk), dev)
        self._inb = {}
        self._board = board
        self.inb_via = M.to_t(raster.inside_board(board, g, inset=MR.EDGE_INSET + MR.VIA_SIZE / 2)[win.r0:win.r1 + 1, win.c0:win.c1 + 1], dev)
        fine = M.Model._fine(board, g)[win.r0:win.r1 + 1, win.c0:win.c1 + 1]
        self.fine = M.to_t(fine, dev)
        self.groups = set(self.cls.values())
        self.wave = BatchWave([MR.LAYER_COST[l] for l in LAYERS], MR.VIA_COST, dev)
        self.base_placement = {r: (self.fps[r]['x'], self.fps[r]['y'], self.fps[r]['rot'], self.fps[r]['flipped'])
                               for r in self.movable}
        # Nets that leave the window (tracks/vias outside the window — anchors — or pads on
        # footprints outside the window): in L1 they need an escape to the ring; the rest connect
        # inside the window.
        self.outside_nets = set()
        ag = self.anchor_prims.t['group']
        for n, i in self.net_id.items():
            if bool((ag == PAD_GROUP_OFFSET + i).any()):
                self.outside_nets.add(n)
        for fp in geom['footprints']:
            if fp['ref'] in self.movable_set or fp['ref'] in self.ic_refs:
                continue
            if not (x0 <= fp['x'] <= x1 and y0 <= fp['y'] <= y1):
                for p in fp['pads']:
                    if p['net'] in self.net_id:
                        self.outside_nets.add(p['net'])
        log('pocket %s: window %dx%d cells (%.1fx%.1f mm), movable %d, nets %d, statics: pads %d, tracks %d'
            % (','.join(self.ic_refs), win.h, win.w, win.h * g.step, win.w * g.step, len(self.movable),
               len(self.nets), len(self.pads_static), len(self.tracks_static)))

    def _in_ic_pad(self, vx, vy, net):
        """Point inside a same-net pad of a fixed pocket IC (exposed-pad thermal via)."""
        for r in self.ic_refs:
            if r in self.movable_set:
                continue
            fp = self.fps[r]
            th = math.radians(fp['rot'])
            c, s = math.cos(th), math.sin(th)
            for p in fp['pads']:
                if p['net'] != net:
                    continue
                lx0 = -p['cx'] if fp['flipped'] else p['cx']
                bx, by = fp['x'] + lx0 * c + p['cy'] * s, fp['y'] - lx0 * s + p['cy'] * c
                dx, dy = vx - bx, vy - by
                lx, ly = dx * c - dy * s, dx * s + dy * c
                if abs(lx) <= p['hw'] + 0.05 and abs(ly) <= p['hh'] + 0.05:
                    return True
        return False

    def _fp_bbox(self, fp):
        cb = fp['courtyard']
        th = math.radians(fp['rot'])
        c, s = math.cos(th), math.sin(th)
        if cb is None:
            return (fp['x'] - 1, fp['y'] - 1, fp['x'] + 1, fp['y'] + 1)
        pts = [(cb[0], cb[1]), (cb[2], cb[1]), (cb[2], cb[3]), (cb[0], cb[3])]
        bx = [fp['x'] + px * c + py * s for px, py in pts]
        by = [fp['y'] - px * s + py * c for px, py in pts]
        return (min(bx), min(by), max(bx), max(by))

    def inb_for(self, w):
        key = round(w, 4)
        if key not in self._inb:
            win = self.win
            m = raster.inside_board(self._board, self.g, inset=MR.EDGE_INSET + w / 2)
            self._inb[key] = M.to_t(m[win.r0:win.r1 + 1, win.c0:win.c1 + 1], self.dev)
        return self._inb[key]

    # --- candidate ------------------------------------------------------------------
    def pads_of(self, placement):
        """Pad primitives of the MOVABLE pocket footprints at a given placement (fixed pocket ICs
        are in pp_fixed / pads_static)."""
        full = dict(self.base_placement)
        full.update(placement or {})
        return sdf.prims_from_geom(self.geom, self.dev, refs=self.movable_set,
                                   placement=full, group_of=self.pad_group_of)

    def plane_via_keep(self, placement):
        """(keep_via (V,) bool, plane_vias {net: [(x, y)]}) — plane vias of the window that remain at
        this placement: those within PLANE_RIP_MM of the original same-net pad of a moved part are removed."""
        V = len(self.pv_info)
        keep = torch.ones(V, dtype=torch.bool, device=self.dev)
        pts = []
        for r, v in (placement or {}).items():
            if tuple(v) == tuple(self.base_placement[r]):
                continue
            fp = self.fps[r]
            th = math.radians(fp['rot'])
            c, s = math.cos(th), math.sin(th)
            for p in fp['pads']:
                nid = self.net_id.get(p['net'], -1)
                if nid < 0 or p['net'].split('/')[-1] not in D.PLANE_NETS:
                    continue
                lx = -p['cx'] if fp['flipped'] else p['cx']
                pts.append((fp['x'] + lx * c + p['cy'] * s, fp['y'] - lx * s + p['cy'] * c, nid))
        if pts and V:
            P = torch.tensor(pts, dtype=torch.float32, device=self.dev)
            d = torch.cdist(self.pv_xy, P[:, :2])
            same = self.pv_nid[:, None] == P[:, 2].long()[None, :]
            keep = ~((d <= PLANE_RIP_MM) & same).any(dim=1)
        pvs = {}
        for k, (vx, vy, nn, _) in enumerate(self.pv_info):
            if bool(keep[k]):
                pvs.setdefault(nn, []).append((vx, vy))
        return keep, pvs

    def dP_net_base(self, net):
        """min(dP0, SDF of all fixed pocket pads except the net's own) — cached per net."""
        if net not in self._dP_base:
            gid = PAD_GROUP_OFFSET + self.net_id[net]
            pf = self.pp_fixed
            if par.enabled():
                if not hasattr(self, '_excl_fixed'):
                    gids = [PAD_GROUP_OFFSET + i for i in range(len(self.nets))]
                    self._excl_fixed = sdf.excl_min(sdf.dist_map_grouped(pf, self.win, gids))
                self._dP_base[net] = torch.minimum(self.dP0, self._excl_fixed[self.net_id[net]])
                return self._dP_base[net]
            others = pf.select(pf.t['group'] != gid, poly_keep=lambda g, gid=gid: g != gid)
            self._dP_base[net] = sdf.dist_map(others, self.win, group=None, out=self.dP0.clone())
        return self._dP_base[net]

    def d_own_fixed(self, net):
        """Distance (cap 0.3) to the fixed pads of the net itself — cached per net; None if there are none."""
        if net not in self._d_own_fixed:
            gid = PAD_GROUP_OFFSET + self.net_id[net]
            pf = self.pp_fixed
            sel = pf.select(pf.t['group'] == gid, poly_keep=lambda g, gid=gid: g == gid)
            self._d_own_fixed[net] = sdf.dist_map(sel, self.win, group=gid, cap=0.3) if (len(sel) or sel.polys) else None
        return self._d_own_fixed[net]

    def model_for(self, placement, moved_static=None):
        """CandidateModel for a placement. Pads of NON-pocket nets on movable parts go to the
        statics (group 0/strict), pads of pocket nets go to own/cover per net."""
        win = self.win
        pads = self.pads_of(placement)                     # pads of the movable footprints
        t = pads.t
        stat = pads.select(t['group'] < PAD_GROUP_OFFSET, poly_keep=lambda g: g < PAD_GROUP_OFFSET)
        dP = sdf.dist_map(stat, win, group=None, out=self.dP0.clone())
        pp_mov = pads.select(t['group'] >= PAD_GROUP_OFFSET, poly_keep=lambda g: g >= PAD_GROUP_OFFSET)
        mov_groups = set(pp_mov.t['group'].tolist()) | {gg for (_, gg, _) in pp_mov.polys}
        own, cover = {}, {}
        Dmov = None
        if par.enabled() and mov_groups:
            Dmov = sdf.dist_map_grouped(pp_mov, win, [PAD_GROUP_OFFSET + i for i in range(len(self.nets))], cap=0.3)
        for net, i in self.net_id.items():
            gid = PAD_GROUP_OFFSET + i
            d = self.d_own_fixed(net)                       # fixed pads of the net (cached)
            if gid in mov_groups:
                if Dmov is not None:
                    d = Dmov[i] if d is None else torch.minimum(d, Dmov[i])
                else:
                    sel = pp_mov.select(pp_mov.t['group'] == gid, poly_keep=lambda g, gid=gid: g == gid)
                    d = sdf.dist_map(sel, win, group=gid, cap=0.3, out=None if d is None else d.clone())
            if d is None:
                own[net] = torch.zeros((len(LAYERS), win.h, win.w), dtype=torch.bool, device=self.dev)
                cover[net] = own[net]
                continue
            own[net] = d <= 1e-6                          # cell centre inside copper
            cover[net] = d <= 0.5 * win.step * math.sqrt(2)
        gid_of = {n: PAD_GROUP_OFFSET + i for n, i in self.net_id.items()}
        # plane vias of the window that remain at this placement: copper (by clearance group) and drill
        keep_via, plane_vias = self.plane_via_keep(placement)
        pv = self.pv.select(keep_via[self.pv_row]) if len(self.pv) else self.pv
        pvh = self.pv_holes.select(keep_via[self.pvh_row]) if len(self.pv_holes) else self.pv_holes
        dT = sdf.dist_map(pv, win, group=0, out=self.dT0.clone())
        dS = {}
        for need in self.strict_needs:
            dS[need] = sdf.dist_map(pv, win, group=int(round(need * 100)), out=self.dS0[need].clone())
        dH = sdf.dist_map(pvh, win, group=999, out=self.dH0.clone())
        cm = CandidateModel(win, self.dev, self.cls, self.need, dP, dT, dS, self.dK, dH,
                            self.inb_for, self.inb_via, self.fine, pp_mov, gid_of, own, cover,
                            base_of=self.dP_net_base)
        cm.plane_vias = plane_vias
        cm.dV = self.dV
        return cm, pads

    def ends_of(self, placement, refs=None):
        """[(net, row, col)] — centres of pocket-net pads (in window cells) on the given footprints."""
        full = dict(self.base_placement)
        full.update(placement or {})
        out = []
        for r in (refs or (self.ic_refs + self.movable)):
            fp = self.fps[r]
            x, y, rot, flipped = full.get(r, (fp['x'], fp['y'], fp['rot'], fp['flipped']))
            th = math.radians(rot)
            c, s = math.cos(th), math.sin(th)
            for p in fp['pads']:
                if p['net'] not in self.net_id:
                    continue
                short = p['net'].split('/')[-1]
                if short.startswith('unconnected-') or short in D.PLANE_NETS:
                    continue                # unconnected pins and plane pads are not "ends"
                lx, ly = (-p['cx'] if flipped else p['cx']), p['cy']
                bx, by = x + lx * c + ly * s, y - lx * s + ly * c
                rr, cc = self.win_row(by), self.win_col(bx)
                if 0 <= rr < self.win.h and 0 <= cc < self.win.w:
                    out.append((p['net'], rr, cc, r, p['num']))
        return out

    def win_row(self, y):
        return int(round((y - self.win.y0) / self.win.step)) - self.win.r0

    def win_col(self, x):
        return int(round((x - self.win.x0) / self.win.step)) - self.win.c0

    def l0(self, cm, ends, zeros=None):
        """Locked ends: list of (net, r, c, ref, num, reach) with reach < LOCKED_CELLS."""
        return [e for e in self.l0_reach(cm, ends, zeros) if e[5] < LOCKED_CELLS]

    def l0_reach(self, cm, ends, zeros=None):
        """All ends with their reachability: list of (net, r, c, ref, num, reach)."""
        L, h, w = len(LAYERS), self.win.h, self.win.w
        z = torch.zeros((L, h, w), device=self.dev) if zeros is None else zeros
        costs = {}
        k = int(round(REACH_MM / self.win.step))
        batch_c, batch_v, starts = [], [], []
        meta = []
        for (net, r, c, ref, num) in ends:
            if net not in costs:
                costs[net] = cm.cost_for(net, z, z, z, 0.0, 0.0)
            cost, fv = costs[net]
            r0, r1 = max(0, r - k), min(h - 1, r + k)
            c0, c1 = max(0, c - k), min(w - 1, c + k)
            sub = torch.full((L, 2 * k + 1, 2 * k + 1), float('inf'), device=self.dev)
            subv = torch.zeros((2 * k + 1, 2 * k + 1), dtype=torch.bool, device=self.dev)
            sub[:, r0 - r + k:r1 - r + k + 1, c0 - c + k:c1 - c + k + 1] = cost[:, r0:r1 + 1, c0:c1 + 1]
            subv[r0 - r + k:r1 - r + k + 1, c0 - c + k:c1 - c + k + 1] = fv[r0:r1 + 1, c0:c1 + 1]
            b = len(batch_c)
            batch_c.append(sub); batch_v.append(subv)
            # start — the pad's own copper within +-k
            own = cm.own[net][:, r0:r1 + 1, c0:c1 + 1]
            idx = torch.nonzero(own, as_tuple=False)
            if idx.numel() == 0:
                idx = torch.tensor([[0, r - r0, c - c0]], device=self.dev)
            starts.append(torch.stack([torch.full((idx.shape[0],), b, device=self.dev, dtype=torch.long),
                                       idx[:, 0], idx[:, 1] + (r0 - r + k), idx[:, 2] + (c0 - c + k)]))
            meta.append((net, r, c, ref, num))
        if not batch_c:
            return []
        cost_b = torch.stack(batch_c)
        fv_b = torch.stack(batch_v)
        st = torch.cat(starts, dim=1)
        reach = self.wave.reach(cost_b, fv_b, st, iters=k)            # iters = k, as in locked.py
        return [(net, r, c, ref, num, int(n)) for (net, r, c, ref, num), n in zip(meta, reach.tolist())]


def load_geom(path):
    return json.load(open(path))
