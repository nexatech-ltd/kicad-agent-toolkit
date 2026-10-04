#!/usr/bin/env python3
"""Widen narrow segments one by one to the maximum the neighbours' geometry allows (not all-or-nothing class width).
  python3 tools/widen_segs.py --board B [--classes MOTOR,PWR,...] [--nets A,B] [--min-step 0.05] [--apply]
For every segment narrower than its class width (except necks <= 3 mm near pads/vias of its own net): width =
min(class width, 2*(distance to the nearest foreign object on the same layer - clearance - its half-width)),
rounded down to a 0.05 step, never below the current one. Pads are treated as rotated rectangles (a circle over
the diagonal forbade widening next to power packages). DRC judges afterwards: segments in violations go back
to their previous width (`set_widths.py`), looping until zero errors. Unconnected items must not increase."""
import sys, os, json, math, fnmatch, argparse, collections, subprocess, pcbnew
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import drcinfo as D
import maze_route as MR
import paths
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--nets'); ap.add_argument('--classes')
ap.add_argument('--clr', type=float, default=0.15); ap.add_argument('--keep', type=float, default=3.0); ap.add_argument('--min-step', type=float, default=0.05)
ap.add_argument('--pro', default=None, help='.kicad_pro (default: next to the board)'); ap.add_argument('--apply', action='store_true')
a = ap.parse_args(); MM = 1e6
a.pro = a.pro or paths.sibling(a.board, '.kicad_pro')
b = pcbnew.LoadBoard(a.board); ns = json.load(open(a.pro))['net_settings']
W = {c['name']: float(c['track_width']) for c in ns['classes']}; pats = [(x['netclass'], x['pattern']) for x in ns.get('netclass_patterns', [])]
def tt_clr(net_a, net_b, base):
    # track<->track clearance: .kicad_dru rules by the class of either side (e.g. a 0.25 mm motor-class spacing)
    return max(base, MR.DRU_CLEARANCE.get(cls_of(net_a), 0.0), MR.DRU_CLEARANCE.get(cls_of(net_b), 0.0))
def cls_of(net):
    base = net.split('/')[-1]
    for c, pat in pats:
        if fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(net, pat): return c
    return 'Default'
want_nets = set(a.nets.split(',')) if a.nets else None; want_cls = set(a.classes.split(',')) if a.classes else None
def dseg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2)); return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
def segseg(a1, a2, b1, b2): return min(dseg(*a1, *b1, *b2), dseg(*a2, *b1, *b2), dseg(*b1, *a1, *a2), dseg(*b2, *a1, *a2))
def pad_angle(pd):
    try: return math.radians(pd.GetOrientation().AsDegrees())
    except AttributeError: return math.radians(pd.GetOrientationDegrees())


def dseg_rect(x1, y1, x2, y2, cx, cy, hw, hh, ang, steps=24):
    """Distance from a segment to a pad's rotated rectangle.

    A pad used to be modelled as a circle of half its diagonal. For 0402 the error is tenths of a
    millimetre, but for power packages it is prohibitive: for a 2.9 x 5.4 mm inductor pad that
    circle has a 3.07 mm radius instead of the half-sizes, and a power run next to it could not be
    widened at all. Use the rectangle: the point is rotated into the pad's frame.
    """
    c, sn = math.cos(ang), math.sin(ang)
    best = 9.0
    for i in range(steps + 1):
        t = i / steps
        px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        dx = abs((px - cx) * c + (py - cy) * sn) - hw
        dy = abs(-(px - cx) * sn + (py - cy) * c) - hh
        best = min(best, math.hypot(max(dx, 0.0), max(dy, 0.0)))
        if best <= 0: return 0.0
    return best


TR = list(b.GetTracks()); PADS = [pd for fp in b.GetFootprints() for pd in fp.Pads()]
padc = collections.defaultdict(list); vias = collections.defaultdict(list); by_layer = collections.defaultdict(list); VIAS = []
for pd in PADS:
    if pd.GetNetname(): padc[pd.GetNetname().split('/')[-1]].append((pd.GetPosition().x / MM, pd.GetPosition().y / MM))
for t in TR:
    if t.GetClass() == 'PCB_VIA':
        VIAS.append(t)
        if t.GetNetname(): vias[t.GetNetname().split('/')[-1]].append((t.GetPosition().x / MM, t.GetPosition().y / MM))
    else: by_layer[t.GetLayer()].append(t)
# keepout zones / board edge are ignored here - DRC checks them
changes = []   # (track, old_w, new_w)
for t in TR:
    if t.GetClass() != 'PCB_TRACK' or not t.GetNetname(): continue
    net = t.GetNetname(); base = net.split('/')[-1]; c = cls_of(net)
    if want_nets and base not in want_nets: continue
    if want_cls and c not in want_cls: continue
    need = W.get(c, 0.2); w = t.GetWidth() / MM
    if w >= need - 0.005: continue
    s, e = t.GetStart(), t.GetEnd(); x1, y1, x2, y2 = s.x / MM, s.y / MM, e.x / MM, e.y / MM; L = math.hypot(x2 - x1, y2 - y1)
    pts = padc.get(base, []) + vias.get(base, [])
    if pts and L <= a.keep:
        d1 = min(math.hypot(px - x1, py - y1) for px, py in pts); d2 = min(math.hypot(px - x2, py - y2) for px, py in pts)
        if max(d1, d2) < a.keep: continue
    Lr = t.GetLayer(); room = need
    for o in by_layer[Lr]:
        if o.GetNetCode() == t.GetNetCode(): continue
        os_, oe = o.GetStart(), o.GetEnd(); d = segseg((x1, y1), (x2, y2), (os_.x / MM, os_.y / MM), (oe.x / MM, oe.y / MM))
        room = min(room, 2 * (d - tt_clr(net, o.GetNetname(), a.clr) - o.GetWidth() / MM / 2))
        if room < w + a.min_step: break
    if room >= w + a.min_step:
        for o in VIAS:
            if o.GetNetCode() == t.GetNetCode(): continue
            p = o.GetPosition(); room = min(room, 2 * (dseg(p.x / MM, p.y / MM, x1, y1, x2, y2) - a.clr - o.GetWidth() / MM / 2))
            if room < w + a.min_step: break
    if room >= w + a.min_step:
        for pd in PADS:
            if pd.GetNetCode() == t.GetNetCode() or not pd.IsOnLayer(Lr): continue
            p = pd.GetPosition(); sz = pd.GetSize()
            room = min(room, 2 * (dseg_rect(x1, y1, x2, y2, p.x / MM, p.y / MM,
                                            sz.x / MM / 2, sz.y / MM / 2, pad_angle(pd)) - a.clr))
            if room < w + a.min_step: break
    new = math.floor(min(need, room) / a.min_step + 1e-9) * a.min_step
    if new >= w + a.min_step - 1e-9: changes.append((t, w, round(new, 3)))
byc = collections.Counter((cls_of(t.GetNetname()), round(nw, 2)) for t, ow, nw in changes)
print('segments to widen: %d; by class/new width: %s' % (len(changes), dict(byc.most_common(12))))
if not a.apply or not changes: sys.exit(0)
for t, ow, nw in changes: t.SetWidth(int(round(nw * MM)))
pcbnew.SaveBoard(a.board, b); print('written', a.board)
# DRC judges: segments in errors go back to their previous width (set_widths.py in a subprocess), repeat until zero errors
for rnd in range(6):
    out = a.board + '.widen_segs_drc%d.json' % (rnd + 1)
    subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones', '--severity-all', '--format', 'json', '-o', out, a.board], capture_output=True)
    d = D.load(out); u, e, wn = D.score(d)
    print('  check %d: unconnected %d, errors %d' % (rnd, u, e))
    if e == 0: break
    bad = []
    for x in d['violations']:
        if x['severity'] != 'error': continue
        for it in x['items']:
            if it['description'].startswith('Track'): bad.append((it['pos']['x'], it['pos']['y']))
    # restore the previous width of changed segments whose violation point lies on the segment
    rest = []
    for t, ow, nw in changes:
        s, e_ = t.GetStart(), t.GetEnd()
        if any(dseg(px, py, s.x / MM, s.y / MM, e_.x / MM, e_.y / MM) < nw / 2 + 0.02 for px, py in bad): rest.append((t, ow))
    if not rest: print('  violations are not on widened segments - stop'); break
    spec = {t.m_Uuid.AsString(): int(round(ow * MM)) for t, ow in rest}
    json.dump(spec, open(a.board + '.restore.json', 'w'))
    r = subprocess.run([sys.executable, os.path.join(HERE, 'set_widths.py'), '--board', a.board, '--json', a.board + '.restore.json'], capture_output=True, text=True)
    print('  restored width of %d segments: %s' % (len(rest), (r.stdout.strip().splitlines() or ['?'])[-1]))
    changes = [(t, ow, nw) for t, ow, nw in changes if not any(t is r_[0] for r_ in rest)]
