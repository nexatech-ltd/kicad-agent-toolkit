"""Geometric search for through-via spots of net NET inside a window (no router model, ~1 s).

Usage: python3 tools/util/via_spots.py BOARD.kicad_pcb NET x0,y0,x1,y1 [--via 0.5] [--clr 0.15]
           [--step 0.05] [--top 6] [--walls] [--rank-walls] [--need-fill F.Cu,In1.Cu]

For every grid point computes the margin to the nearest foreign copper on all layers (tracks,
vias, pads; zone fills are ignored) and prints the best spots.
--walls: for the best spots with a negative margin, list the nets/layers in the way (the wall
         to rip up).
--rank-walls: rank spots by the number of blocking signal nets; spots blocked by pads or by
         plane/supply nets (project plane_nets + supply_nets) are dropped.
--need-fill: keep only points where the net has a zone fill on one of the given layers.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import os, sys, math, pcbnew
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import project
a = sys.argv; bp, net = a[1], a[2]; x0, y0, x1, y1 = (float(v) for v in a[3].split(','))
def opt(k, d): return float(a[a.index(k) + 1]) if k in a else d
VIA, CLR, STEP, TOP = opt('--via', 0.5), opt('--clr', 0.15), opt('--step', 0.05), int(opt('--top', 6)); WALLS = '--walls' in a; RANK = '--rank-walls' in a
NEED = a[a.index('--need-fill') + 1].split(',') if '--need-fill' in a else None   # only points where the net has a fill on one of these layers
PLANES = project.plane_nets() | set(project.get('supply_nets', []))   # nets never ripped for a via
b = pcbnew.LoadBoard(bp); MM = 1e6; R = VIA / 2
segs = []   # (net, layer, ax, ay, bx, by, halfw)
discs = []  # (net, what, x, y, radius)
PAD = 1.5
for t in b.GetTracks():
    n = t.GetNetname().split('/')[-1]
    if n == net: continue
    if t.GetClass() == 'PCB_VIA':
        p = t.GetPosition(); discs.append((n, 'via', p.x / MM, p.y / MM, t.GetWidth() / MM / 2))
    else:
        s, e = t.GetStart(), t.GetEnd()
        if min(s.x, e.x) / MM > x1 + PAD or max(s.x, e.x) / MM < x0 - PAD or min(s.y, e.y) / MM > y1 + PAD or max(s.y, e.y) / MM < y0 - PAD: continue
        segs.append((n, b.GetLayerName(t.GetLayer()), s.x / MM, s.y / MM, e.x / MM, e.y / MM, t.GetWidth() / MM / 2))
for fp in b.GetFootprints():
    for pd in fp.Pads():
        if not pd.IsOnCopperLayer(): continue   # paste-only EP pads have no copper
        n = pd.GetNetname().split('/')[-1]; p = pd.GetPosition(); sz = pd.GetSize()
        if abs(p.x / MM - (x0 + x1) / 2) > (x1 - x0) / 2 + PAD or abs(p.y / MM - (y0 + y1) / 2) > (y1 - y0) / 2 + PAD: continue
        if n == net and n: continue
        # pad as a rectangle -> distance to the rectangle
        discs.append((n or '(no net)', 'pad %s.%s' % (fp.GetReference(), pd.GetNumber()), p.x / MM, p.y / MM, (sz.x / MM, sz.y / MM, pd.GetOrientationDegrees())))
def dseg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
def drect(px, py, cx, cy, sx, sy, rot):
    th = math.radians(-rot); dx, dy = px - cx, py - cy
    u, v = dx * math.cos(th) - dy * math.sin(th), dx * math.sin(th) + dy * math.cos(th)
    return math.hypot(max(abs(u) - sx / 2, 0), max(abs(v) - sy / 2, 0))
zones = [z for z in b.Zones() if z.GetNetname().split('/')[-1] == net] if NEED else []
def has_fill(x, y):
    P = pcbnew.VECTOR2I(int(x * MM), int(y * MM))
    return any(z.HitTestFilledArea(b.GetLayerID(L), P) for z in zones for L in NEED if z.IsOnLayer(b.GetLayerID(L)))
res = []
y = y0
while y <= y1 + 1e-9:
    x = x0
    while x <= x1 + 1e-9:
        worst = (9, None)
        for (n, L, ax, ay, bx, by, hw) in segs:
            m = dseg(x, y, ax, ay, bx, by) - hw - R - CLR
            if m < worst[0]: worst = (m, (n, L))
        for (n, what, cx, cy, r) in discs:
            m = (drect(x, y, cx, cy, *r) if isinstance(r, tuple) else math.hypot(x - cx, y - cy) - r) - R - CLR
            if m < worst[0]: worst = (m, (n, what))
        if NEED is None or has_fill(x, y): res.append((worst[0], round(x, 3), round(y, 3), worst[1]))
        x += STEP
    y += STEP
def wallset(x, y):
    W = set(); bad = False
    for (n, L, ax, ay, bx, by, hw) in segs:
        if dseg(x, y, ax, ay, bx, by) - hw - R - CLR < 0: W.add(n); bad |= n in PLANES
    for (n, what, cx, cy, r) in discs:
        if (drect(x, y, cx, cy, *r) if isinstance(r, tuple) else math.hypot(x - cx, y - cy) - r) - R - CLR < 0: W.add(n); bad |= n in PLANES or what.startswith('pad')
    return W, bad
if RANK:
    ranked = []
    for m, x, y, who in res:
        if m >= 0: ranked.append((0, -m, x, y, ())); continue
        W, bad = wallset(x, y)
        if not bad: ranked.append((len(W), -m, x, y, tuple(sorted(W))))
    ranked.sort()
    chosen = []
    for k, nm, x, y, W in ranked:
        if any(math.hypot(x - cx, y - cy) < VIA + 0.2 for _, _, cx, cy, _ in chosen): continue
        chosen.append((k, nm, x, y, W))
        if len(chosen) >= TOP: break
    for k, nm, x, y, W in chosen: print('  (%.2f, %.2f) walls %d, margin %+.3f: %s' % (x, y, k, -nm, ', '.join(W)))
    sys.exit(0)
res.sort(key=lambda r: -r[0])
print('window %s, net %s, via %.2f, clearance %.2f: points %d, usable (margin>=0) %d' % (a[3], net, VIA, CLR, len(res), sum(1 for r in res if r[0] >= 0)))
chosen = []
for m, x, y, who in res:
    if any(math.hypot(x - cx, y - cy) < VIA + 0.2 for _, cx, cy, _ in chosen): continue
    chosen.append((m, x, y, who))
    if len(chosen) >= TOP: break
for m, x, y, who in chosen:
    line = '  (%.2f, %.2f) margin %+.3f  nearest: %s' % (x, y, m, who)
    if WALLS and m < 0:
        walls = set()
        for (n, L, ax, ay, bx, by, hw) in segs:
            if dseg(x, y, ax, ay, bx, by) - hw - R - CLR < 0: walls.add('%s@%s' % (n, L))
        for (n, what, cx, cy, r) in discs:
            if (drect(x, y, cx, cy, *r) if isinstance(r, tuple) else math.hypot(x - cx, y - cy) - r) - R - CLR < 0: walls.add('%s@%s' % (n, what))
        line += '  wall: ' + ', '.join(sorted(walls))
    print(line)
