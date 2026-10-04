#!/usr/bin/env python3
"""Stitch a fill fragment cut off from the plane with a via INSIDE the fragment itself.

Why separate from gnd_link.py: that tool runs an F.Cu jumper from the pad to a via nearby, which is
right when there is room around the pad. But a fragment cut off by a bundle of signals is usually the
size of the pad itself: any jumper crosses a foreign track, and a via outside the fragment connects the
plane to something else. Here the via goes into the fragment, so no jumper is needed at all.

Taken into account:
  * the obstacle for a THROUGH via is copper on all copper layers, not just the fragment's layer
    (a B.Cu track blocks a via in an F.Cu fragment);
  * the point must be over a plane of the same net (outline > 50 mm^2), otherwise the via goes nowhere;
  * clearance to foreign copper and via-to-via distance follow the board rules.

If there is no room, the ACHIEVABLE margin and the blocking item are printed. That is a decision aid,
not a refusal: it shows whether moving one track is enough or the fragment is hopeless.

    python3 tools/island_stitch.py --board B [--net GND] [--via 0.5,0.2]
                                   [--clr 0.15] [--step 0.025] [--apply]

ORDER MATTERS. First rip the blocking nets (--relax prints which ones; util/rip_at_nets.py rips them),
and only then place the vias. Not the other way round: a via placed over a foreign track gets THAT
track's net when the board is saved, and ripping that net also removes the via; the pad then comes
back on the next round as if the stitch did not hold.
"""
import argparse, collections, json, math, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, pcbnew, drcinfo as D

MM = 1e6


def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def loose_pads(board, net):
    """Pads of the net that are not in KiCad's main connectivity cluster."""
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    items = [t for t in board.GetTracks() if t.GetNetname().split('/')[-1] == net]
    items += [pd for fp in board.GetFootprints() for pd in fp.Pads() if pd.GetNetname().split('/')[-1] == net]
    U = lambda x: x.m_Uuid.AsString()
    byu = {U(i): i for i in items}
    seen, out = set(), []
    for it in items:
        if U(it) in seen:
            continue
        cl = [x for x in conn.GetConnectedItems(it) if x.GetClass() != 'ZONE']
        members = {U(x) for x in cl} | {U(it)}
        seen |= members
        out.append([byu[u] for u in members if u in byu])
    out.sort(key=len, reverse=True)
    res = []
    for c in out[1:]:
        for it in c:
            if it.GetClass() == 'PAD':
                res.append(it)
    return res


def all_copper(board, net):
    """Foreign copper of the whole board: segments (x1,y1,x2,y2,halfw,net), discs (x,y,r,name), holes (x,y,r).

    Computed once: SWIG wrapper calls inside the per-pad loop are the main time cost.
    Copper on every layer blocks a THROUGH via, so the layer is ignored."""
    segs, discs, rects, holes = [], [], [], []
    for t in board.GetTracks():
        n = t.GetNetname().split('/')[-1]
        p = t.GetPosition()
        if t.GetClass() == 'PCB_VIA':
            holes.append((p.x / MM, p.y / MM, t.GetDrill() / MM / 2))
            if n != net:
                discs.append((p.x / MM, p.y / MM, t.GetWidth() / MM / 2, n))
            continue
        if n == net:
            continue
        s, e = t.GetStart(), t.GetEnd()
        segs.append((s.x / MM, s.y / MM, e.x / MM, e.y / MM, t.GetWidth() / MM / 2, n))
    for fp in board.GetFootprints():
        for pd in fp.Pads():
            p = pd.GetPosition()
            if pd.GetDrillSize().x > 0:
                holes.append((p.x / MM, p.y / MM, pd.GetDrillSize().x / MM / 2))
            if pd.GetNetname().split('/')[-1] == net:
                continue
            sz = pd.GetSize()
            try:
                ang = pd.GetOrientation().AsDegrees()
            except AttributeError:
                ang = pd.GetOrientationDegrees()
            rects.append((p.x / MM, p.y / MM, sz.x / MM / 2, sz.y / MM / 2, math.radians(ang),
                          fp.GetReference() + '.' + pd.GetNumber()))
    return segs, discs, rects, holes


def drect(px, py, cx, cy, hw, hh, ang):
    """Distance from a point to a rotated rectangle (for round and oval pads, to the bounding
    rectangle, i.e. a conservative estimate)."""
    c, s_ = math.cos(ang), math.sin(ang)
    dx = abs((px - cx) * c + (py - cy) * s_) - hw
    dy = abs(-(px - cx) * s_ + (py - cy) * c) - hh
    return math.hypot(max(dx, 0.0), max(dy, 0.0))


def search(o, x0, y0, x1, y1, step, R, DRILL, clr, hole, segs, discs, rects, holes, ignore):
    """Best via spot inside the outline: (margin over the rule, x, y, blocking item)."""
    best = None
    gx = x0
    while gx <= x1:
        gy = y0
        while gy <= y1:
            if o.PointInside(pcbnew.VECTOR2I(int(gx * MM), int(gy * MM))):
                m, who = 9.0, ''
                for s in segs:
                    if s[5] in ignore:
                        continue
                    d = dseg(gx, gy, s[0], s[1], s[2], s[3]) - s[4] - R
                    if d < m:
                        m, who = d, s[5]
                for dk in discs:
                    if dk[3] in ignore:
                        continue
                    d = math.hypot(gx - dk[0], gy - dk[1]) - dk[2] - R
                    if d < m:
                        m, who = d, dk[3]
                for rc in rects:
                    if rc[5] in ignore:
                        continue
                    d = drect(gx, gy, rc[0], rc[1], rc[2], rc[3], rc[4]) - R
                    if d < m:
                        m, who = d, rc[5]
                m -= clr
                hm = 9.0
                for (hx, hy, hr) in holes:
                    hm = min(hm, math.hypot(gx - hx, gy - hy) - hr - DRILL / 2)
                if hm - hole < m:
                    m, who = hm - hole, 'hole'
                if best is None or m > best[0]:
                    best = (m, gx, gy, who)
            gy += step
        gx += step
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--net', default='GND')
    ap.add_argument('--via', default='0.5,0.2')
    ap.add_argument('--clr', type=float, default=0.15)
    ap.add_argument('--hole', type=float, default=0.25, help='via-to-via hole clearance')
    ap.add_argument('--step', type=float, default=0.025)
    ap.add_argument('--min-fill', type=float, default=50.0, help='mm^2, minimum plane outline under the point')
    ap.add_argument('--max-island', type=float, default=60.0, help='mm^2, anything larger is not a fragment')
    ap.add_argument('--relax', type=int, default=0,
                    help='how many blocking nets may be virtually removed: prints which ones '
                         'and the resulting margin; this is the rip-up list')
    ap.add_argument('--ignore-nets', default='',
                    help='nets treated as absent (already scheduled for rip-up)')
    ap.add_argument('--force', action='store_true',
                    help='place even where the clearance rule is not met: blocking nets are ripped and rerouted')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    VIA, DRILL = (float(v) for v in a.via.split(','))
    R = VIA / 2

    board = pcbnew.LoadBoard(a.board)
    t0 = time.time()
    pads = loose_pads(board, a.net)
    print('detached pads of net %s: %d (%.0f s)' % (a.net, len(pads), time.time() - t0))
    if not pads:
        return
    zones = [z for z in board.Zones()
             if z.GetNetname().split('/')[-1] == a.net and not z.GetIsRuleArea()]

    # Fill cache: GetFilledPolysList is a SWIG wrapper and every call COPIES the polygon set.
    # A large plane has thousands of vertices; a copy per pad can take hours.
    t0 = time.time()
    cache = {}          # layer -> list of (outline, bbox, area)
    for L in board.GetEnabledLayers().CuStack():
        out = []
        for z in zones:
            if not z.IsOnLayer(L):
                continue
            pl = z.GetFilledPolysList(L)
            for k in range(pl.OutlineCount()):
                o = pl.Outline(k)
                bb = o.BBox()
                out.append((o, (bb.GetLeft() / MM, bb.GetTop() / MM,
                                bb.GetRight() / MM, bb.GetBottom() / MM), abs(o.Area()) / MM / MM))
        cache[L] = out
    print('net fills parsed: %d layers, %d outlines (%.0f s)'
          % (len(cache), sum(len(v) for v in cache.values()), time.time() - t0))

    # Obstacles: once for the whole board, then filtered by bounding box.
    segs, discs, rects, holes = all_copper(board, a.net)
    print('foreign copper: %d segments, %d via discs, %d pads, %d holes'
          % (len(segs), len(discs), len(rects), len(holes)))

    ops, misses, tight, plan = [], [], [], []
    for pd in pads:
        ref = pd.GetParentFootprint().GetReference() + '.' + pd.GetNumber()
        px, py = pd.GetPosition().x / MM, pd.GetPosition().y / MM
        lay = pd.GetLayer()
        island = None
        for (o, bb, area) in cache.get(lay, ()):
            if area > a.max_island:
                continue
            if bb[0] - 0.4 <= px <= bb[2] + 0.4 and bb[1] - 0.4 <= py <= bb[3] + 0.4:
                if island is None or area < island[2]:
                    island = (o, bb, area)
        if island is None:
            misses.append((ref, px, py, 'no fill fragment under the pad'))
            continue
        o, bb, area = island
        x0, y0, x1, y1 = bb
        near_s = [s for s in segs
                  if not (min(s[0], s[2]) > x1 + 2 or max(s[0], s[2]) < x0 - 2
                          or min(s[1], s[3]) > y1 + 2 or max(s[1], s[3]) < y0 - 2)]
        near_d = [d for d in discs
                  if x0 - 2 - d[2] <= d[0] <= x1 + 2 + d[2] and y0 - 2 - d[2] <= d[1] <= y1 + 2 + d[2]]
        near_r = [r for r in rects
                  if x0 - 2 - r[2] - r[3] <= r[0] <= x1 + 2 + r[2] + r[3]
                  and y0 - 2 - r[2] - r[3] <= r[1] <= y1 + 2 + r[2] + r[3]]
        near_h = [h for h in holes
                  if x0 - 2 <= h[0] <= x1 + 2 and y0 - 2 <= h[1] <= y1 + 2]
        ignore = set(n for n in a.ignore_nets.split(',') if n)
        removed = []
        for _relax in range(a.relax + 1):
            best = search(o, x0, y0, x1, y1, a.step, R, DRILL, a.clr, a.hole,
                          near_s, near_d, near_r, near_h, ignore)
            if best is None or best[0] >= 0 or _relax == a.relax:
                break
            # a NET can be ripped, a pad cannot: "blocked by C1.1" is the other pad of the capacitor
            # itself, and relaxing it gives a margin that will never exist on the board
            if re.match(r'^[A-Z]+\d+\.\w+$', best[3] or '') or best[3] == 'hole':
                break
            removed.append(best[3])
            ignore.add(best[3])
        if removed:
            print('  %-10s rip %s -> margin %+.3f mm' % (ref, ', '.join(removed), best[0] if best else float('nan')))
        if best is None:
            misses.append((ref, px, py, 'no grid points inside the %.2f mm^2 fragment' % area))
            continue
        m, vx, vy, who = best
        Q = pcbnew.VECTOR2I(int(vx * MM), int(vy * MM))
        big = 0.0
        for L, lst in cache.items():
            if L == lay:
                continue
            for (oo, bb2, ar) in lst:
                if ar < a.min_fill or not (bb2[0] <= vx <= bb2[2] and bb2[1] <= vy <= bb2[3]):
                    continue
                if oo.PointInside(Q):
                    big = max(big, ar)
        if (m >= 0 or a.force) and big >= a.min_fill:
            ops.append({'op': 'add_via', 'net': a.net, 'x': round(vx, 3), 'y': round(vy, 3),
                        'size': VIA, 'drill': DRILL})
            plan.append({'ref': ref, 'x': round(vx, 3), 'y': round(vy, 3), 'rip': removed,
                         'margin': round(m, 3)})
            print('  %-10s fragment %5.2f mm^2 -> via (%.3f,%.3f), margin over the rule %+.3f mm%s'
                  % (ref, area, vx, vy, m, '' if m >= 0 else ('  TIGHT, blocked by ' + (who or '-'))))
            if m < 0:
                tight.append(who)
        else:
            misses.append((ref, px, py,
                           'fragment %.2f mm^2: best margin %+.3f mm over the %.2f rule, blocked by %s, plane under the point %.0f mm^2'
                           % (area, m, a.clr, who or '-', big)))
    for ref, px, py, why in misses:
        print('  %-10s (%.2f,%.2f): %s' % (ref, px, py, why))
    print('vias to place: %d (tighter than the rule: %d), not closed: %d'
          % (len(ops), len(tight), len(misses)))
    if tight:
        print('blocking nets: ' + ', '.join(sorted(set(tight))))
    pj = paths.work('island_plan.json')
    try:
        json.dump(plan, open(pj, 'w'), ensure_ascii=False, indent=1)
        print('plan ->', pj)
    except OSError:
        pass
    if not a.apply or not ops:
        return
    json.dump(ops, open(a.board + '.islands.json', 'w'))
    shutil.copy(a.board, a.board + '.bakIsland')
    subprocess.run([sys.executable, os.path.join(HERE, 'util', 'edit_ops.py'),
                    a.board, a.board + '.islands.json', '--apply'], check=False)
    subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones', '--severity-all',
                    '--format', 'json', '-o', a.board + '.islanddrc.json', a.board], capture_output=True)
    d = D.load(a.board + '.islanddrc.json')
    u, e, w = D.score(d)
    print('after stitching fragments: unconnected %d, errors %d' % (u, e))


main()
