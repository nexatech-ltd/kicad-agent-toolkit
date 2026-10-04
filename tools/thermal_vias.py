#!/usr/bin/env python3
"""Thermal vias under exposed pads (EP): a via array into the pad's own plane; DRC is the judge.
  python3 tools/thermal_vias.py --board B --refs U1,U2 [--pitch 1.2] [--via 0.5,0.25] [--apply]
Vias are placed only inside the EP, inset from its edge, where no foreign copper is near on any layer
and the pad's own net has a fill under the point on at least one non-top copper layer. Existing vias
are taken into account."""
import sys, os, math, json, argparse, subprocess, shutil
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, pcbnew, drcinfo as D
MM = 1e6
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--refs', required=True)
ap.add_argument('--pitch', type=float, default=1.2); ap.add_argument('--via', default='0.5,0.25')
ap.add_argument('--margin', type=float, default=0.45); ap.add_argument('--clr', type=float, default=0.15)
ap.add_argument('--all-eps', action='store_true',
                help='all thermal pads of the package, not only the largest: some LGA modules have a 3x3 '
                     'array of thermal pads, and stitching only one of them cools the module several times worse')
ap.add_argument('--min-ep', type=float, default=4.0, help='mm^2, minimum pad area to treat a pad as a thermal pad')
ap.add_argument('--apply', action='store_true')
a = ap.parse_args(); VIA, DRILL = (float(v) for v in a.via.split(','))
b = pcbnew.LoadBoard(a.board)
byref = {f.GetReference(): f for f in b.GetFootprints()}
# every copper layer below the top one (inner planes and B.Cu)
inner = [L for L in b.GetEnabledLayers().CuStack() if L != pcbnew.F_Cu]

def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1; L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

ops = []
for ref in a.refs.split(','):
    f = byref.get(ref)
    if not f: print('  %s: not on the board' % ref); continue
    eps = sorted(f.Pads(), key=lambda p: -(p.GetSize().x * p.GetSize().y))
    if a.all_eps:
        eps = [p for p in eps if p.GetSize().x / MM * p.GetSize().y / MM >= a.min_ep]
    else:
        eps = eps[:1]
    if not eps:
        print('  %-5s no thermal pads found' % ref); continue
    for ep in eps:
        net = ep.GetNetname()
        if not net:
            print('  %-5s thermal pad has no net, skipped (fix the schematic)' % ref); continue
        c = ep.GetPosition(); sz = ep.GetSize()
        x0, x1 = c.x / MM - sz.x / MM / 2 + a.margin, c.x / MM + sz.x / MM / 2 - a.margin
        y0, y1 = c.y / MM - sz.y / MM / 2 + a.margin, c.y / MM + sz.y / MM / 2 - a.margin
        zones = [z for z in b.Zones() if z.GetNetname() == net and not z.GetIsRuleArea()]
        others = []
        for t in b.GetTracks():
            if t.GetNetname() == net: continue
            if t.GetClass() == 'PCB_VIA':
                p = t.GetPosition()
                if abs(p.x / MM - c.x / MM) < 6 and abs(p.y / MM - c.y / MM) < 6: others.append(('v', p.x / MM, p.y / MM, t.GetWidth() / MM / 2))
            else:
                s, e = t.GetStart(), t.GetEnd()
                if min(s.x, e.x) / MM > c.x / MM + 6 or max(s.x, e.x) / MM < c.x / MM - 6: continue
                others.append(('t', s.x / MM, s.y / MM, e.x / MM, e.y / MM, t.GetWidth() / MM / 2))
        have = [(t.GetPosition().x / MM, t.GetPosition().y / MM) for t in b.GetTracks()
                if t.GetClass() == 'PCB_VIA' and x0 - 1 <= t.GetPosition().x / MM <= x1 + 1 and y0 - 1 <= t.GetPosition().y / MM <= y1 + 1]
        n = 0
        nx = max(1, int((x1 - x0) / a.pitch) + 1); ny = max(1, int((y1 - y0) / a.pitch) + 1)
        for i in range(nx):
            for j in range(ny):
                x = x0 + (x1 - x0) * (i / max(1, nx - 1) if nx > 1 else 0.5)
                y = y0 + (y1 - y0) * (j / max(1, ny - 1) if ny > 1 else 0.5)
                if any(math.hypot(x - hx, y - hy) < VIA + 0.15 for hx, hy in have): continue
                ok = True
                for o in others:
                    d = math.hypot(x - o[1], y - o[2]) - o[3] if o[0] == 'v' else dseg(x, y, o[1], o[2], o[3], o[4]) - o[5]
                    if d < VIA / 2 + a.clr: ok = False; break
                if not ok: continue
                P = pcbnew.VECTOR2I(int(x * MM), int(y * MM))
                if not any(z.HitTestFilledArea(L, P) for z in zones for L in inner if z.IsOnLayer(L)): continue
                ops.append({'op': 'add_via', 'net': net.split('/')[-1], 'x': round(x, 3), 'y': round(y, 3), 'size': VIA, 'drill': DRILL})
                have.append((x, y)); n += 1
        print('  %-5s EP %.1fx%.1f (%s): placing %d vias' % (ref, sz.x / MM, sz.y / MM, net.split('/')[-1], n))
print('thermal vias total: %d' % len(ops))
if not a.apply or not ops: sys.exit(0)
json.dump(ops, open(a.board + '.tvias.json', 'w'))
shutil.copy(a.board, a.board + '.tviasbak')
subprocess.run([sys.executable, os.path.join(HERE, 'util', 'edit_ops.py'), a.board, a.board + '.tvias.json', '--apply'], capture_output=True)
subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones', '--severity-all', '--format', 'json', '-o', a.board + '.tviasdrc.json', a.board], capture_output=True)
d = D.load(a.board + '.tviasdrc.json'); u, e, w = D.score(d)
print('after placement: unconnected %d, errors %d' % (u, e))
