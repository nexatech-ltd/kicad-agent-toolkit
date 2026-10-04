"""Per-layer obstacle raster of a board - the common base for placement and routing.

Why a raster rather than analytic geometry. "Where on the surface does a 1.5 mm pad fit"
and "how to run a track from A to B" are both questions about free space, and both are
solved on one grid: obstacles are drawn once, grown by the required clearance
(morphological dilation), and a spot or a path is searched in what remains.

Two rules learned the hard way:

1. The pad shape comes from KiCad, not from the (size) field. For custom pads (e.g. the
   thermal pads of a power IC) that field can say 0.15 mm while the real copper of the
   primitives is 1.3 x 1.2 mm. Rounding to a bounding rectangle is not acceptable in
   checks: a gate built on rectangles reported eight false overlaps on one QFN. Here
   TransformShapeToPolySet is used.
2. Plane pours are NOT obstacles: a pour retreats from new copper when refilled. Only
   pads, tracks, vias, keepout zones and the board edge count.

Run with an interpreter that sees both numpy AND pcbnew (e.g. KiCad's bundled Python
or a venv created with --system-site-packages).
"""
import collections
import math
import os
import sys

import numpy as np
import pcbnew

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gpuroute'))
import par  # noqa: E402  (KAT_PAR* flags)

MM = 1e6                        # KiCad nanometres per millimetre
# Copper layers by user name, top to bottom. The default is a 6-layer stack; for an 8-layer
# board (layers8.py) use KAT_SIGNAL=F.Cu,GND1,SIG1,PWR1,PWR2,SIG2,GND2,B.Cu (or take it from
# the board header automatically via maze_route.board_layers - see set_layers).
# Precedence: KAT_SIGNAL env > config key `signal_layers` (list) > the 6-layer default below.
# maze_route.set_layers() then replaces it with the copper layers declared in the board
# header unless KAT_SIGNAL is set.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project  # noqa: E402
_SIGNAL_DEFAULT = ','.join(project.get('signal_layers', ['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']))
SIGNAL = [s for s in os.environ.get('KAT_SIGNAL', _SIGNAL_DEFAULT).split(',') if s]


class Grid:
    """Grid over the board extent. Indexing [row, col] = [y, x]."""

    def __init__(self, board, step=0.1, margin=0.0):
        bb = board.GetBoardEdgesBoundingBox()
        self.step = step
        self.x0 = bb.GetLeft() / MM - margin
        self.y0 = bb.GetTop() / MM - margin
        self.w = int(math.ceil((bb.GetWidth() / MM + 2 * margin) / step)) + 1
        self.h = int(math.ceil((bb.GetHeight() / MM + 2 * margin) / step)) + 1

    def col(self, x_mm):
        return int(round((x_mm - self.x0) / self.step))

    def row(self, y_mm):
        return int(round((y_mm - self.y0) / self.step))

    def x(self, col):
        return self.x0 + col * self.step

    def y(self, row):
        return self.y0 + row * self.step

    def empty(self):
        return np.zeros((self.h, self.w), dtype=bool)

    def area(self, mask):
        """Mask area in mm^2."""
        return float(mask.sum()) * self.step * self.step

    def __repr__(self):
        return 'Grid(%dx%d, step %.2f mm, origin %.1f,%.1f)' % (
            self.w, self.h, self.step, self.x0, self.y0)


def fill_poly(grid, mask, pts):
    """Even-odd polygon fill, row by row."""
    if len(pts) < 3:
        return
    ys = [p[1] for p in pts]
    r0 = max(0, grid.row(min(ys)) - 1)
    r1 = min(grid.h - 1, grid.row(max(ys)) + 1)
    n = len(pts)
    for r in range(r0, r1 + 1):
        yc = grid.y(r)
        xs = []
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if y1 == y2:
                continue
            if (y1 <= yc < y2) or (y2 <= yc < y1):
                xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
        if not xs:
            continue
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            c0 = max(0, grid.col(xs[i]))
            c1 = min(grid.w - 1, grid.col(xs[i + 1]))
            if c1 >= c0:
                mask[r, c0:c1 + 1] = True


def fill_poly_np(grid, mask, pts, chunk=4_000_000):
    """The same even-odd fill as fill_poly, vectorised: (edge, row) pairs are built for all edges
    at once, the intersection uses the same formula x1 + (yc - y1) * (x2 - x1) / (y2 - y1) in
    float64, the column uses np.rint (banker's rounding, like round()). The result is
    bit-identical to fill_poly (verified in KAT_PAR_CHECK mode)."""
    if len(pts) < 3:
        return
    P = np.asarray(pts, dtype=np.float64)
    ys = P[:, 1]
    r0 = max(0, grid.row(float(ys.min())) - 1)
    r1 = min(grid.h - 1, grid.row(float(ys.max())) + 1)
    if r1 < r0:
        return
    x1, y1 = P[:, 0], P[:, 1]
    x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
    keep = y1 != y2
    x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
    if x1.size == 0:
        return
    lo, hi = np.minimum(y1, y2), np.maximum(y1, y2)
    ra = np.clip(np.rint((lo - grid.y0) / grid.step).astype(np.int64) - 1, r0, r1 + 1)
    rb = np.clip(np.rint((hi - grid.y0) / grid.step).astype(np.int64) + 1, r0 - 1, r1)
    cnt = np.clip(rb - ra + 1, 0, None)
    total = int(cnt.sum())
    if total == 0:
        return
    e = np.repeat(np.arange(x1.size), cnt)
    off = np.cumsum(cnt) - cnt
    rows = ra[e] + (np.arange(total) - off[e])
    yc = grid.y0 + rows * grid.step
    Y1, Y2 = y1[e], y2[e]
    cond = ((Y1 <= yc) & (yc < Y2)) | ((Y2 <= yc) & (yc < Y1))
    if not cond.any():
        return
    e, rows, yc = e[cond], rows[cond], yc[cond]
    xs = x1[e] + (yc - y1[e]) * (x2[e] - x1[e]) / (y2[e] - y1[e])
    order = np.lexsort((xs, rows))
    rows, xs = rows[order], xs[order]
    # position within the row: pairs (0,1), (2,3), ... - like range(0, len(xs) - 1, 2)
    first = np.r_[True, rows[1:] != rows[:-1]]
    start_idx = np.flatnonzero(first)
    grp = np.cumsum(first) - 1
    pos = np.arange(rows.size) - start_idx[grp]
    cols = np.rint((xs - grid.x0) / grid.step)
    same_next = np.r_[rows[1:] == rows[:-1], False]
    a = np.flatnonzero((pos % 2 == 0) & same_next)
    if a.size == 0:
        return
    c0 = np.maximum(0, cols[a]).astype(np.int64)
    c1 = np.minimum(grid.w - 1, cols[a + 1]).astype(np.int64)
    ok = c1 >= c0
    if not ok.any():
        return
    rr, c0, c1 = rows[a][ok], c0[ok], c1[ok]
    cmin, cmax = int(c0.min()), int(c1.max())
    rmin, rmax = int(rr.min()), int(rr.max())
    diff = np.zeros((rmax - rmin + 1, cmax - cmin + 2), dtype=np.int32)
    np.add.at(diff, (rr - rmin, c0 - cmin), 1)
    np.add.at(diff, (rr - rmin, c1 - cmin + 1), -1)
    sub = np.cumsum(diff, axis=1)[:, :-1] > 0
    mask[rmin:rmax + 1, cmin:cmax + 1] |= sub


def poly_fill(grid, mask, pts):
    """Pick the fill implementation by flag; in check mode run both and compare."""
    if not par.enabled():
        return fill_poly(grid, mask, pts)
    if par.check():
        a = grid.empty(); fill_poly(grid, a, pts)
        b = grid.empty(); fill_poly_np(grid, b, pts)
        if not np.array_equal(a, b):
            raise AssertionError('fill_poly_np != fill_poly: %d cells, %d vertices' % (int((a != b).sum()), len(pts)))
        mask |= a
        return
    return fill_poly_np(grid, mask, pts)


def fill_disc(grid, mask, cx, cy, radius):
    r0 = max(0, grid.row(cy - radius))
    r1 = min(grid.h - 1, grid.row(cy + radius))
    c0 = max(0, grid.col(cx - radius))
    c1 = min(grid.w - 1, grid.col(cx + radius))
    if r1 < r0 or c1 < c0:
        return
    rr = np.arange(r0, r1 + 1)[:, None] * grid.step + grid.y0 - cy
    cc = np.arange(c0, c1 + 1)[None, :] * grid.step + grid.x0 - cx
    mask[r0:r1 + 1, c0:c1 + 1] |= (rr * rr + cc * cc) <= radius * radius


def fill_segment(grid, mask, x1, y1, x2, y2, width):
    """Segment with round ends - exactly how KiCad draws a track."""
    rad = width / 2.0
    lo_x, hi_x = min(x1, x2) - rad, max(x1, x2) + rad
    lo_y, hi_y = min(y1, y2) - rad, max(y1, y2) + rad
    r0 = max(0, grid.row(lo_y)); r1 = min(grid.h - 1, grid.row(hi_y))
    c0 = max(0, grid.col(lo_x)); c1 = min(grid.w - 1, grid.col(hi_x))
    if r1 < r0 or c1 < c0:
        return
    py = np.arange(r0, r1 + 1)[:, None] * grid.step + grid.y0
    px = np.arange(c0, c1 + 1)[None, :] * grid.step + grid.x0
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        fill_disc(grid, mask, x1, y1, rad)
        return
    t = ((px - x1) * dx + (py - y1) * dy) / L2
    t = np.clip(t, 0.0, 1.0)
    ddx = px - (x1 + t * dx)
    ddy = py - (y1 + t * dy)
    mask[r0:r1 + 1, c0:c1 + 1] |= (ddx * ddx + ddy * ddy) <= rad * rad


def _polyset_outlines(ps):
    """Outer outlines of a SHAPE_POLY_SET, in millimetres."""
    out = []
    for i in range(ps.OutlineCount()):
        ol = ps.Outline(i)
        pts = [(ol.CPoint(j).x / MM, ol.CPoint(j).y / MM) for j in range(ol.PointCount())]
        if len(pts) >= 3:
            out.append(pts)
    return out


def pad_polys(pad, layer_id):
    """Real pad shape on a layer - via KiCad, not via (size)."""
    ps = pcbnew.SHAPE_POLY_SET()
    try:
        pad.TransformShapeToPolySet(ps, layer_id, 0, pcbnew.FromMM(0.005),
                                    pcbnew.ERROR_OUTSIDE)
    except Exception:
        try:
            pad.TransformShapeToPolygon(ps, layer_id, 0, pcbnew.FromMM(0.005),
                                        pcbnew.ERROR_OUTSIDE)
        except Exception:
            return []
    return _polyset_outlines(ps)


def courtyard_mask(board, grid, back=False):
    """Footprint courtyards - where new components must not be placed."""
    m = grid.empty()
    n = 0
    for fp in board.GetFootprints():
        ps = fp.GetCourtyard(pcbnew.B_CrtYd if back else pcbnew.F_CrtYd)
        outs = _polyset_outlines(ps)
        if not outs and not back:
            ps = fp.GetCourtyard(pcbnew.B_CrtYd)
            outs = _polyset_outlines(ps)
        for pts in outs:
            fill_poly(grid, m, pts)
            n += 1
    return m, n


def copper_mask(board, grid, layer, nets_skip=(), nets_only=None, kinds=('pads', 'tracks', 'vias'), basket=None):
    """Copper of pads, tracks and vias on a layer. Zone fills are NOT included.

    basket - a board object basket (Basket): rasterise from it without another SWIG pass;
    with KAT_PAR the basket is taken from the process cache automatically (index_for).

    kinds - which copper kinds to take: 'pads', 'tracks', 'vias' (all by default). The
    router needs separate maps: a track_to_pad rule (e.g. 0.15) applies to pads of any
    class, while per-class clearances apply only track <-> track.

    nets_only - if given, take copper of ONLY these nets. Needed for clearances carried by
    the copper itself: a board rule `A.NetClass == 'POWER' && A.Type == 'Track' &&
    B.Type == 'Track'` demands e.g. 0.25 mm regardless of what is nearby, so POWER copper
    must be grown by its own clearance, not by the clearance of the net being routed. This
    cannot be expressed through nets_skip: it would mean listing every other net on the board.
    """
    if basket is None and par.enabled():
        basket = index_for(board)
    if basket is not None:
        return basket.copper_mask(grid, layer, nets_skip, nets_only, kinds)
    lid = board.GetLayerID(layer)
    m = grid.empty()
    stats = {'pads': 0, 'tracks': 0, 'vias': 0}
    for fp in (board.GetFootprints() if 'pads' in kinds else ()):
        for p in fp.Pads():
            if p.GetNetname() in nets_skip:
                continue
            if nets_only is not None and p.GetNetname() not in nets_only:
                continue
            if not p.IsOnLayer(lid):
                continue
            for pts in pad_polys(p, lid):
                fill_poly(grid, m, pts)
            stats['pads'] += 1
    for t in board.GetTracks():
        if t.GetNetname() in nets_skip:
            continue
        if nets_only is not None and t.GetNetname() not in nets_only:
            continue
        if t.GetClass() == 'PCB_VIA':
            if 'vias' not in kinds:
                continue
            v = t.Cast()
            if v.IsOnLayer(lid):
                # In KiCad 10 a via's width depends on the layer (complex padstacks),
                # and GetWidth() without an argument only logs a warning.
                try:
                    w = v.GetWidth(lid)
                except TypeError:
                    w = v.GetWidth()
                fill_disc(grid, m, v.GetPosition().x / MM, v.GetPosition().y / MM,
                          w / MM / 2.0)
                stats['vias'] += 1
            continue
        if t.GetLayer() != lid or 'tracks' not in kinds:
            continue
        fill_segment(grid, m, t.GetStart().x / MM, t.GetStart().y / MM,
                     t.GetEnd().x / MM, t.GetEnd().y / MM, t.GetWidth() / MM)
        stats['tracks'] += 1
    return m, stats


def hole_mask(board, grid, extra=0.0, nets_skip=()):
    """All holes of the board - plated-through pads, vias, mounting holes.

    `nets_skip` excludes holes of the listed nets. Needed for the "where a track may go"
    mask: clearance to the net's OWN hole is not a violation, and growing its own hole by
    the clearance locks a pre-placed via - a ring of occupied cells remains around it and
    the router answers "no path". For the "where a via may go" mask nothing may be excluded:
    hole-to-hole applies regardless of net.
    """
    skip = {n.split('/')[-1] for n in nets_skip}
    m = grid.empty()
    n = 0
    for t in board.GetTracks():
        if t.GetClass() != 'PCB_VIA':
            continue
        if t.GetNetname().split('/')[-1] in skip:
            continue
        v = t.Cast()
        fill_disc(grid, m, v.GetPosition().x / MM, v.GetPosition().y / MM,
                  v.GetDrill() / MM / 2.0 + extra)
        n += 1
    for fp in board.GetFootprints():
        for p in fp.Pads():
            d = p.GetDrillSizeX() / MM
            if d <= 0:
                continue
            if p.GetNetname().split('/')[-1] in skip:
                continue
            fill_disc(grid, m, p.GetPosition().x / MM, p.GetPosition().y / MM,
                      d / 2.0 + extra)
            n += 1
    return m, n


def keepout_mask(board, grid, layer):
    """Keepout (rule area) zones active on a layer."""
    lid = board.GetLayerID(layer)
    m = grid.empty()
    n = 0
    for z in board.Zones():
        if not z.GetIsRuleArea():
            continue
        if not z.IsOnLayer(lid):
            continue
        ol = z.Outline()
        for i in range(ol.OutlineCount()):
            o = ol.Outline(i)
            pts = [(o.CPoint(j).x / MM, o.CPoint(j).y / MM) for j in range(o.PointCount())]
            fill_poly(grid, m, pts)
            n += 1
    return m, n


def inside_board(board, grid, inset=0.3):
    """Mask "inside the board outline, with an inset"."""
    m = grid.empty()
    ps = pcbnew.SHAPE_POLY_SET()
    board.GetBoardPolygonOutlines(ps, True)
    for pts in _polyset_outlines(ps):
        fill_poly(grid, m, pts)
    if inset > 0:
        m = erode(m, inset, grid.step)
    return m


def _pool(mask, k, op):
    """Morphology with a square 2k+1 kernel via cumulative sums - no scipy."""
    if k <= 0:
        return mask
    a = mask.astype(np.int32)
    pad = np.zeros((a.shape[0] + 2 * k, a.shape[1] + 2 * k), dtype=np.int32)
    pad[k:k + a.shape[0], k:k + a.shape[1]] = a
    s = pad.cumsum(0).cumsum(1)
    s = np.pad(s, ((1, 0), (1, 0)))
    n = 2 * k + 1
    H, W = a.shape
    box = (s[n:n + H, n:n + W] - s[0:H, n:n + W]
           - s[n:n + H, 0:W] + s[0:H, 0:W])
    return box > 0 if op == 'dilate' else box == n * n


def dilate(mask, radius_mm, step):
    return _pool(mask, int(math.ceil(radius_mm / step)), 'dilate')


def erode(mask, radius_mm, step):
    return _pool(mask, int(math.ceil(radius_mm / step)), 'erode')


def courtyard_mask_except(board, grid, skip_refs=()):
    """Like courtyard_mask, but without the footprints in skip_refs.

    Needed for placement: a footprint being moved must not be an obstacle to itself, and a
    group of new footprints dumped in one spot would otherwise create a false obstacle
    exactly where they need to be laid out.
    """
    m = grid.empty()
    n = 0
    for fp in board.GetFootprints():
        if fp.GetReference() in skip_refs:
            continue
        ps = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
        outs = _polyset_outlines(ps)
        if not outs:
            ps = fp.GetCourtyard(pcbnew.F_CrtYd if fp.IsFlipped() else pcbnew.B_CrtYd)
            outs = _polyset_outlines(ps)
        for pts in outs:
            fill_poly(grid, m, pts)
            n += 1
    return m, n


def zone_fill_mask(board, grid, layer, net=None):
    """Copper of zone FILLS on a layer (unlike copper_mask, which has no fills).

    Needed where the fill itself is what is checked: continuity of the reference plane
    under an RF feed line is a property of the fill, not of tracks.
    """
    lid = board.GetLayerID(layer)
    m = grid.empty()
    n = 0
    for z in board.Zones():
        if z.GetIsRuleArea() or not z.IsOnLayer(lid):
            continue
        if net is not None and z.GetNetname() != net:
            continue
        try:
            poly = z.GetFilledPolysList(lid)
        except Exception:
            continue
        for i in range(poly.OutlineCount()):
            ol = poly.Outline(i)
            pts = [(ol.CPoint(j).x / MM, ol.CPoint(j).y / MM)
                   for j in range(ol.PointCount())]
            poly_fill(grid, m, pts)
            n += 1
        # subtract holes in the fill (cut-outs around other copper): one hole mask per zone
        # (formerly a sub-raster allocation and a full m &= ~hm for EACH hole); same result
        hm = None
        for i in range(poly.OutlineCount()):
            for h in range(poly.HoleCount(i)):
                hl = poly.Hole(i, h)
                pts = [(hl.CPoint(j).x / MM, hl.CPoint(j).y / MM)
                       for j in range(hl.PointCount())]
                if hm is None:
                    hm = grid.empty()
                poly_fill(grid, hm, pts)
        if hm is not None:
            m &= ~hm
    return m, n


class Basket:
    """Board objects collected in ONE SWIG pass: pads (polygons per layer, drill), tracks
    (ends, width per layer), vias (centre, diameter per layer, drill) - grouped by net, with
    no references to pcbnew. A raster from the basket is bit-identical to copper_mask on the
    board: the same primitives (the same expressions over the same numbers), the same fill,
    and OR is order-independent. copper_mask with nets_only={net} walked all ~16k tracks of
    a board through SWIG on every call (384 calls = 11.5 s per model) - here only the
    baskets of the requested nets are visited."""

    def __init__(self, board, layers=SIGNAL):
        self.layers = list(layers)
        lids = {L: board.GetLayerID(L) for L in self.layers}
        self.pads = collections.defaultdict(list)      # net -> [(x, y, drill, {L: [pts, …]})]
        self.tracks = collections.defaultdict(lambda: collections.defaultdict(list))  # net -> L -> [(x1,y1,x2,y2,w)]
        self.vias = collections.defaultdict(list)      # net -> [(x, y, drill, {L: w})]
        self.pad_refs = collections.defaultdict(list)  # net -> [ref] (parallel to self.pads)
        self.nets = set()
        for fp in board.GetFootprints():
            ref = fp.GetReference()
            for p in fp.Pads():
                net = p.GetNetname()
                polys = {}
                for L, lid in lids.items():
                    if p.IsOnLayer(lid):
                        polys[L] = pad_polys(p, lid)
                pos = p.GetPosition()
                self.pads[net].append((pos.x / MM, pos.y / MM, p.GetDrillSizeX() / MM, polys))
                self.pad_refs[net].append(ref)
                self.nets.add(net)
        lname = {lid: L for L, lid in lids.items()}
        for t in board.GetTracks():
            net = t.GetNetname()
            self.nets.add(net)
            if t.GetClass() == 'PCB_VIA':
                v = t.Cast()
                wl = {}
                for L, lid in lids.items():
                    if v.IsOnLayer(lid):
                        try:
                            wl[L] = v.GetWidth(lid) / MM
                        except TypeError:
                            wl[L] = v.GetWidth() / MM
                pos = v.GetPosition()
                self.vias[net].append((pos.x / MM, pos.y / MM, v.GetDrill() / MM, wl))
                continue
            L = lname.get(t.GetLayer())
            if L is None:
                continue
            s_, e_ = t.GetStart(), t.GetEnd()
            self.tracks[net][L].append((s_.x / MM, s_.y / MM, e_.x / MM, e_.y / MM, t.GetWidth() / MM))

    def _nets(self, nets_skip, nets_only, table):
        if nets_only is not None:
            return [n for n in nets_only if n not in nets_skip and n in table]
        return [n for n in table if n not in nets_skip]

    def copper_mask(self, grid, layer, nets_skip=(), nets_only=None, kinds=('pads', 'tracks', 'vias')):
        m = grid.empty()
        stats = {'pads': 0, 'tracks': 0, 'vias': 0}
        if 'pads' in kinds:
            for n in self._nets(nets_skip, nets_only, self.pads):
                for (x, y, drill, polys) in self.pads[n]:
                    pl = polys.get(layer)
                    if pl is None:
                        continue
                    for pts in pl:
                        poly_fill(grid, m, pts)
                    stats['pads'] += 1
        if 'vias' in kinds:
            for n in self._nets(nets_skip, nets_only, self.vias):
                for (x, y, drill, wl) in self.vias[n]:
                    w = wl.get(layer)
                    if w is None:
                        continue
                    fill_disc(grid, m, x, y, w / 2.0)
                    stats['vias'] += 1
        if 'tracks' in kinds:
            for n in self._nets(nets_skip, nets_only, self.tracks):
                for (x1, y1, x2, y2, w) in self.tracks[n].get(layer, ()):
                    fill_segment(grid, m, x1, y1, x2, y2, w)
                    stats['tracks'] += 1
        return m, stats

    def holes(self, skip_nets=()):
        """(x, y, drill radius) of vias and plated-through pads, except nets in skip_nets - like
        Model._holes (order: vias first, then pads; order does not matter for OR masks)."""
        out = []
        for n, lst in self.vias.items():
            if n in skip_nets:
                continue
            for (x, y, drill, wl) in lst:
                out.append((x, y, drill / 2.0))
        for n, lst in self.pads.items():
            if n in skip_nets:
                continue
            for (x, y, drill, polys) in lst:
                if drill > 0:
                    out.append((x, y, drill / 2.0))
        return out


_BASKETS = {}


def index_for(board):
    """Board basket for single copper_mask calls. The key is the board object AND the track/
    footprint counts: ripup.py removes copper and restores it between attempts within one
    process, and a cache keyed by id(board) returned a stale basket without the restored copper
    (which produced shorts to a power net in a rip-up campaign). The router and pocket models
    rebuild their basket themselves (0.12 s)."""
    key = (id(board), len(board.GetTracks()), len(board.GetFootprints()))   # in KiCad 10 GetTracks() returns a list, no .size()
    if key not in _BASKETS:
        _BASKETS.clear()
        _BASKETS[key] = Basket(board)
    return _BASKETS[key]
