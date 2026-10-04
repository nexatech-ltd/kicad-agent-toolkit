#!/usr/bin/env python3
"""Single-net maze router: what Freerouting could not finish.

Why a custom one. After about ten rounds Freerouting reached a plateau: over a hundred
`Pad`+`Pad` breaks with a median of 18 mm remained, and the engine closed 0-1 of them per
round in any mode. Copper density was only 3-5 %: each connection is routable on its own,
they just do not all fit in one go. That is exactly the job of an incremental per-net router.

Design. A 0.1 mm grid over the board extent, all signal layers. Obstacles come from
tools/raster.py: pads by their real shape (not a bounding rectangle), tracks, vias, keepout
zones, an inset from the outline. Plane pours are NOT obstacles - they retreat on refill.
The search is A* over the layers with a per-layer cost and a via cost; the search window is
the bounding box of the endpoints plus a margin.

The layer cost expresses a policy: F.Cu and B.Cu are cheap, inner signal layers cost more,
and inner layers declared as type "power" in the board's layer stack (planes) cost the most,
since cutting a plane - especially the reference ground of the outer layers - is the last
resort. Override per layer with KAT_LAYER_COST. An antenna feed area is a keepout on all layers.

    python3 tools/maze_route.py --list          # what is pending
    python3 tools/maze_route.py --limit 5
    python3 tools/maze_route.py --limit 20 --apply

(run with an interpreter that sees numpy and pcbnew)
"""
import argparse
import collections
import heapq
import json
import math
import os
import re
import shutil
import sys
import time
import uuid as _uuid

import numpy as np
import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drcinfo as D
import paths
import project
import raster

MM = 1e6
STEP = 0.1                     # grid step, mm
# Search window margin around the break endpoints. It used to be 12 mm, which turned out to
# be an artificial limit: connections longer than 60 mm answered "NO PATH" because the
# detour simply did not fit in the window. With 25 mm the router closed 6 of 11 long
# connections, including a 107 mm one (116.6 mm of copper, 29 segments, 2 vias, 20 s), and
# DRC stayed clean.
MARGIN = 25.0
# Board min_copper_edge_clearance (config key `copper_edge_clearance`); a smaller value made
# the router produce copper_edge_clearance violations.
EDGE_INSET = float(project.get('copper_edge_clearance', 0.50))
# Per-step layer cost and layer-change cost. The numbers express policy, not physics:
# F.Cu and B.Cu have plenty of room, cutting a power plane is undesirable, cutting the
# reference ground even more so.
LAYER_COST = {'F.Cu': 1.0, 'B.Cu': 1.0}
INNER_LAYER_COST = 1.8         # inner layer of type signal/mixed
POWER_LAYERS = set()       # filled from the board header by board_layers()
PLANE_LAYER_COST = 3.0         # inner layer of type "power" in the board header (a plane)
# KAT_LAYER_COST=In1.Cu:8,In4.Cu:8 - override costs by layer name (canonical or user alias)
for _kv in os.environ.get('KAT_LAYER_COST', '').split(','):
    if ':' in _kv:
        _k, _v = _kv.split(':', 1)
        LAYER_COST[_k.strip()] = float(_v)


def _default_layer_costs():
    """Every signal layer needs a cost: unknown inner layer names (e.g. In1.Cu on a board
    without layer aliases) get INNER_LAYER_COST instead of a KeyError in the search."""
    for _L in raster.SIGNAL:
        LAYER_COST.setdefault(_L, 1.0 if _L in ('F.Cu', 'B.Cu')
                              else PLANE_LAYER_COST if _L in POWER_LAYERS else INNER_LAYER_COST)


_default_layer_costs()
VIA_COST = float(os.environ.get('KAT_VIA_COST', '12.0'))   # in grid steps; a through via pierces every layer
# KAT_VIA_COST - raise the layer-change cost. Useful for RF: every via on a feed line is an
# impedance discontinuity, and an extra 10 mm detour on one layer is cheaper than two vias.
# KAT_VIA="0.5,0.2" - size/drill of the router's new vias.
# A 0.5 mm-pitch fan-out at 0.15 clearance needs a via <= 0.5: 0.25 + 0.15 + 0.075 = 0.475 < 0.5.
# Default 0.5/0.2 (0.15 annular ring - standard at common fabs). A campaign on a copy of a
# dense board closed none of the nets in a fine-pitch MCU escape corridor with 0.6/0.3, and
# four of them in one round with 0.5/0.2; 0.45/0.2 adds more, but is an "advanced" option at
# several fabs.
VIA_SIZE, VIA_DRILL = (float(v) for v in os.environ.get('KAT_VIA', '0.5,0.2').split(','))
HOLE_TO_HOLE = 0.25           # margin to another hole; typical board minimum is 0.2 mm
# The router lays a NARROW track and widen_net.py widens it later. This is not laziness: a
# 1.2 mm power-class track cannot leave a 0.5 mm-pitch pad - the first three attempts failed
# exactly on this ("endpoints not in raster"). Real routing necks down at the pad, and the
# toolkit already has KEEP_NARROW in audit_widths.py and a widening tool driven by actual
# clearances.
# 0.15 (was 0.25): escaping from under a 0.5 mm-pitch QFN next to a stub of a strict-clearance
# net (0.25) is only possible with a narrow track; on a test board this raised closed breaks
# from 2 to 8 of 37. widen_net.py brings the width up to the class width.
ROUTE_WIDTH = float(os.environ.get('KAT_ROUTE_WIDTH', '0.15'))
DIAG = math.sqrt(2.0)
# In the board file copper refers to the CANONICAL layer name (In1.Cu); a user name ("GND1")
# is only an alias in the (layers ...) section. pcbnew's GetLayerName returns the alias, and
# writing it into the file makes the board unloadable: "items were found on undefined
# layers (PWR1, PWR2)".
CANON = {L: L for L in ('F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu')}


def board_layers(board_path):
    """[(canonical, user name)] of copper layers from the (layers ...) header of the board
    file, top to bottom (F.Cu, In1..InN, B.Cu). User name = canonical when there is no alias."""
    head = open(board_path).read(20000)
    m = re.search(r'\t\(layers\n((?:\t\t\(\d+ "[^"]+" (?:signal|power|mixed|jumper)(?: "[^"]+")?\)\n)+)', head)
    if not m:
        return None
    rows = re.findall(r'\((\d+) "([^"]+)" (signal|power|mixed|jumper)(?: "([^"]+)")?\)', m.group(1))
    for i, c, t, u in rows:
        if c.endswith('.Cu') and t == 'power':
            POWER_LAYERS.update({c, u or c})
    rows = [(int(i), c, u or c) for i, c, t, u in rows if c.endswith('.Cu')]
    order = lambda r: (0 if r[1] == 'F.Cu' else 2 if r[1] == 'B.Cu' else 1, r[0])
    return [(c, u) for _, c, u in sorted(rows, key=order)]


def set_layers(board_path):
    """Adapt CANON (and raster.SIGNAL unless KAT_SIGNAL is set) to the board header - call at
    the start of tools that work on a board with a different stack (e.g. 8 layers)."""
    rows = board_layers(board_path)
    if not rows:
        return
    CANON.clear()
    CANON.update({u: c for c, u in rows})
    if not os.environ.get('KAT_SIGNAL'):
        raster.SIGNAL[:] = [u for c, u in rows]
    _default_layer_costs()


if os.path.exists(paths.BOARD):
    set_layers(paths.BOARD)


_CLASSES = None


def load_classes(board_path):
    """Net classes and their patterns from the board's .kicad_pro.

    Not taken via pcbnew: in KiCad 10 `net.GetNetClass()` returns a SwigPyObject without
    methods (`'SwigPyObject' object has no attribute 'GetTrackWidth'`). Class settings live
    in the project file, and the other tools read them from there too.
    """
    global _CLASSES
    if _CLASSES is not None:
        return _CLASSES
    pro = paths.sibling(board_path, '.kicad_pro')
    with open(pro) as f:
        d = json.load(f)
    ns = d.get('net_settings', {})
    by_name = {c['name']: c for c in ns.get('classes', [])}
    pats = [(p['netclass'], p['pattern']) for p in ns.get('netclass_patterns', [])]
    _CLASSES = (by_name, pats)
    return _CLASSES


# Track-to-track clearances per net class from the board's .kicad_dru. The net classes do
# not have them: their base clearance is small, and the real requirements are imposed by
# board rules only on the copper we draw. A router that read only the net class put a
# signal track 0.15 mm from a power-class track that required 0.25.
# {netclass: mm}; see project.class_track_clearance() (config key `class_clearance`).
DRU_CLEARANCE = project.class_track_clearance()

# A courtyard escape rule in the board's .kicad_dru (e.g. `fine_pitch_escape`): inside the
# courtyard of these footprints the clearance is relaxed (e.g. 0.09 mm). With a 0.4 mm
# pitch (WQFN/VQFN power ICs, small secure elements) adjacent pads are 0.2 mm apart, and
# demanding the general 0.15 - let alone 0.25 for a power class - forbids escaping the
# package at all.
#
# Without this rule the router answered "NO PATH" for a 0.82 mm break between adjacent pads
# of one IC: it demanded 0.25 + 0.25 = 0.75 mm where the package gives 0.2. Dozens of
# breaks hung on two such ICs.
# KAT_FINE_REFS=U1,U2 overrides the footprints the escape rule applies to; by default they
# are read from the rule's insideCourtyard() conditions (and config key `fine_pitch_refs`).
FINE_PITCH_REFS = tuple(r for r in (os.environ.get('KAT_FINE_REFS', '').split(',')
                                    if os.environ.get('KAT_FINE_REFS')
                                    else sorted(project.fine_pitch_refs())) if r)
FINE_PITCH_CLEARANCE = project.fine_pitch_clearance(default=0.09)


def strict_nets(board_path, board):
    """Nets whose copper requires a clearance LARGER than that of the net being routed.

    A board rule is written as `A.NetClass == 'POWER' && A.Type == 'Track' &&
    B.Type == 'Track'` - i.e. the requirement is carried by the POWER copper ITSELF,
    regardless of what is nearby. The router used to take only the routed net's clearance
    and, routing a Default net at 0.15, came within 0.15 of POWER copper instead of 0.25.

    Returns {full net name: required clearance}.
    """
    import fnmatch
    by_name, pats = load_classes(board_path)
    out = {}
    seen = set()
    for fp in board.GetFootprints():
        for p in fp.Pads():
            seen.add(p.GetNetname())
    for t in board.GetTracks():
        seen.add(t.GetNetname())
    for net in seen:
        if not net:
            continue
        base = net.split('/')[-1]
        cname = 'Default'
        for cls, pat in pats:
            if fnmatch.fnmatch(base, pat):
                cname = cls
                break
        need = DRU_CLEARANCE.get(cname, 0.0)
        if need:
            out[net] = need
    return out


def net_class_width(board_path, net):
    """Width and clearance of a net from its class; the Default class if none matches."""
    import fnmatch
    by_name, pats = load_classes(board_path)
    base = net.split('/')[-1]
    cname = 'Default'
    for cls, pat in pats:
        if fnmatch.fnmatch(base, pat):
            cname = cls
            break
    c = by_name.get(cname, {})
    w = min(float(c.get('track_width', 0.2)), ROUTE_WIDTH)
    cl = float(c.get('clearance', 0.15))
    return max(w, 0.15), max(cl, 0.15, DRU_CLEARANCE.get(cname, 0.0))


class Field:
    """Free space for one net: per layer, a boolean array "a track centre may go here"."""

    def __init__(self, board, g, net, width, clearance):
        self.g = g
        self.layers = [L for L in raster.SIGNAL]
        rad = width / 2.0 + clearance
        inb = raster.inside_board(board, g, inset=EDGE_INSET + width / 2.0)
        own = {n for n in (net,)}
        # Mask of fine-pitch footprints: a relaxed clearance applies inside it.
        fine = getattr(Field, '_fine', None)
        if fine is None:
            fine = g.empty()
            for fp in board.GetFootprints():
                if fp.GetReference() not in FINE_PITCH_REFS:
                    continue
                ps = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
                outs = raster._polyset_outlines(ps)
                for pts in outs:
                    raster.fill_poly(g, fine, pts)
            Field._fine = fine
        strict = getattr(Field, '_strict', None)
        if strict is None:
            strict = {}
        self.free = {}
        for L in self.layers:
            # copper of ordinary nets - grow by our own clearance
            skip_norm = own | set(strict)
            cu, _ = raster.copper_mask(board, g, L, nets_skip=skip_norm)
            ko, _ = raster.keepout_mask(board, g, L)
            # own holes are excluded from the track mask: clearance to one's own hole is not a violation
            holes, _ = raster.hole_mask(board, g, extra=clearance + width / 2.0, nets_skip=own)
            m = (inb & ~raster.dilate(cu, rad, g.step)
                 & ~raster.dilate(ko, width / 2.0, g.step)
                 & ~holes)
            # Inside the courtyard of fine-pitch footprints the clearance is relaxed
            # (courtyard escape rule). A separate, less strict mask is used there.
            if fine.any() and clearance > FINE_PITCH_CLEARANCE:
                rad_f = width / 2.0 + FINE_PITCH_CLEARANCE
                m_f = (inb & ~raster.dilate(cu, rad_f, g.step)
                       & ~raster.dilate(ko, width / 2.0, g.step)
                       & ~holes)
                m = (m & ~fine) | (m_f & fine)
            # copper of strict-rule nets - grown by its own clearance if that is larger.
            # Grouped by clearance value: one raster per group, not per net.
            groups = {}
            for snet, need in strict.items():
                if snet == net or need <= clearance:
                    continue
                groups.setdefault(round(need, 3), set()).add(snet)
            for need, nets in groups.items():
                cus, n_s = raster.copper_mask(board, g, L, nets_only=nets)
                if sum(n_s.values()) if isinstance(n_s, dict) else n_s:
                    m &= ~raster.dilate(cus, need + width / 2.0, g.step)
            self.free[L] = m
        # A via needs its OWN mask, and a stricter one: it is wider than a track (0.6 vs
        # 0.25) and pierces every layer, so the spot must be free on each, plus a margin to
        # other holes. Checking the spot "as for a track" gave 12 violations on 11 nets -
        # five clearance, five hole_clearance and two shorting_items.
        vrad = VIA_SIZE / 2.0 + clearance
        # The edge inset of a via is its own: it is wider than a track, and inb was computed
        # for the track width. A shared inb produced a copper_edge_clearance violation at a
        # via on a USB data line.
        inb_via = raster.inside_board(board, g, inset=EDGE_INSET + VIA_SIZE / 2.0)
        if isinstance(inb_via, tuple):
            inb_via = inb_via[0]
        self.free_via = inb_via.copy()
        for L in self.layers:
            cu, _ = raster.copper_mask(board, g, L, nets_skip=own)
            ko, _ = raster.keepout_mask(board, g, L)
            self.free_via &= ~raster.dilate(cu, vrad, g.step)
            self.free_via &= ~raster.dilate(ko, VIA_SIZE / 2.0, g.step)
        # to other holes - no closer than the board hole-to-hole (0.2 mm) plus the drill radius
        holes2, _ = raster.hole_mask(board, g, extra=VIA_DRILL / 2.0 + HOLE_TO_HOLE)
        self.free_via &= ~holes2

        # own copper is a target, not an obstacle: mark it separately
        self.own = {}
        for L in self.layers:
            m = g.empty()
            lid = board.GetLayerID(L)
            for t in board.GetTracks():
                if t.GetNetname() != net:
                    continue
                if t.GetClass() == 'PCB_VIA':
                    v = t.Cast()
                    if v.IsOnLayer(lid):
                        raster.fill_disc(g, m, v.GetPosition().x / MM,
                                         v.GetPosition().y / MM, 0.2)
                elif t.GetLayer() == lid:
                    raster.fill_segment(g, m, t.GetStart().x / MM, t.GetStart().y / MM,
                                        t.GetEnd().x / MM, t.GetEnd().y / MM, 0.2)
            for fp in board.GetFootprints():
                for p in fp.Pads():
                    if p.GetNetname() == net and p.IsOnLayer(lid):
                        raster.fill_disc(g, m, p.GetPosition().x / MM,
                                         p.GetPosition().y / MM, 0.15)
            self.own[L] = m


def astar(field, starts, goals, window):
    """A* over the signal layers. starts/goals are sets of (layer, row, col)."""
    g = field.g
    (r0, r1, c0, c1) = window
    L = field.layers
    idx = {n: i for i, n in enumerate(L)}
    dist = {}
    prev = {}
    pq = []
    gr = [(rr, cc) for (_, rr, cc) in goals]

    def h(node):
        _, rr, cc = node
        return min(max(abs(rr - a), abs(cc - b)) for a, b in gr) if gr else 0

    for s in starts:
        dist[s] = 0.0
        heapq.heappush(pq, (h(s), 0.0, s))
    goalset = set(goals)
    seen = 0
    while pq:
        _, d, node = heapq.heappop(pq)
        if d > dist.get(node, 1e18):
            continue
        if node in goalset:
            path = [node]
            while path[-1] in prev:
                path.append(prev[path[-1]])
            return list(reversed(path)), seen
        seen += 1
        ln, rr, cc = node
        # within the layer plane: eight directions
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = rr + dr, cc + dc
                if not (r0 <= nr <= r1 and c0 <= nc <= c1):
                    continue
                if not field.free[ln][nr, nc]:
                    continue
                step = (DIAG if dr and dc else 1.0) * LAYER_COST[ln]
                nd = d + step
                key = (ln, nr, nc)
                if nd < dist.get(key, 1e18):
                    dist[key] = nd
                    prev[key] = node
                    heapq.heappush(pq, (nd + h(key), nd, key))
        # layer change - a via; the spot is checked against the via mask
        if field.free_via[rr, cc]:
          for other in L:
            if other == ln:
                continue
            if not field.free[other][rr, cc]:
                continue
            nd = d + VIA_COST
            key = (other, rr, cc)
            if nd < dist.get(key, 1e18):
                dist[key] = nd
                prev[key] = node
                heapq.heappush(pq, (nd + h(key), nd, key))
    return None, seen


def simplify(path):
    """Compress a path into segments: consecutive steps in one direction are merged."""
    out = []
    i = 0
    while i < len(path) - 1:
        ln, r, c = path[i]
        j = i + 1
        # layer change
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


def endpoint_cells(field, x, y, reach=0.35):
    """Cells that count as start or goal: at the pin coordinate, on any layer of own copper.

    The radius grows until a FREE cell of own copper is found. This is not a convenience but
    a condition for working at all: A* only expands into free cells, so a goal in an
    occupied cell is unreachable in principle - the queue runs dry and the router prints
    "no path" after exploring a million cells while the goal is geometrically reachable. And
    a cell easily ends up occupied: DRC reports the object's POSITION (for a track, its
    `start`), and if the track start lies inside a pad of a fine-pitch package, growing the
    neighbouring pads on a 0.1 mm grid rounds up to 0.3 mm and covers it. This happened on
    an MCU pin: the escape neck started inside pad 41, between pads 40 and 42.

    Continuing along OWN copper is legitimate: net connectivity does not suffer - the router
    still arrives at copper of the same net, just at another point of it.
    """
    g = field.g
    rr, cc = g.row(y), g.col(x)
    # The whole CONNECTED piece of own copper containing the point: any of its cells may be
    # joined, because the piece is already connected internally - it is copper of one net.
    # Taking only cells within 0.35 mm, the goal may sit in a pocket cut off from the rest of
    # the piece by the RASTER (grown neighbouring pads), and A* would not reach it, although
    # the break would close by joining the same piece 2 mm further away.
    seed = []
    k0 = max(1, int(reach / g.step))
    for L in field.layers:
        for r in range(rr - k0, rr + k0 + 1):
            for c in range(cc - k0, cc + k0 + 1):
                if 0 <= r < g.h and 0 <= c < g.w and field.own[L][r, c]:
                    seed.append((L, r, c))
    if seed:
        island = set(seed)
        dq = collections.deque(seed)
        while dq:
            L, r, c = dq.popleft()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < g.h and 0 <= nc < g.w):
                        continue
                    if field.own[L][nr, nc] and (L, nr, nc) not in island:
                        island.add((L, nr, nc))
                        dq.append((L, nr, nc))
            for other in field.layers:
                if other != L and field.own[other][r, c] and (other, r, c) not in island:
                    island.add((other, r, c))
                    dq.append((other, r, c))
        out = [(L, r, c) for (L, r, c) in island if field.free[L][r, c]]
        if out:
            return out
    out = []
    # No own copper in a free cell - almost always because the pad itself occupies the cell.
    # Then take the layers where its copper EXISTS, without requiring "free": starting from
    # one's own pad is legitimate as long as a neighbouring cell is free.
    for L in field.layers:
        if 0 <= rr < g.h and 0 <= cc < g.w and field.own[L][rr, cc]:
            out.append((L, rr, cc))
    if out:
        return out
    # Last resort - OUTER layers only. All layers used to be offered here, and A* happily
    # started a path on an inner layer right from an F.Cu pad: the track went onto the inner
    # layer with no via under it, the net stayed broken, and DRC showed "two items of one
    # net at one point on different layers". This hit half a dozen nets (QSPI, SPI, status
    # lines, a power rail) - SMD pads are all on outer layers.
    for L in ('F.Cu', 'B.Cu'):
        if L in field.layers and 0 <= rr < g.h and 0 <= cc < g.w and field.free[L][rr, cc]:
            out.append((L, rr, cc))
    return out


def emit(g, net, width, steps):
    """Path -> .kicad_pcb copper blocks."""
    out = []
    for s in steps:
        if s[0] == 'raw':
            # a segment in real coordinates, off the grid: use the top outer layer
            out.append('\t(segment\n\t\t(start %.4f %.4f)\n\t\t(end %.4f %.4f)\n'
                       '\t\t(width %.4f)\n\t\t(layer "F.Cu")\n\t\t(net "%s")\n'
                       '\t\t(uuid "%s")\n\t)'
                       % (s[1], s[2], s[3], s[4], width, net, _uuid.uuid4()))
        elif s[0] == 'via':
            _, r, c = s
            out.append('\t(via\n\t\t(at %.4f %.4f)\n\t\t(size %s)\n\t\t(drill %s)\n'
                       '\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "%s")\n\t\t(uuid "%s")\n\t)'
                       % (g.x(c), g.y(r), VIA_SIZE, VIA_DRILL, net, _uuid.uuid4()))
        else:
            _, ln, r1, c1, r2, c2 = s
            out.append('\t(segment\n\t\t(start %.4f %.4f)\n\t\t(end %.4f %.4f)\n'
                       '\t\t(width %.4f)\n\t\t(layer "%s")\n\t\t(net "%s")\n\t\t(uuid "%s")\n\t)'
                       % (g.x(c1), g.y(r1), g.x(c2), g.y(r2), width,
                          CANON.get(ln, ln), net, _uuid.uuid4()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=None, help='existing DRC report; computed if omitted')
    ap.add_argument('--limit', type=int, default=10, help='how many breaks to try')
    ap.add_argument('--max-len', type=float, default=None, help='only breaks shorter than this, mm')
    ap.add_argument('--longest-first', action='store_true',
                    help='take the LONGEST breaks first. Otherwise the batch is filled in '
                         'ascending length and never reaches 70..110 mm connections: they are '
                         'crowded out by medium ones that do not route. Experience: with a '
                         '25 mm window a targeted run on ten long ones closed six.')
    ap.add_argument('--min-len', type=float, default=None,
                    help='only breaks longer than this, mm. Keeps two campaign waves from '
                         'replaying the same work: without a lower bound the second wave '
                         'again takes the shortest breaks the first one already failed on.')
    ap.add_argument('--list', action='store_true', help='only show what is pending')
    ap.add_argument('--exclude-net', default=None,
                    help='file with a list of nets (one per line) not to try')
    ap.add_argument('--only-net', default=None,
                    help='file with a list of nets - try only these')
    ap.add_argument('--exclude-ref', default=None,
                    help='skip breaks that end on these footprints (comma-separated).\n'
                         'Useful while a part is not final: its copper will be redone anyway.')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    # Rules come from the board being routed, which may differ from the project default.
    if os.path.abspath(a.board) != os.path.abspath(paths.BOARD):
        DRU_CLEARANCE.clear()
        DRU_CLEARANCE.update(project.class_track_clearance(a.board))

    lck = os.path.join(os.path.dirname(os.path.abspath(a.board)),
                       '~' + os.path.basename(a.board) + '.lck')
    if a.apply and os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')

    drc = D.load(a.drc) if a.drc else D.run_drc(
        a.board, paths.work('maze_drc.json'))
    # Pour fragments are not the router's business - it lays tracks, not zones.
    # stitch_gnd.py closes them; without this filter they silently take batch slots.
    breaks = [b for b in D.signal_breaks(drc) if b[4] and 'Zone' not in b[1]]
    breaks.sort(key=lambda b: b[2], reverse=bool(getattr(a, 'longest_first', False)))
    if a.max_len:
        breaks = [b for b in breaks if b[2] <= a.max_len]
    if a.min_len:
        breaks = [b for b in breaks if b[2] >= a.min_len]
    if a.exclude_net and os.path.exists(a.exclude_net):
        bad = {l.strip() for l in open(a.exclude_net) if l.strip()}
        before = len(breaks)
        breaks = [b for b in breaks if b[0] not in bad and b[0].split('/')[-1] not in bad]
        print('hard nets deferred: %d breaks' % (before - len(breaks)))
    if a.only_net and os.path.exists(a.only_net):
        want = {l.strip() for l in open(a.only_net) if l.strip()}
        breaks = [b for b in breaks if b[0] in want or b[0].split('/')[-1] in want]
        print('kept by list: %d breaks' % len(breaks))
    if a.exclude_ref:
        skip = {r.strip() for r in a.exclude_ref.split(',') if r.strip()}
        before = len(breaks)
        breaks = [b for b in breaks
                  if not (skip & {str(x).split('-')[0].split(' ')[0] for x in (b[3] or [])})]
        print('excluded by footprints %s: %d breaks' % (','.join(sorted(skip)),
                                                        before - len(breaks)))
    print('signal breaks: %d, taking up to %d' % (len(breaks), a.limit))
    if a.list:
        for net, kinds, dist, refs, pos in breaks[:a.limit]:
            print('   %6.2f mm  %-28s %s' % (dist, net.split('/')[-1], '+'.join(kinds)))
        return 0

    board = pcbnew.LoadBoard(a.board)
    # Nets whose copper carries a clearance larger than that of the routed net (board rules
    # are set on the class of the copper ITSELF, not on the pair). Computed once per run.
    Field._strict = strict_nets(a.board, board)
    if Field._strict:
        import collections as _c
        print('nets with strict clearance: %d %s' % (
            len(Field._strict), dict(_c.Counter(Field._strict.values()))))
    g = raster.Grid(board, step=STEP)
    added_geom = []          # copper laid in this run: (net, layer, x1,y1,x2,y2, w) / via
    new_blocks = []
    done = failed = 0
    t0 = time.time()
    for net, kinds, dist, refs, pos in breaks[:a.limit]:
        w, clr = net_class_width(a.board, net)
        field = Field(board, g, net, w, clr)
        # Copper already laid in this run is an obstacle for the other nets. It must be
        # subtracted from BOTH masks. While only the track mask was updated, vias of two nets
        # in one run ended up 0.33 mm apart (0.25+0.25+0.15 required) and gave shorting_items
        # with `--limit 8`.
        for (onet, kind, data) in added_geom:
            if onet == net:
                continue
            for L in field.layers:
                m = g.empty()
                if kind == 'via':
                    raster.fill_disc(g, m, data[0], data[1], VIA_SIZE / 2 + clr + w / 2)
                elif kind[0] == L:
                    raster.fill_segment(g, m, data[0], data[1], data[2], data[3],
                                        data[4] + 2 * clr + w)
                field.free[L] &= ~m
            mv = g.empty()
            if kind == 'via':
                # via<->via: copper by clearance and drill by hole-to-hole, take the stricter
                raster.fill_disc(g, mv, data[0], data[1],
                                 max(VIA_SIZE + clr, VIA_DRILL + HOLE_TO_HOLE + VIA_SIZE / 2))
            else:
                raster.fill_segment(g, mv, data[0], data[1], data[2], data[3],
                                    data[4] + 2 * clr + VIA_SIZE)
            field.free_via &= ~mv
        (ax, ay), (bx, by) = pos
        starts = endpoint_cells(field, ax, ay)
        goals = endpoint_cells(field, bx, by)
        if not starts or not goals:
            print('  %-26s endpoints not in raster - skipped' % net.split('/')[-1])
            failed += 1
            continue
        rs = [r for _, r, _ in starts + goals]
        cs = [c for _, _, c in starts + goals]
        k = int(MARGIN / g.step)
        window = (max(0, min(rs) - k), min(g.h - 1, max(rs) + k),
                  max(0, min(cs) - k), min(g.w - 1, max(cs) + k))
        t = time.time()
        path, seen = astar(field, starts, goals, window)
        if path is None:
            print('  %-26s %6.2f mm  NO PATH (%d cells explored, %.1f s)'
                  % (net.split('/')[-1], dist, seen, time.time() - t))
            failed += 1
            continue
        steps = simplify(path)
        # The endpoints may fall into one 0.1 mm grid cell - then the path is empty and a
        # "success" would be false: the break remains. Lay a straight segment between the real
        # coordinates.
        if not any(x[0] == 'seg' for x in steps):
            if not any(x[0] == 'via' for x in steps):
                if math.hypot(bx - ax, by - ay) < 1e-4:
                    print('  %-26s endpoints coincide - the break cannot be closed with copper'
                          % net.split('/')[-1])
                    failed += 1
                    continue
                steps = [('raw', ax, ay, bx, by)]
            elif math.hypot(bx - ax, by - ay) >= 1e-4:
                # The path is a single layer change, and the step list used to be REPLACED by
                # a straight segment here, i.e. the via was dropped. What remained was a
                # 0.035 mm micro-segment on F.Cu without a via - the net is broken but looks
                # "almost connected". Now the via stays and the segment is added to it.
                steps = list(steps) + [('raw', ax, ay, bx, by)]
        nseg = sum(1 for s in steps if s[0] in ('seg', 'raw'))
        nvia = sum(1 for s in steps if s[0] == 'via')
        # Guard against a degenerate result. The router builds starts and goals from ALL copper
        # of the net and does not know which pieces are already connected. If the break is two
        # pieces of one net at one point on different layers, the start and goal sets overlap,
        # A* returns a single-node path, and a zero-length segment came out: the break is still
        # there while the report says "routed". Such a result now counts as a failure.
        total = sum(math.hypot(s[3] - s[1], s[4] - s[2]) for s in steps if s[0] == 'raw')
        total += sum(math.hypot(g.x(s[5]) - g.x(s[3]), g.y(s[4]) - g.y(s[2]))
                     for s in steps if s[0] == 'seg')
        if nvia == 0 and total < 0.05:
            print('  %-26s %6.2f mm  DEGENERATE PATH (%.3f mm without a via) - skipped'
                  % (net.split('/')[-1], dist, total))
            failed += 1
            continue
        length = sum(math.hypot(g.x(s[5]) - g.x(s[3]), g.y(s[4]) - g.y(s[2]))
                     for s in steps if s[0] == 'seg')
        length += sum(math.hypot(s[3] - s[1], s[4] - s[2])
                      for s in steps if s[0] == 'raw')
        print('  %-26s %6.2f mm -> %5.1f mm of copper, segments %d, vias %d (%.1f s)'
              % (net.split('/')[-1], dist, length, nseg, nvia, time.time() - t))
        new_blocks += emit(g, net, w, steps)
        for s in steps:
            if s[0] == 'via':
                added_geom.append((net, 'via', (g.x(s[2]), g.y(s[1]))))
            elif s[0] == 'raw':
                added_geom.append((net, ('F.Cu',), (s[1], s[2], s[3], s[4], w)))
            else:
                added_geom.append((net, (s[1],), (g.x(s[3]), g.y(s[2]),
                                                  g.x(s[5]), g.y(s[4]), w)))
        done += 1
    print('\nrouted %d, failed %d, total %.0f s' % (done, failed, time.time() - t0))
    if not a.apply or not new_blocks:
        if new_blocks:
            print('this is a plan; add --apply to write it')
        return 0
    txt = open(a.board).read()
    shutil.copy2(a.board, a.board + '.bakMaze')
    head = txt.rstrip()
    assert head.endswith(')')
    head = head[:-1].rstrip('\n')
    open(a.board, 'w').write(head + '\n' + '\n'.join(new_blocks) + '\n)\n')
    print('copper blocks written: %d, backup .bakMaze' % len(new_blocks))
    return 0


if __name__ == '__main__':
    sys.exit(main())
