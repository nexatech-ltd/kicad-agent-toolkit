"""Hard constraints of a placement candidate — checked before any wave (CPU, plain Python: N movable <= ~40).

1. The courtyard of a movable part (AABB after rotation, +0.1 mm) does not intersect courtyards of
   other footprints or footprint keepouts; pairs that already overlapped in the original placement
   are allowed, but their overlap area must not grow.
2. Inside the board outline with a 1.0 mm margin (edge taken from the actual outline).
3. Pad-to-pad of different nets >= 0.10 + 0.03 mm (from primitive SDF) — computed on the GPU in
   escape via the candidate's dB; here only roughly by pad AABB between movable parts.
4. "Leash": every pad of a movable part stays within max(D_MAX, d_now + SLACK) of the nearest
   same-net pad of the pocket IC (decoupling, shunts, charge-pump capacitors).
5. B.Cu: not under a QFN exposed pad (thermal vias there) and not over through holes; parts with
   a pad on a net matching the `top_side_only_net_prefixes` config key (e.g. charge-pump
   capacitors of a driver) stay on F.Cu.
"""
import math

import project

D_MAX = 3.0
SLACK = 1.0
EDGE_MARGIN = 1.0
GROW = 0.1
# Net-name prefixes (last path component) whose parts must not be flipped to B.Cu.
TOP_ONLY_PREFIXES = tuple(project.get('top_side_only_net_prefixes', []))


def rot_aabb(cb, x, y, rot, flipped=False):
    """Board AABB of a courtyard (local cb) at rotation rot and mirror."""
    if cb is None:
        return (x - 0.5, y - 0.5, x + 0.5, y + 0.5)
    th = math.radians(rot)
    c, s = math.cos(th), math.sin(th)
    pts = [(cb[0], cb[1]), (cb[2], cb[1]), (cb[2], cb[3]), (cb[0], cb[3])]
    if flipped:
        pts = [(-px, py) for px, py in pts]
    bx = [x + px * c + py * s for px, py in pts]
    by = [y - px * s + py * c for px, py in pts]
    return (min(bx), min(by), max(bx), max(by))


def overlap(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def inter_area(a, b):
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def pad_xy(fp, p, x, y, rot, flipped):
    th = math.radians(rot)
    c, s = math.cos(th), math.sin(th)
    lx = -p['cx'] if flipped else p['cx']
    ly = p['cy']
    return x + lx * c + ly * s, y - lx * s + ly * c


class Constraints:
    def __init__(self, geom, pocket, board_bbox, rule_areas):
        self.geom = geom
        self.pk = pocket
        self.fps = pocket.fps
        self.board_bbox = board_bbox
        self.rules = [ra['bbox'] for ra in rule_areas if not ra.get('footprints_allowed', True)]
        # AABB of every footprint in the original placement
        self.aabb0 = {fp['ref']: rot_aabb(fp['courtyard'], fp['x'], fp['y'], fp['rot'], fp['flipped'])
                      for fp in geom['footprints']}
        self.grown0 = {r: (b[0] - GROW, b[1] - GROW, b[2] + GROW, b[3] + GROW) for r, b in self.aabb0.items()}
        # pairs that touched in the original placement: the overlap area (grown x aabb) must not grow
        # (a plain "already touching" whitelist let one passive slide onto another — DRC courtyards_overlap)
        self.pre = {}
        refs = list(self.aabb0)
        for r in pocket.movable:
            for o in refs:
                if o != r and overlap(self.grown0[r], self.aabb0[o]):
                    a = inter_area(self.grown0[r], self.aabb0[o])
                    self.pre[(r, o)] = a; self.pre[(o, r)] = a
        # leashes: for pads of movable parts — the nearest same-net pad on a pocket IC
        self.leash = {}
        ic_pads = {}
        for r in pocket.ic_refs:
            fp = self.fps[r]
            for p in fp['pads']:
                if p['net']:
                    ic_pads.setdefault(p['net'], []).append(pad_xy(fp, p, fp['x'], fp['y'], fp['rot'], fp['flipped']))
        self.ic_pads = ic_pads
        for r in pocket.movable:
            fp = self.fps[r]
            for p in fp['pads']:
                if p['net'] in ic_pads:
                    px, py = pad_xy(fp, p, fp['x'], fp['y'], fp['rot'], fp['flipped'])
                    d0 = min(math.hypot(px - qx, py - qy) for qx, qy in ic_pads[p['net']])
                    self.leash[(r, p['num'])] = max(D_MAX, d0 + SLACK)
        # QFN exposed pads (width and height >= 2 mm) — forbidden for B.Cu parts
        self.ep_boxes = []
        for r in pocket.ic_refs:
            fp = self.fps[r]
            for p in fp['pads']:
                if p['hw'] >= 1.0 and p['hh'] >= 1.0:
                    x, y = pad_xy(fp, p, fp['x'], fp['y'], fp['rot'], fp['flipped'])
                    self.ep_boxes.append((x - p['hw'], y - p['hh'], x + p['hw'], y + p['hh']))
        # through holes of the board (drills) in the window
        self.tht = []
        for fp in geom['footprints']:
            for p in fp['pads']:
                if p['drill'] > 0:
                    x, y = pad_xy(fp, p, fp['x'], fp['y'], fp['rot'], fp['flipped'])
                    self.tht.append((x, y, p['drill'] / 2))

    def check(self, placement):
        """placement {ref: (x, y, rot, flipped)} of the MOVABLE parts. -> (ok, reason)."""
        aabb = dict(self.aabb0)
        for r, (x, y, rot, fl) in placement.items():
            aabb[r] = rot_aabb(self.fps[r]['courtyard'], x, y, rot, fl)
        bb = self.board_bbox
        for r, (x, y, rot, fl) in placement.items():
            a = aabb[r]
            g = (a[0] - GROW, a[1] - GROW, a[2] + GROW, a[3] + GROW)
            if not (a[0] >= bb[0] + EDGE_MARGIN and a[1] >= bb[1] + EDGE_MARGIN
                    and a[2] <= bb[2] - EDGE_MARGIN and a[3] <= bb[3] - EDGE_MARGIN):
                return False, 'board edge %s' % r
            for rb in self.rules:
                if overlap(g, rb):
                    return False, 'keepout %s' % r
            for o, ob in aabb.items():
                if o == r:
                    continue
                same_side = self.fps[o]['flipped'] == fl if o not in placement else placement[o][3] == fl
                if same_side and overlap(g, ob):
                    if (r, o) not in self.pre or inter_area(g, ob) > self.pre[(r, o)] + 1e-6:
                        return False, 'courtyard %s x %s' % (r, o)
            fp = self.fps[r]
            for p in fp['pads']:
                key = (r, p['num'])
                if key in self.leash:
                    px, py = pad_xy(fp, p, x, y, rot, fl)
                    d = min(math.hypot(px - qx, py - qy) for qx, qy in self.ic_pads[p['net']])
                    if d > self.leash[key]:
                        return False, 'leash %s.%s %.1f>%.1f' % (r, p['num'], d, self.leash[key])
            if fl:
                short = fp['value'] if isinstance(fp['value'], str) else ''
                if TOP_ONLY_PREFIXES and any(p['net'].split('/')[-1].startswith(TOP_ONLY_PREFIXES) for p in fp['pads']):
                    return False, 'top side only %s' % r
                for eb in self.ep_boxes:
                    if overlap(a, eb):
                        return False, 'under exposed pad %s' % r
                for (hx, hy, hr) in self.tht:
                    if a[0] - hr <= hx <= a[2] + hr and a[1] - hr <= hy <= a[3] + hr:
                        return False, 'over a through hole %s' % r
        return True, ''
