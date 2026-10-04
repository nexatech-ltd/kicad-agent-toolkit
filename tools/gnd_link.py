#!/usr/bin/env python3
"""Sew detached pads of a plane net to the plane: for every pad KiCad considers unconnected, place a via
into the plane next to it and join them with a short segment. DRC is the judge.
  python3 tools/gnd_link.py --board B [--net GND] [--r 2.5] [--via 0.5,0.2] [--apply]
The via spot is found geometrically (clearance to foreign copper on all layers) and only where the pad's
own net has a fill under it on at least one non-top copper layer; otherwise the via connects to nothing."""
import sys, os, math, json, argparse, subprocess, collections, shutil
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, pcbnew, drcinfo as D
MM = 1e6
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--net', default='GND')
ap.add_argument('--r', type=float, default=2.5); ap.add_argument('--via', default='0.5,0.2')
ap.add_argument('--clr', type=float, default=0.15); ap.add_argument('--step', type=float, default=0.05)
ap.add_argument('--width', type=float, default=0.25); ap.add_argument('--apply', action='store_true')
a = ap.parse_args(); VIA, DRILL = (float(v) for v in a.via.split(','))

def clusters(board):
    filler = pcbnew.ZONE_FILLER(board); filler.Fill(board.Zones())
    board.BuildConnectivity(); conn = board.GetConnectivity()
    items = [t for t in board.GetTracks() if t.GetNetname().split('/')[-1] == a.net]
    items += [pd for fp in board.GetFootprints() for pd in fp.Pads() if pd.GetNetname().split('/')[-1] == a.net]
    U = lambda x: x.m_Uuid.AsString(); byu = {U(i): i for i in items}
    seen = set(); out = []
    for it in items:
        if U(it) in seen: continue
        cl = [x for x in conn.GetConnectedItems(it) if x.GetClass() != 'ZONE']
        members = {U(x) for x in cl} | {U(it)}; seen |= members
        out.append([byu[u] for u in members if u in byu])
    out.sort(key=len, reverse=True)
    return out

def seg_seg(ax, ay, bx, by, cx_, cy_, dx_, dy_):
    """Distance between two segments. The minimum of the four endpoint-to-segment distances is NOT
    enough: for crossing segments all four are positive while the true distance is zero, and the
    corridor check lets a jumper run straight across a foreign track (tracks_crossing in DRC).
    So signed areas first: opposite signs for both pairs mean the segments intersect."""
    def cross(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)
    d1 = cross(cx_, cy_, dx_, dy_, ax, ay)
    d2 = cross(cx_, cy_, dx_, dy_, bx, by)
    d3 = cross(ax, ay, bx, by, cx_, cy_)
    d4 = cross(ax, ay, bx, by, dx_, dy_)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(dseg(ax, ay, cx_, cy_, dx_, dy_), dseg(bx, by, cx_, cy_, dx_, dy_), dseg(cx_, cy_, ax, ay, bx, by), dseg(dx_, dy_, ax, ay, bx, by))


def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1; L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

def spot(board, cx, cy, net):
    """spot for a through via: clearance to foreign copper >= 0 and a fill of its own net under it on an inner layer"""
    segs, discs = [], []
    for t in board.GetTracks():
        n = t.GetNetname().split('/')[-1]
        if n == net: continue
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition()
            if abs(p.x / MM - cx) < 8 and abs(p.y / MM - cy) < 8: discs.append((p.x / MM, p.y / MM, t.GetWidth() / MM / 2))
        else:
            s, e = t.GetStart(), t.GetEnd()
            if min(s.x, e.x) / MM > cx + 8 or max(s.x, e.x) / MM < cx - 8: continue
            if min(s.y, e.y) / MM > cy + 8 or max(s.y, e.y) / MM < cy - 8: continue
            segs.append((s.x / MM, s.y / MM, e.x / MM, e.y / MM, t.GetWidth() / MM / 2))
    for fp in board.GetFootprints():
        for pd in fp.Pads():
            p = pd.GetPosition()
            if abs(p.x / MM - cx) > 8 or abs(p.y / MM - cy) > 8: continue
            if pd.GetNetname().split('/')[-1] == net and pd.GetDrillSize().x == 0: continue
            sz = pd.GetSize(); discs.append((p.x / MM, p.y / MM, math.hypot(sz.x / MM, sz.y / MM) / 2))
    F = board.GetLayerID('F.Cu')
    segs_top = []
    for t in board.GetTracks():
        if t.GetClass() != 'PCB_TRACK' or t.GetLayer() != F: continue
        if t.GetNetname().split('/')[-1] == net: continue
        s_, e_ = t.GetStart(), t.GetEnd()
        if min(s_.x, e_.x) / MM > cx + 8 or max(s_.x, e_.x) / MM < cx - 8: continue
        if min(s_.y, e_.y) / MM > cy + 8 or max(s_.y, e_.y) / MM < cy - 8: continue
        segs_top.append((s_.x / MM, s_.y / MM, e_.x / MM, e_.y / MM, t.GetWidth() / MM / 2))
    discs_top = []
    for fp in board.GetFootprints():
        for pd in fp.Pads():
            p_ = pd.GetPosition()
            if abs(p_.x / MM - cx) > 8 or abs(p_.y / MM - cy) > 8: continue
            if pd.GetNetname().split('/')[-1] == net: continue
            if not pd.IsOnLayer(F): continue
            sz = pd.GetSize(); discs_top.append((p_.x / MM, p_.y / MM, math.hypot(sz.x / MM, sz.y / MM) / 2))
    zones = [z for z in board.Zones() if z.GetNetname().split('/')[-1] == net and not z.GetIsRuleArea()]
    inner = [L for L in board.GetEnabledLayers().CuStack() if L != pcbnew.F_Cu]   # inner planes and B.Cu
    best = None
    r = 0.6
    while r <= a.r and best is None:
        n = max(12, int(2 * math.pi * r / a.step))
        for i in range(n):
            ang = 2 * math.pi * i / n
            x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
            m = min([dseg(x, y, *s[:4]) - s[4] for s in segs] + [math.hypot(x - d[0], y - d[1]) - d[2] for d in discs] + [9.0]) - VIA / 2 - a.clr
            if m < 0: continue
            P = pcbnew.VECTOR2I(int(x * MM), int(y * MM))
            # the point must be over the MAIN fill of the net, not a cut-off island: an island that contains
            # a pad is not removed, but it is not connected to the plane, so a via into it connects nothing
            big = False
            for z in zones:
                for L in inner:
                    if not z.IsOnLayer(L) or not z.HitTestFilledArea(L, P): continue
                    polys = z.GetFilledPolysList(L)
                    for k in range(polys.OutlineCount()):
                        o = polys.Outline(k)
                        if o.PointInside(P) and abs(o.Area()) / MM / MM > 50.0:
                            big = True
                            break
                    if big: break
                if big: break
            if not big: continue
            # corridor from the pad to the spot: the F.Cu jumper must not touch foreign copper on that layer
            ok = True
            for (sx, sy, ex, ey, hw) in segs_top:
                if seg_seg(cx, cy, x, y, sx, sy, ex, ey) < hw + a.width / 2 + a.clr: ok = False; break
            if ok:
                for (dx_, dy_, r_) in discs_top:
                    if dseg(dx_, dy_, cx, cy, x, y) < r_ + a.width / 2 + a.clr: ok = False; break
            if not ok: continue
            best = (x, y, m); break
        r += 0.15
    return best

board = pcbnew.LoadBoard(a.board)
cl = clusters(board)
print('clusters of net %s: %d (main one has %d items)' % (a.net, len(cl), len(cl[0])))
targets = []
for c in cl[1:]:
    for it in c:
        if it.GetClass() == 'PAD':
            p = it.GetPosition(); targets.append((it.GetParentFootprint().GetReference() + '.' + it.GetNumber(), p.x / MM, p.y / MM))
print('detached pads: %d' % len(targets))
ops = []
for ref, x, y in targets:
    s = spot(board, x, y, a.net)
    if not s: print('  %-10s (%.2f,%.2f): no room within %.1f mm' % (ref, x, y, a.r)); continue
    vx, vy, m = s
    ops.append({'op': 'add_via', 'net': a.net, 'x': round(vx, 3), 'y': round(vy, 3), 'size': VIA, 'drill': DRILL})
    ops.append({'op': 'add_seg', 'net': a.net, 'layer': 'F.Cu', 'a': [round(x, 3), round(y, 3)], 'b': [round(vx, 3), round(vy, 3)], 'w': a.width})
    print('  %-10s (%.2f,%.2f) -> via (%.2f,%.2f), margin %+.3f, length %.2f mm' % (ref, x, y, vx, vy, m, math.hypot(vx - x, vy - y)))
print('operations: %d' % len(ops))
if not a.apply or not ops: sys.exit(0)
json.dump(ops, open(a.board + '.gndlink.json', 'w'))
shutil.copy(a.board, a.board + '.gndbak')
subprocess.run([sys.executable, os.path.join(HERE, 'util', 'edit_ops.py'), a.board, a.board + '.gndlink.json', '--apply'], capture_output=True)
subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones', '--severity-all', '--format', 'json', '-o', a.board + '.gnddrc.json', a.board], capture_output=True)
d = D.load(a.board + '.gnddrc.json'); u, e, w = D.score(d)
print('after sewing: unconnected %d, errors %d' % (u, e))
