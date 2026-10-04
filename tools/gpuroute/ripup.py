#!/usr/bin/env python3
"""Regional rip-up: remove foreign copper around a locked pin and re-route it together with the pin.

A locked pin (locked.py) is surrounded not by pads but by a ring of other nets' escapes (fanout,
escape tracks, vias). The incremental router does not move them, so the pin stays locked forever.
Here tracks and vias of foreign nets within radius R of the pin are removed, those nets join the
negotiation (their pieces must be reconnected), and PathFinder looks for a consistent solution
for all of them at once. DRC judges: fewer breaks with zero violations (gpu_campaign.sh /
rip_campaign.py).

Never removed: copper of plane nets (project.plane_nets(); the fill holds their pieces) unless
--rip-planes is given, nets listed in the `protect_nets` config key, nets of the net classes in
the `no_touch_classes` config key (e.g. a controlled-impedance RF class), and pads (not routing copper).

    python3 tools/gpuroute/ripup.py --board B --drc D --at 132.93,76.36 [--radius 2.5] [--window 4] [--apply]
"""
import argparse
import collections
import fnmatch
import math
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import numpy as np
import pcbnew
import torch

import drcinfo as D
import paths
import project
import raster
import sexp
import maze_route as MR
import connectivity as CN
from model import Model, emit_fine
from negotiate import Job, negotiate
from wave import simplify

MM = 1e6
# Net classes and nets whose copper is never ripped (e.g. an impedance-controlled RF feed).
NO_RIP_CLASSES = set(project.get('no_touch_classes', []))
PROTECT_NETS = set(project.get('protect_nets', []))


def class_of(board_path, net):
    by_name, pats = MR.load_classes(board_path)
    base = net.split('/')[-1]
    for cls, pat in pats:
        if fnmatch.fnmatch(base, pat):
            return cls
    return 'Default'


def seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def rip_candidates(board, board_path, cx, cy, radius, protect, planes='none', only=None, layers=None):
    """Tracks/vias of foreign nets within the radius; -> [(item, uuid, net)].

    planes != 'none' also removes copper of plane nets. Measured under a large MCU: without the
    planes, re-routing 26 nets did not open the target pin; with the planes the count went
    124 -> 129 at zero violations: the cut-off supply pads did not reconnect. Off by default
    (--rip-planes enables it)."""
    out = []
    for t in board.GetTracks():
        net = t.GetNetname()
        if not net or net in protect or net in PROTECT_NETS or net.split('/')[-1] in PROTECT_NETS:
            continue
        if only is not None and net.split('/')[-1] not in only:
            continue      # --nets: rip only the named nets (walls of specific neighbours)
        if layers is not None and t.GetClass() != 'PCB_VIA' and board.GetLayerName(t.GetLayer()) not in layers:
            continue      # --layers: only copper on these layers (e.g. inner layers under an escape)
        is_plane = net.split('/')[-1] in D.PLANE_NETS
        if is_plane and planes == 'none':
            continue
        if is_plane and planes == 'tracks' and t.GetClass() == 'PCB_VIA':
            continue          # plane vias stay: they are the link to the plane
        if class_of(board_path, net) in NO_RIP_CLASSES:
            continue
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition()
            d = math.hypot(p.x / MM - cx, p.y / MM - cy)
        else:
            s, e = t.GetStart(), t.GetEnd()
            d = seg_dist(cx, cy, s.x / MM, s.y / MM, e.x / MM, e.y / MM)
        if d <= radius:
            out.append((t, t.m_Uuid.AsString(), net))
    return out


def comp_pairs(labels, g, max_main=3000):
    """Pairs (main component, k, length, positions) to connect all pieces of a net."""
    L, H, W = labels.shape
    sizes = collections.Counter(labels[labels > 0].tolist())
    if len(sizes) < 2:
        return []
    order = [k for k, _ in sizes.most_common()]
    main = order[0]
    mc = np.argwhere(labels == main)               # (n, 3): l, r, c
    if len(mc) > max_main:
        mc = mc[np.random.RandomState(0).choice(len(mc), max_main, replace=False)]
    pairs = []
    for k in order[1:]:
        kc = np.argwhere(labels == k)
        d = ((kc[:, None, 1] - mc[None, :, 1]) ** 2 + (kc[:, None, 2] - mc[None, :, 2]) ** 2)
        i, j = np.unravel_index(int(d.argmin()), d.shape)
        pa = (g.x(int(kc[i, 2])), g.y(int(kc[i, 1])))
        pb = (g.x(int(mc[j, 2])), g.y(int(mc[j, 1])))
        pairs.append((main, int(k), math.hypot(pa[0] - pb[0], pa[1] - pb[1]), (pa, pb)))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', required=True)
    ap.add_argument('--at', required=True, help='x,y of the locked pin, mm')
    ap.add_argument('--radius', type=float, default=2.5, help='radius for removing foreign copper, mm')
    ap.add_argument('--window', type=float, default=4.0,
                    help='breaks with an end within this radius are re-route targets, mm')
    ap.add_argument('--iters', type=int, default=8)
    ap.add_argument('--rip-planes', choices=['none', 'tracks', 'all'], default='none',
                    help='plane-net copper: none - leave it; tracks - remove tracks (supply/ground feeds '
                         'along QFN pin columns), fix_plane_pads later connects their pads with a via '
                         'into the plane; all - vias too (harmful)')
    ap.add_argument('--probe', type=float, default=0.0,
                    help='after ripping, print the F.Cu raster around the point with radius N mm and exit')
    ap.add_argument('--probe-net', default=None, help='whose mask to print (short net name)')
    ap.add_argument('--nets', default=None, help='rip only these nets (short names, comma-separated)')
    ap.add_argument('--layers', default=None, help='rip only copper on these layers (comma-separated); vias always')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    cx, cy = (float(v) for v in a.at.split(','))

    lck = os.path.join(os.path.dirname(os.path.abspath(a.board)),
                       '~' + os.path.basename(a.board) + '.lck')
    if a.apply and os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')

    t_all = time.time()
    drc = D.load(a.drc)
    breaks = [b for b in D.signal_breaks(drc)
              if b[4] and 'Zone' not in b[1] and b[0].split('/')[-1] not in D.PLANE_NETS]
    targets = collections.OrderedDict()
    for b in breaks:
        if any(math.hypot(x - cx, y - cy) <= a.window for (x, y) in b[4]):
            targets.setdefault(b[0], []).append(b)
    # all breaks of the target nets, not only those in the window
    for b in breaks:
        if b[0] in targets and b not in targets[b[0]]:
            targets[b[0]].append(b)
    print('target nets %d (breaks %d) in a %.1f mm window around (%.2f, %.2f)'
          % (len(targets), sum(len(v) for v in targets.values()), a.window, cx, cy))
    if not targets:
        return 0

    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=MR.STEP)
    rips = rip_candidates(board, a.board, cx, cy, a.radius, protect=set(targets),
                          planes=a.rip_planes,
                          only=set(a.nets.split(',')) if a.nets else None,
                          layers=set(a.layers.split(',')) if a.layers else None)
    # Ripped plane-net tracks: their pads are connected by fix_plane_pads.py with a via into the plane
    # (class via size) rather than by the router with a track.
    plane_ripped = {net for _, _, net in rips if net.split('/')[-1] in D.PLANE_NETS}
    rip_nets = collections.OrderedDict()
    for _, u, net in rips:
        rip_nets.setdefault(net, 0)
        rip_nets[net] += 1
    if plane_ripped:
        # Plane nets do join the negotiation, but last and not mandatory: their "own copper"
        # includes the plane fill, so a path is a short track + a via into the plane where there
        # is room (fix_plane_pads looks for room only at the pad itself and after dense routing
        # does not find it: 118 -> 126 with two pins opened).
        print('plane-net copper ripped: %d items (%s) - connected last, the rest goes to fix_plane_pads'
              % (sum(1 for r in rips if r[2] in plane_ripped),
                 ', '.join(sorted(n.split('/')[-1] for n in plane_ripped))))
    print('ripping %d items of %d nets: %s' % (
        len(rips), len(rip_nets),
        ', '.join('%sx%d' % (n.split('/')[-1][:20], k) for n, k in rip_nets.items())))
    for item, _, _ in rips:
        board.Remove(item)

    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    # A ripped net that did not reconnect gets its copper back (a fixed obstacle again), and the
    # negotiation is repeated without it - "rip less until it converges". Before, one such net
    # cancelled the whole re-route (9 rounds out of 10).
    for attempt in range(1, 4):
        nets = list(targets) + [n for n in rip_nets if n not in targets]
        t = time.time()
        model = Model(board, a.board, g, nets, dev, log=lambda *x: None)
        print('attempt %d: model of %d nets, %.0f s' % (attempt, len(nets), time.time() - t))

        jobs = []
        for net in nets:
            lab = model.labels[net]
            pairs = []
            if net in targets:
                for (n, kinds, dist, refs, pos) in targets[net]:
                    (ax, ay), (bx, by) = pos
                    la, lb = CN.label_at(lab, g, ax, ay), CN.label_at(lab, g, bx, by)
                    if la and lb and la != lb:
                        pairs.append((la, lb, dist, pos))
            if net in rip_nets:
                have = {(p[0], p[1]) for p in pairs} | {(p[1], p[0]) for p in pairs}
                for pr in comp_pairs(lab, g):
                    if (pr[0], pr[1]) not in have:
                        pairs.append(pr)
            if pairs:
                jb = Job(net, pairs)
                jb.must = net in rip_nets and net not in plane_ripped
                if net in plane_ripped:
                    jb.length += 1000.0     # plane nets go to the end of the queue
                jobs.append(jb)
        print('jobs %d, breaks to route %d' % (len(jobs), sum(len(j.breaks) for j in jobs)))

        ok, conflicted = negotiate(model, jobs, iters=a.iters, log=print)
        bad = {j.net for j in conflicted}
        failed_rip = sorted({j.net for j in jobs
                             if j.net in rip_nets and j.net not in plane_ripped and (j.fail or j.net in bad)})
        if not failed_rip:
            break
        print('ripped nets left unconnected: %s - restoring their copper and retrying'
              % ', '.join(n.split('/')[-1] for n in failed_rip))
        for item, u, net in rips:
            if net in failed_rip:
                board.Add(item)
        rips = [r for r in rips if r[2] not in failed_rip]
        for net in failed_rip:
            rip_nets.pop(net, None)
        if attempt == 3:
            print('no convergence in three attempts - re-route cancelled')
            return 2

    blocks, done, fail = [], 0, 0
    for j in jobs:
        if j.net in bad:
            continue
        w, clr = model.cls[j.net]
        fail += j.fail
        for p in j.paths:
            if len(p) >= 2:
                blocks += emit_fine(g, j.net, w, p, model.fine)
                done += 1
    print('\nrouted %d, not routed %d, ripped %d, blocks to write %d; %.0f s'
          % (done, fail, len(rips), len(blocks), time.time() - t_all))
    if not blocks or done == 0:
        print('nothing to write - re-route cancelled')
        return 2
    if not a.apply:
        return 0
    txt = open(a.board).read()
    shutil.copy2(a.board, a.board + '.bakRip')
    kill = {u for _, u, _ in rips}
    spans = []
    for tag in ('segment', 'arc', 'via'):
        for i, j in sexp.blocks(txt, tag):
            m = re.search(r'\(uuid "([^"]+)"\)', txt[i:j])
            if m and m.group(1) in kill:
                spans.append((i, j))
    spans.sort(reverse=True)
    for i, j in spans:
        # sexp.blocks returns a start that includes the newline before '\t(' - the block goes entirely
        txt = txt[:i] + txt[j:]
    head = txt.rstrip()
    assert head.endswith(')')
    head = head[:-1].rstrip('\n')
    open(a.board, 'w').write(head + '\n' + '\n'.join(blocks) + '\n)\n')
    print('blocks removed %d, written %d, backup .bakRip' % (len(spans), len(blocks)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
