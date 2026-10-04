"""Placement-candidate model in a pocket window — the gpuroute.model.Model interface (cost_for,
footprint, free_for, own, cover, fine, labels), built from the GPU maps of place/sdf.

Window statics: dP — pads outside the pocket (including moved pads of foreign nets on movable
parts), dT — tracks/vias, dS — tracks of strict-clearance nets, dK — keepouts, dH — drill holes.
Pads of pocket nets (on ICs, movable and fixed footprints) are kept in the pocket_pads table
(group = 1000 + net index), and the free mask is built PER NET: exact SDF to all pocket pads
except its own, through the same model.build_free. The previous approach — cover mask plus an
integer dilation in cost_for (fixed) — is ~0.1 mm more conservative than the exact distance at a
0.5 mm pitch and locked a QFN's own pad even for a signal net (every cell of the pad was "blocked").
"""
import torch

import model as M
import par
import sdf


class WinGrid:
    """Window grid: (r, c) indices inside the window; x()/y() return board coordinates."""

    def __init__(self, win):
        self.step = win.step
        self.h, self.w = win.h, win.w
        self.r0, self.c0 = win.r0, win.c0
        self.x0 = win.x0 + win.c0 * win.step
        self.y0 = win.y0 + win.r0 * win.step

    def x(self, c):
        return self.x0 + c * self.step

    def y(self, r):
        return self.y0 + r * self.step

    def row(self, y):
        return int(round((y - self.y0) / self.step))

    def col(self, x):
        return int(round((x - self.x0) / self.step))


class CandidateModel(M.ModelBase):
    fixed_pad = None          # foreign pads are handled exactly in free_for, not by a cover mask

    def __init__(self, win, dev, cls, need, dP, dT, dS, dK, dH, inb_for, inb_via, fine,
                 pocket_pads, gid_of, own, cover, labels=None, base_of=None):
        self.g = WinGrid(win)
        self.win = win
        self.dev = dev
        self.cls, self.need = cls, need
        self.dP, self.dT, self.dS, self.dK, self.dH = dP, dT, dS, dK, dH
        self.inb_for, self.inb_via = inb_for, inb_via
        self.fine = fine
        self.pocket_pads, self.gid_of = pocket_pads, gid_of      # pads of MOVABLE parts, per net
        self.base_of = base_of                                    # net -> cached map of fixed foreign pads
        self.own, self.cover = own, cover
        self.labels = labels or {}
        self.nets = list(cls)
        self.fixed = {}
        self.k_via = M.cells(M.MR.VIA_SIZE / 2, win.step)
        self._free = {}
        self._excl = None
        self._gorder = list(self.gid_of)

    def excl_mov(self, net):
        """KAT_PAR: map of "all movable pads except this net's" from a single grouped computation."""
        if self._excl is None:
            gids = [self.gid_of[n] for n in self._gorder]
            self._excl = sdf.excl_min(sdf.dist_map_grouped(self.pocket_pads, self.win, gids))
        return self._excl[self._gorder.index(net)]

    def free_for(self, net):
        """Free-space mask for a net: statics + all pocket pads except its own."""
        if net not in self._free:
            gid = self.gid_of.get(net, -1)
            pp = self.pocket_pads
            if par.enabled() and net in self.gid_of:
                dP = torch.minimum(self.base_of(net), self.dP) if self.base_of else self.dP
                dP = torch.minimum(dP, self.excl_mov(net))
            else:
                others = pp.select(pp.t['group'] != gid, poly_keep=lambda g, gid=gid: g != gid)
                dP = torch.minimum(self.base_of(net), self.dP) if self.base_of else self.dP.clone()
                dP = sdf.dist_map(others, self.win, group=None, out=dP)
            key = self.cls[net]
            f, fv = M.build_free(dP, self.dT, self.dK, self.dS, self.dH, self.inb_for, self.inb_via,
                                 self.fine, {key})
            self._free[net] = (f[key], fv[key])
        return self._free[net]

    # for code that expects per-group dicts (statics only, pocket pads not included)
    @property
    def free(self):
        if not hasattr(self, '_free_grp'):
            self._free_grp, self._fv_grp = M.build_free(self.dP, self.dT, self.dK, self.dS, self.dH,
                                                        self.inb_for, self.inb_via, self.fine, set(self.cls.values()))
        return self._free_grp

    @property
    def free_via(self):
        self.free
        return self._fv_grp
