#!/usr/bin/env python3
"""Unlock trapped ends of signal nets: next to each break end, place a via in free space (with a check of
the straight corridor to it) and join it with a short segment; the router then finishes the net (--any-target).
  python3 tools/sig_link.py --board B --drc D.json [--r 4] [--apply]
DRC is the judge: failed vias/jumpers are removed automatically."""
import sys, os, math, json, argparse, subprocess, shutil, collections
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, pcbnew, drcinfo as D
MM = 1e6
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--drc', required=True)
ap.add_argument('--r', type=float, default=4.0); ap.add_argument('--via', default='0.5,0.2')
ap.add_argument('--clr', type=float, default=0.18); ap.add_argument('--width', type=float, default=0.15)
ap.add_argument('--step', type=float, default=0.05); ap.add_argument('--apply', action='store_true')
a = ap.parse_args(); VIA, DRILL = (float(v) for v in a.via.split(','))
b = pcbnew.LoadBoard(a.board)
drc = D.load(a.drc)
ends = []
for br in D.signal_breaks(drc):
    net = br[0]
    for (x, y) in br[4]: ends.append((net, x, y))
print('break ends: %d' % len(ends))

def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1; L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

F = b.GetLayerID('F.Cu')
TR = list(b.GetTracks()); PADS = [(fp, pd) for fp in b.GetFootprints() for pd in fp.Pads()]
ops = []
for net, cx, cy in ends:
    short = net.split('/')[-1]
    segs = []; discs = []; segs_top = []; discs_top = []
    for t in TR:
        if t.GetNetname() == net: continue
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition()
            if abs(p.x / MM - cx) < 7 and abs(p.y / MM - cy) < 7:
                discs.append((p.x / MM, p.y / MM, t.GetWidth() / MM / 2)); discs_top.append((p.x / MM, p.y / MM, t.GetWidth() / MM / 2))
        else:
            s, e = t.GetStart(), t.GetEnd()
            if min(s.x, e.x) / MM > cx + 7 or max(s.x, e.x) / MM < cx - 7: continue
            if min(s.y, e.y) / MM > cy + 7 or max(s.y, e.y) / MM < cy - 7: continue
            item = (s.x / MM, s.y / MM, e.x / MM, e.y / MM, t.GetWidth() / MM / 2)
            segs.append(item)
            if t.GetLayer() == F: segs_top.append(item)
    for fp, pd in PADS:
        p = pd.GetPosition()
        if abs(p.x / MM - cx) > 7 or abs(p.y / MM - cy) > 7: continue
        if pd.GetNetname() == net: continue
        sz = pd.GetSize(); r = math.hypot(sz.x / MM, sz.y / MM) / 2
        discs.append((p.x / MM, p.y / MM, r))
        if pd.IsOnLayer(F): discs_top.append((p.x / MM, p.y / MM, r))
    best = None; r = 0.55
    while r <= a.r and best is None:
        n = max(16, int(2 * math.pi * r / a.step))
        for i in range(n):
            ang = 2 * math.pi * i / n
            x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
            m = min([dseg(x, y, *s[:4]) - s[4] for s in segs] + [math.hypot(x - d[0], y - d[1]) - d[2] for d in discs] + [9.0]) - VIA / 2 - a.clr
            if m < 0: continue
            ok = True
            for (sx, sy, ex, ey, hw) in segs_top:
                if min(dseg(cx, cy, sx, sy, ex, ey), dseg(x, y, sx, sy, ex, ey), dseg(sx, sy, cx, cy, x, y), dseg(ex, ey, cx, cy, x, y)) < hw + a.width / 2 + a.clr: ok = False; break
            if ok:
                for (dx_, dy_, r_) in discs_top:
                    if dseg(dx_, dy_, cx, cy, x, y) < r_ + a.width / 2 + a.clr: ok = False; break
            if not ok: continue
            best = (x, y, m); break
        r += 0.15
    if not best:
        print('  %-12s (%.2f,%.2f): no room within %.1f mm' % (short, cx, cy, a.r)); continue
    vx, vy, m = best
    ops.append({'op': 'add_via', 'net': short, 'x': round(vx, 3), 'y': round(vy, 3), 'size': VIA, 'drill': DRILL})
    ops.append({'op': 'add_seg', 'net': short, 'layer': 'F.Cu', 'a': [round(cx, 3), round(cy, 3)], 'b': [round(vx, 3), round(vy, 3)], 'w': a.width})
    print('  %-12s (%.2f,%.2f) -> via (%.2f,%.2f), margin %+.3f, length %.2f' % (short, cx, cy, vx, vy, m, math.hypot(vx - cx, vy - cy)))
print('operations: %d' % len(ops))
if not a.apply or not ops: sys.exit(0)
json.dump(ops, open(a.board + '.siglink.json', 'w'))
shutil.copy(a.board, a.board + '.sigbak')
subprocess.run([sys.executable, os.path.join(HERE, 'util', 'edit_ops.py'), a.board, a.board + '.siglink.json', '--apply'], capture_output=True)
subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones', '--severity-all', '--format', 'json', '-o', a.board + '.sigdrc.json', a.board], capture_output=True)
d = D.load(a.board + '.sigdrc.json'); u, e, w = D.score(d)
print('after linking: unconnected %d, errors %d' % (u, e))
