#!/usr/bin/env python3
"""Remove GND stitching vias (vias without tracks) that stand as a wall next to narrow segments of power/motor nets.
  python3 tools/unstitch_walls.py --board B --classes MOTOR,PWR,... [--net GND] [--apply]
A stitch is a GND via that no track reaches (connected through pours only). It blocks widening a segment if it lies
within the band (class width/2 + clearance + via radius) of it. Afterwards run widen_segs.py and DRC; check GND
connectivity with ratsnest_pos.py (there are many stitches, but planes with islands sometimes have a single link)."""
import sys, os, json, math, fnmatch, argparse, collections, pcbnew
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True)
ap.add_argument('--classes', required=True, help='comma-separated net classes whose narrow segments are checked')
ap.add_argument('--net', default='GND', help='stitching net (default GND)')
ap.add_argument('--clr', type=float, default=0.15); ap.add_argument('--keep', type=float, default=3.0); ap.add_argument('--apply', action='store_true')
ap.add_argument('--pro', default=None, help='.kicad_pro (default: next to the board)')
a = ap.parse_args(); MM = 1e6
a.pro = a.pro or paths.sibling(a.board, '.kicad_pro')
b = pcbnew.LoadBoard(a.board); ns = json.load(open(a.pro))['net_settings']
W = {c['name']: float(c['track_width']) for c in ns['classes']}; pats = [(x['netclass'], x['pattern']) for x in ns.get('netclass_patterns', [])]
def cls_of(net):
    base = net.split('/')[-1]
    for c, pat in pats:
        if fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(net, pat): return c
    return 'Default'
want = set(a.classes.split(','))
def dseg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2)); return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
TR = list(b.GetTracks()); segs = [t for t in TR if t.GetClass() == 'PCB_TRACK']; vias = [t for t in TR if t.GetClass() == 'PCB_VIA']
padc = collections.defaultdict(list)
for fp in b.GetFootprints():
    for pd in fp.Pads():
        if pd.GetNetname(): padc[pd.GetNetname().split('/')[-1]].append((pd.GetPosition().x / MM, pd.GetPosition().y / MM))
viac = collections.defaultdict(list)
for v in vias:
    if v.GetNetname(): viac[v.GetNetname().split('/')[-1]].append((v.GetPosition().x / MM, v.GetPosition().y / MM))
gnd_vias = [v for v in vias if v.GetNetname() == a.net]
def is_stitch(v):
    p = v.GetPosition(); r = v.GetWidth() / MM / 2 + 0.01
    for s in segs:
        if s.GetNetCode() != v.GetNetCode(): continue
        for q in (s.GetStart(), s.GetEnd()):
            if math.hypot(q.x / MM - p.x / MM, q.y / MM - p.y / MM) <= r: return False
    return True
victims = {}
for t in segs:
    if not t.GetNetname(): continue
    net = t.GetNetname(); base = net.split('/')[-1]; c = cls_of(net)
    if c not in want: continue
    need = W.get(c, 0.2); w = t.GetWidth() / MM
    if w >= need - 0.005: continue
    s, e = t.GetStart(), t.GetEnd(); x1, y1, x2, y2 = s.x / MM, s.y / MM, e.x / MM, e.y / MM; L = math.hypot(x2 - x1, y2 - y1)
    pts = padc.get(base, []) + viac.get(base, [])
    if pts and L <= a.keep:
        d1 = min(math.hypot(px - x1, py - y1) for px, py in pts); d2 = min(math.hypot(px - x2, py - y2) for px, py in pts)
        if max(d1, d2) < a.keep: continue
    for v in gnd_vias:
        p = v.GetPosition()
        if dseg(p.x / MM, p.y / MM, x1, y1, x2, y2) < need / 2 + a.clr + v.GetWidth() / MM / 2:
            u = v.m_Uuid.AsString()
            if u not in victims and is_stitch(v): victims[u] = (v, base)
print('stitching vias next to narrow segments: %d - %s' % (len(victims), sorted(collections.Counter(n for _, n in victims.values()).items())))
if a.apply and victims:
    for v, _ in victims.values(): b.Remove(v)
    pcbnew.SaveBoard(a.board, b); print('removed %d, written %s' % (len(victims), a.board))
