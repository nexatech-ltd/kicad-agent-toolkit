#!/usr/bin/env python3
"""What blocks widening a net's narrow segments to class width: for every segment narrower than the class width
(except necks <= KEEP near pads/vias of its own net) - foreign objects on the same layer within the band
(width/2 + clearance + their half-width).
  python3 tools/widen_walls.py --board B [--nets A,B | --classes MOTOR,PWR] [--width 0.4] [--clr 0.15]
Report per net: narrow length, walls (net@layer: how many segments they block). Pad walls and plane nets are flagged."""
import sys, os, json, math, fnmatch, argparse, collections, pcbnew
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maze_route as MR
import paths, project
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--nets'); ap.add_argument('--classes')
ap.add_argument('--width', type=float); ap.add_argument('--clr', type=float, default=0.15); ap.add_argument('--keep', type=float, default=3.0)
ap.add_argument('--pro', default=None, help='.kicad_pro (default: next to the board)'); ap.add_argument('--json')
a = ap.parse_args(); MM = 1e6
a.pro = a.pro or paths.sibling(a.board, '.kicad_pro')
b = pcbnew.LoadBoard(a.board); ns = json.load(open(a.pro))['net_settings']
W = {c['name']: float(c['track_width']) for c in ns['classes']}; pats = [(x['netclass'], x['pattern']) for x in ns.get('netclass_patterns', [])]
PLANES = project.plane_nets()
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
def segseg(a1, a2, b1, b2):
    return min(dseg(*a1, *b1, *b2), dseg(*a2, *b1, *b2), dseg(*b1, *a1, *a2), dseg(*b2, *a1, *a2))
padc = collections.defaultdict(list); vias = collections.defaultdict(list)
TR = list(b.GetTracks()); PADS = [(fp.GetReference() + '.' + pd.GetNumber(), pd) for fp in b.GetFootprints() for pd in fp.Pads()]
for name, pd in PADS:
    if pd.GetNetname(): padc[pd.GetNetname().split('/')[-1]].append((pd.GetPosition().x / MM, pd.GetPosition().y / MM))
for t in TR:
    if t.GetClass() == 'PCB_VIA' and t.GetNetname(): vias[t.GetNetname().split('/')[-1]].append((t.GetPosition().x / MM, t.GetPosition().y / MM))
by_layer = collections.defaultdict(list)
for t in TR:
    if t.GetClass() == 'PCB_TRACK': by_layer[t.GetLayer()].append(t)
out = collections.defaultdict(lambda: {'len': 0.0, 'n': 0, 'walls': collections.Counter(), 'segs': []})
for t in TR:
    if t.GetClass() != 'PCB_TRACK' or not t.GetNetname(): continue
    net = t.GetNetname(); base = net.split('/')[-1]; c = cls_of(net)
    if want_nets and base not in want_nets: continue
    if want_cls and c not in want_cls: continue
    need = a.width or W.get(c, 0.2); w = t.GetWidth() / MM
    if w >= need - 0.005: continue
    s, e = t.GetStart(), t.GetEnd(); x1, y1, x2, y2 = s.x / MM, s.y / MM, e.x / MM, e.y / MM; L = math.hypot(x2 - x1, y2 - y1)
    pts = padc.get(base, []) + vias.get(base, [])
    if pts and L <= a.keep:
        d1 = min(math.hypot(px - x1, py - y1) for px, py in pts); d2 = min(math.hypot(px - x2, py - y2) for px, py in pts)
        if max(d1, d2) < a.keep: continue
    Lr = t.GetLayer(); walls = set()
    for o in by_layer[Lr]:
        if o.GetNetCode() == t.GetNetCode(): continue
        os_, oe = o.GetStart(), o.GetEnd()
        if segseg((x1, y1), (x2, y2), (os_.x / MM, os_.y / MM), (oe.x / MM, oe.y / MM)) < need / 2 + tt_clr(net, o.GetNetname(), a.clr) + o.GetWidth() / MM / 2:
            walls.add(o.GetNetname().split('/')[-1] + '@' + b.GetLayerName(Lr))
    for o in TR:
        if o.GetClass() != 'PCB_VIA' or o.GetNetCode() == t.GetNetCode(): continue
        p = o.GetPosition()
        if dseg(p.x / MM, p.y / MM, x1, y1, x2, y2) < need / 2 + a.clr + o.GetWidth() / MM / 2: walls.add(o.GetNetname().split('/')[-1] + '@via')
    for name, pd in PADS:
        if pd.GetNetCode() == t.GetNetCode() or not pd.IsOnLayer(Lr): continue
        p = pd.GetPosition(); sz = pd.GetSize(); r = math.hypot(sz.x / MM, sz.y / MM) / 2
        if dseg(p.x / MM, p.y / MM, x1, y1, x2, y2) < need / 2 + a.clr + r: walls.add((pd.GetNetname().split('/')[-1] or '-') + '@pad ' + name)
    o = out[base]; o['len'] += L; o['n'] += 1; o['walls'].update(walls)
    o['segs'].append({'layer': b.GetLayerName(Lr), 'w': round(w, 3), 'L': round(L, 2), 'a': [round(x1, 3), round(y1, 3)], 'b': [round(x2, 3), round(y2, 3)], 'walls': sorted(walls)})
tot_walls = collections.Counter()
for base, o in sorted(out.items(), key=lambda kv: -kv[1]['len']):
    ws = ', '.join('%s×%d' % (k, v) for k, v in o['walls'].most_common(8))
    hard = [k for k in o['walls'] if k.split('@')[0] in PLANES or '@pad' in k]
    print('%-14s narrow %2d, %5.1f mm; walls: %s%s' % (base, o['n'], o['len'], ws, ('  [hard: %s]' % ', '.join(hard[:4])) if hard else ''))
    for k, v in o['walls'].items(): tot_walls[k.split('@')[0]] += v
print('\nmost frequent blockers:', tot_walls.most_common(15))
if a.json: json.dump({k: {'len': v['len'], 'n': v['n'], 'segs': v['segs']} for k, v in out.items()}, open(a.json, 'w'), indent=0); print('->', a.json)
