#!/usr/bin/env python3
"""A via into its own plane for every unconnected pad of a plane net.

Plane nets (project.plane_nets(), e.g. GND and +3V3) are poured as zones on one or more
layers and handed to the router with an empty pin list, so the router does not route
them. A pad of such a net with no via into its plane therefore stays unconnected
forever: nobody else will connect it.

Targets come from the DRC report, not a heuristic: unconnected pairs that involve a
pad of a plane net. These are the same facts KiCad judges by.

The via spot is searched exhaustively around the pad: for 0.4-0.5 mm pitch pins a free
pocket appears wherever the neighbouring copper left one, and its direction cannot be
predicted. The point is also checked to lie inside a filled area of the net's own
plane, otherwise the via connects to nothing.

Usage:  python3 fix_plane_pads.py <drc.json>            (plan)
        python3 fix_plane_pads.py <drc.json> --apply    (write)
Board: $BOARD / $BRD or the project board (paths.BOARD); BRD is for runs on a copy.
"""
import json, re, sys, os, math, uuid, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py is a sibling
from sexp import blocks
import paths, project

BRD  = paths.BOARD          # honours $BOARD / $BRD, for runs on a copy
PRO  = paths.sibling(BRD, '.kicad_pro')
PLANE_NETS = project.plane_nets()
# Net class of a plane net when its class name differs from the net name, e.g. {"+3V3": "V3P3"}
PLANE_NET_CLASS = project.get('plane_net_classes', {})

if len(sys.argv) < 2 or sys.argv[1].startswith('--'):
    raise SystemExit(__doc__)
DRC = sys.argv[1]
APPLY = '--apply' in sys.argv

_lck = os.path.join(os.path.dirname(os.path.abspath(BRD)), '~' + os.path.basename(BRD) + '.lck')
if os.path.exists(_lck) and APPLY:
    raise SystemExit(f'REFUSED: the board is open in the KiCad editor ({_lck}).')

CLEAR_CU, CLEAR_HOLE = 0.15, 0.25

s = open(BRD).read()
# copper layers of this board, for pads on "*.Cu"
_hdr = s[:s.find('(setup')] if '(setup' in s else s[:20000]
SIG = re.findall(r'\(\d+ "([A-Za-z0-9_]+\.Cu)"', _hdr) or ['F.Cu', 'B.Cu']
WIDTH = {c['name']: float(c['track_width']) for c in json.load(open(PRO))['net_settings']['classes']}

def _sub(blk, tag):
    res = []
    for m in re.finditer(r'\(' + re.escape(tag) + r'(?=[\s(])', blk):
        i = m.start(); j = i; d = 0; q = False
        while j < len(blk):
            ch = blk[j]
            if q:
                if ch == '\\': j += 2; continue
                if ch == '"': q = False
            else:
                if ch == '"': q = True
                elif ch == '(': d += 1
                elif ch == ')':
                    d -= 1
                    if d == 0: res.append(blk[i:j+1]); break
            j += 1
    return res

def rot(px, py, deg):
    a = math.radians(deg); c, si = math.cos(a), math.sin(a)
    return px * c + py * si, -px * si + py * c

# ---------- pads ----------
pads = []       # (net, layers, x0,y0,x1,y1, ref, pid, cx, cy, w, h)
fpc = {}        # ref -> footprint centre, needed for the "outward" direction
# Holes are handled SEPARATELY from copper: hole_to_hole is a mechanical limit
# and the net does not matter. Two holes cannot be drilled at one point even if
# they are electrically connected. Excluding holes of the own net once put new
# vias right on top of existing ones (0.0000 mm), reported by DRC as hole_to_hole.
holes = []      # (x, y, hole radius): all holes of the board
for a, b in blocks(s, 'footprint'):
    blk = s[a:b]
    rm = re.search(r'\(property "Reference" "([^"]*)"', blk)
    am = re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', blk)
    if not (rm and am): continue
    ref = rm.group(1); fx, fy, fr = float(am.group(1)), float(am.group(2)), float(am.group(3) or 0)
    fpc[ref] = (fx, fy)
    for p in _sub(blk, 'pad'):
        pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', p)
        sz = re.search(r'\(size ([\d.]+) ([\d.]+)\)', p)
        pid = re.search(r'\(pad "([^"]*)"', p)
        if not (pa and sz and pid): continue
        nm = re.search(r'\(net "([^"]*)"\)', p)
        # The pad angle is the third argument of its (at ...); when absent it is
        # ZERO, not the footprint angle (KiCad stores pad angles in board axes).
        pang = float(pa.group(3)) if pa.group(3) is not None else 0.0
        dx, dy = rot(float(pa.group(1)), float(pa.group(2)), fr)
        w, h = float(sz.group(1)), float(sz.group(2))
        if abs(pang % 180) > 45: w, h = h, w
        lys = re.search(r'\(layers ([^)]*)\)', p)
        lay = re.findall(r'"([^"]+)"', lys.group(1)) if lys else []
        cu = list(SIG) if any(l == '*.Cu' for l in lay) else [l for l in lay if l.endswith('.Cu')]
        cx, cy = fx + dx, fy + dy
        _dm = re.search(r'\(drill (?:oval )?([\d.]+)', p)
        if _dm:
            holes.append((cx, cy, float(_dm.group(1)) / 2))
        pads.append((nm.group(1) if nm else '', cu, cx-w/2, cy-h/2, cx+w/2, cy+h/2,
                     ref, pid.group(1), cx, cy, w, h))
assert len(pads) > 0, f'{len(pads)} pads parsed'

vias = []
for a, b in blocks(s, 'via'):
    blk = s[a:b]
    at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)\)', blk)
    nm = re.search(r'\(net "([^"]*)"\)', blk)
    sz = re.search(r'\(size ([\d.]+)\)', blk)
    dr = re.search(r'\(drill ([\d.]+)\)', blk)
    if at and sz:
        vias.append((nm.group(1) if nm else '', float(at.group(1)), float(at.group(2)), float(sz.group(1))))
    if at and dr:
        holes.append((float(at.group(1)), float(at.group(2)), float(dr.group(1)) / 2))

segs = []
for a, b in blocks(s, 'segment'):
    blk = s[a:b]
    nm = re.search(r'\(net "([^"]*)"\)', blk); ly = re.search(r'\(layer "([^"]+)"\)', blk)
    st = re.search(r'\(start (-?[\d.]+) (-?[\d.]+)\)', blk); en = re.search(r'\(end (-?[\d.]+) (-?[\d.]+)\)', blk)
    wd = re.search(r'\(width ([\d.]+)\)', blk)
    if not (nm and ly and st and en and wd): continue
    segs.append((nm.group(1), ly.group(1), float(wd.group(1)),
                 float(st.group(1)), float(st.group(2)), float(en.group(1)), float(en.group(2))))

# ---------- filled areas of zones by net ----------
fills = collections.defaultdict(list)      # (net, layer) -> [polygon]
for a, b in blocks(s, 'zone'):
    blk = s[a:b]
    nm = re.search(r'\(net_name "([^"]*)"\)', blk) or re.search(r'\(zone \(net "([^"]*)"\)', blk)
    if not nm:
        nm = re.search(r'\(net "([^"]*)"\)', blk)
    if not nm: continue
    net = nm.group(1)
    lm = re.search(r'\(layers? ((?:"[^"]+"\s*)+)\)', blk) or re.search(r'\(layer "([^"]+)"\)', blk)
    lys = re.findall(r'"([^"]+)"', lm.group(1)) if lm else []
    for fp in _sub(blk, 'filled_polygon'):
        flm = re.search(r'\(layer "([^"]+)"\)', fp)
        ly = flm.group(1) if flm else (lys[0] if lys else None)
        pts = [(float(x), float(y)) for x, y in re.findall(r'\(xy (-?[\d.]+) (-?[\d.]+)\)', fp)]
        if ly and len(pts) > 2:
            fills[(net, ly)].append(pts)
# ---------- keepout zones ----------
# A 0.8 mm via reaches into a keepout with its edge even when its centre is
# outside: checking only the centre once put a via 0.27 mm from the edge of an
# antenna keepout and produced items_not_allowed.
keepouts = []      # (polygon, layers)
for a, b in blocks(s, 'zone'):
    blk = s[a:b]
    if 'keepout' not in blk[:500]: continue
    if not re.search(r'\(vias not_allowed\)', blk): continue
    lm = re.search(r'\(layers ((?:"[^"]+"\s*)+)\)', blk) or re.search(r'\(layer "([^"]+)"\)', blk)
    lys = re.findall(r'"([^"]+)"', lm.group(1)) if lm else []
    # The polygon is taken with the s-expression parser, not a regex: the
    # non-greedy `\(polygon.*?\)\s*\)` stops at the first pair of closing
    # brackets, a quadrilateral keepout parsed as three points, and the
    # distance to it came out as several mm instead of the real 0.27.
    polys = _sub(blk, 'polygon')
    if not polys: continue
    pts = [(float(x), float(y)) for x, y in
           re.findall(r'\(xy (-?[\d.]+) (-?[\d.]+)\)', polys[0])]
    if len(pts) > 2:
        keepouts.append((pts, lys))
print(f'via keepout zones: {len(keepouts)}')

def d_pt_poly(x, y, pts):
    """Distance from a point to the outline; 0 if inside."""
    if inside(pts, x, y): return 0.0
    best = 1e9
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]; x2, y2 = pts[(i+1) % n]
        best = min(best, d_seg_pt(x, y, x1, y1, x2, y2))
    return best

def in_keepout(x, y, vsz):
    for pts, lys in keepouts:
        if d_pt_poly(x, y, pts) < vsz/2:
            return True
    return False

nfill = sum(len(v) for v in fills.values())
print(f'parsed: {len(pads)} pads, {len(vias)} vias, {len(segs)} segments, '
      f'{nfill} filled areas')
assert nfill > 0, f'{nfill} filled areas: incomplete parse'

def inside(pts, x, y):
    c = False; n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]; x2, y2 = pts[(i+1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1 + 1e-18):
            c = not c
    return c

def in_plane(net, x, y):
    """Does the point fall into a filled area of its own net (any layer)."""
    for (n, ly), polys in fills.items():
        if n.split('/')[-1] != net.split('/')[-1]: continue
        for pts in polys:
            if inside(pts, x, y): return ly
    return None

def d_pt_rect(px, py, r):
    return math.hypot(max(r[0]-px, 0, px-r[2]), max(r[1]-py, 0, py-r[3]))

def d_seg_pt(x, y, x1, y1, x2, y2):
    dx, dy = x2-x1, y2-y1; L2 = dx*dx + dy*dy
    t = 0 if L2 == 0 else max(0, min(1, ((x-x1)*dx + (y-y1)*dy)/L2))
    return math.hypot(x-(x1+t*dx), y-(y1+t*dy))

# Obstacles are narrowed to the pad's neighbourhood: scanning the whole board
# for each of dozens of targets would take minutes.
def local_obstacles(cx, cy, net, ignore, R=4.0):
    lp = [p for p in pads
          if p[0].split('/')[-1] != net and (p[6], p[7]) not in ignore
          and d_pt_rect(cx, cy, p[2:6]) < R]
    lv = [v for v in vias if v[0].split('/')[-1] != net and math.hypot(v[1]-cx, v[2]-cy) < R]
    ls = [g for g in segs
          if g[0].split('/')[-1] != net and d_seg_pt(cx, cy, *g[3:7]) < R]
    return lp, lv, ls

def free_via(x, y, net, vsz, ignore, obs, vdr=None):
    lp, lv, ls = obs
    worst = 99.0
    dr = vdr if vdr is not None else vsz / 2
    for hx, hy, hr in holes:
        worst = min(worst, math.hypot(hx - x, hy - y) - hr - dr / 2 - CLEAR_HOLE)
    for p in lp:
        worst = min(worst, d_pt_rect(x, y, p[2:6]) - vsz/2 - CLEAR_CU)
    for vn, vx, vy, vs in lv:
        worst = min(worst, math.hypot(vx-x, vy-y) - (vsz+vs)/2 - CLEAR_HOLE)
    for n, ly, w, x1, y1, x2, y2 in ls:
        worst = min(worst, d_seg_pt(x, y, x1, y1, x2, y2) - w/2 - vsz/2 - CLEAR_CU)
    return worst

def free_feed(x1, y1, x2, y2, net, fw, obs):
    lp, lv, ls = obs
    worst = 99.0
    pts = [(x1+(x2-x1)*t/8, y1+(y2-y1)*t/8) for t in range(9)]
    for p in lp:
        if 'F.Cu' not in p[1]: continue
        worst = min(worst, min(d_pt_rect(px, py, p[2:6]) for px, py in pts) - fw/2 - CLEAR_CU)
    for vn, vx, vy, vs in lv:
        worst = min(worst, d_seg_pt(vx, vy, x1, y1, x2, y2) - vs/2 - fw/2 - CLEAR_CU)
    for n, ly, w, sx1, sy1, sx2, sy2 in ls:
        if ly != 'F.Cu': continue
        worst = min(worst, min(d_seg_pt(px, py, sx1, sy1, sx2, sy2) for px, py in pts) - w/2 - fw/2 - CLEAR_CU)
    return worst

# ---------- targets: unconnected pads of plane nets ----------
drc = json.load(open(DRC))
targets = {}
for x in drc.get('unconnected_items', []):
    for it in x.get('items', []):
        dsc = it.get('description', '')
        m = re.match(r'Pad (\S+) \[([^\]]+)\] of (\S+)', dsc)
        if not m: continue
        net = m.group(2).split('/')[-1]
        if net not in PLANE_NETS: continue
        targets[(m.group(3), m.group(1))] = net
print(f'unconnected pads of plane nets: {len(targets)}')
by = collections.Counter(targets.values())
print('  by net:', dict(by))

padmap = {(p[6], p[7]): p for p in pads}
plan, links, escapes, skipped = [], [], [], []
for (ref, pid), net in sorted(targets.items()):
    p = padmap.get((ref, pid))
    if not p:
        skipped.append((ref, pid, net, 'pad not found')); continue
    # is there already a via of the net right next to it?
    near = min((math.hypot(vx-p[8], vy-p[9]) for vn, vx, vy, _ in vias
                if vn.split('/')[-1] == net), default=99)
    # The feed width follows the NARROW side of the pad: the feed runs within
    # its width. A fine-pitch QFP pad is e.g. 0.28 x 1.5 mm at 0.5 mm pitch; a
    # track as wide as the long side (0.4 mm and more) physically cannot pass
    # between pins, and sizing by it found no outward escape at all.
    fw = min(WIDTH.get(PLANE_NET_CLASS.get(net, net), 0.25),
             round(min(p[10], p[11]) * 0.9, 2), 0.4)
    ign = {(ref, pid)}
    obs = local_obstacles(p[8], p[9], net, ign, R=6.0)
    # First try a full-size via nearby; if no pocket is found, a smaller size
    # and a longer feed. At 0.5 mm QFP pitch a 0.8 mm via physically cannot fit
    # between pins, while 0.6/0.3 is a common production minimum for multilayer
    # boards.
    best = None
    for vsz, vdr, R in ((0.8, 0.4, 1.6), (0.8, 0.4, 2.6), (0.6, 0.3, 2.6)):
        n = int(R / 0.1)
        for i in range(-n, n+1):
            for j in range(-n, n+1):
                nx, ny = round(p[8] + i*0.1, 3), round(p[9] + j*0.1, 3)
                r = math.hypot(nx-p[8], ny-p[9])
                if r < max(p[10], p[11])/2 + vsz/2 or r > R: continue
                if in_keepout(nx, ny, vsz): continue
                mv = free_via(nx, ny, net, vsz, ign, obs, vdr)
                if mv < 0.02: continue
                mf = free_feed(p[8], p[9], nx, ny, net, fw, obs)
                if mf < 0.02: continue
                if not in_plane(net, nx, ny): continue
                cand = (round(-min(mv, mf), 3), r, nx, ny, mv, mf, vsz, vdr)
                if best is None or cand < best: best = cand
        if best is not None: break
    if best is None:
        # No via fits: try a feed to a nearby pad of the same net. For a QFP
        # this is the right connection anyway: a supply pin goes to the pad of
        # its decoupling capacitor, which already has a via into the plane.
        # Current through the capacitor is what it is there for.
        near_pads = sorted(
            ((math.hypot(q[8]-p[8], q[9]-p[9]), q) for q in pads
             if q[0].split('/')[-1] == net and (q[6], q[7]) != (ref, pid)
             and 'F.Cu' in q[1] and math.hypot(q[8]-p[8], q[9]-p[9]) < 4.0),
            key=lambda z: z[0])
        hop = None
        for dist, q in near_pads[:12]:
            mf = free_feed(p[8], p[9], q[8], q[9], net, fw, obs)
            if mf >= 0.02:
                hop = (dist, q, mf); break
        if hop is not None:
            dist, q, mf = hop
            links.append((ref, pid, net, p[8], p[9], q[6], q[7], q[8], q[9], fw, mf, dist))
            continue
        # Dog-leg path: first outward from the package (escape), then a via.
        # At 0.5 mm QFP pitch a straight line from the pin runs into the
        # neighbouring pins, so the only way out is the one signals use:
        # perpendicular to the package edge, with the via placed beyond it.
        cx0, cy0 = fpc.get(ref, (p[8], p[9]))
        vx0, vy0 = p[8] - cx0, p[9] - cy0
        L0 = math.hypot(vx0, vy0) or 1.0
        ux, uy = vx0 / L0, vy0 / L0
        esc = None
        for step in [x * 0.1 for x in range(6, 30)]:
            ex, ey = round(p[8] + ux*step, 3), round(p[9] + uy*step, 3)
            if free_feed(p[8], p[9], ex, ey, net, fw, obs) < 0.02: continue
            for vsz, vdr in ((0.6, 0.3), (0.8, 0.4)):
                b2 = None
                n2 = 12
                for i in range(-n2, n2+1):
                    for j in range(-n2, n2+1):
                        nx, ny = round(ex + i*0.1, 3), round(ey + j*0.1, 3)
                        rr = math.hypot(nx-ex, ny-ey)
                        if rr > 1.2: continue
                        if in_keepout(nx, ny, vsz): continue
                        mv = free_via(nx, ny, net, vsz, ign, obs, vdr)
                        if mv < 0.02: continue
                        m2 = free_feed(ex, ey, nx, ny, net, fw, obs)
                        if m2 < 0.02: continue
                        if not in_plane(net, nx, ny): continue
                        c2 = (round(-min(mv, m2), 3), rr, nx, ny, mv, m2, vsz, vdr)
                        if b2 is None or c2 < b2: b2 = c2
                if b2 is not None:
                    esc = (ex, ey, step) + b2
                    break
            if esc: break
        if esc is None:
            skipped.append((ref, pid, net, 'no via, no feed, no outward escape')); continue
        ex, ey, step, _, rr, nx, ny, mv, m2, vsz, vdr = esc
        escapes.append((ref, pid, net, p[8], p[9], ex, ey, nx, ny, vsz, vdr, fw,
                        min(mv, m2), step + rr))
        continue
    _, r, nx, ny, mv, mf, vsz, vdr = best
    plan.append((ref, pid, net, p[8], p[9], nx, ny, vsz, vdr, fw, mv, mf, r))

print(f'\nvia into the plane: {len(plan)} | feed to own net: {len(links)} | '
      f'outward escape: {len(escapes)} | failed: {len(skipped)}')
for ref, pid, net, px, py, nx, ny, vsz, vdr, fw, mv, mf, r in plan:
    print(f'  {ref}.{pid:4s} [{net:5s}] via ({nx:.3f},{ny:.3f}) {vsz}/{vdr}, '
          f'feed {r:.2f} mm x {fw} mm, margin {min(mv,mf):+.3f}')
for ref, pid, net, px, py, qref, qpid, qx, qy, fw, mf, dist in links:
    print(f'  {ref}.{pid:4s} [{net:5s}] feed to {qref}.{qpid}: {dist:.2f} mm x {fw} mm, '
          f'margin {mf:+.3f}')
for ref, pid, net, px, py, ex, ey, nx, ny, vsz, vdr, fw, mg, tot in escapes:
    print(f'  {ref}.{pid:4s} [{net:5s}] outward to ({ex:.2f},{ey:.2f}), via '
          f'({nx:.3f},{ny:.3f}) {vsz}/{vdr}, total {tot:.2f} mm, margin {mg:+.3f}')
for ref, pid, net, why in skipped:
    print(f'  SKIPPED {ref}.{pid} [{net}]: {why}')

if not APPLY or not (plan or links or escapes):
    print(f'\nplan only. To write: python3 fix_plane_pads.py {DRC} --apply')
    sys.exit(0)

open(BRD + '.bakPlanePads', 'w').write(s)
add = []
for ref, pid, net, px, py, nx, ny, vsz, vdr, fw, mv, mf, r in plan:
    add.append(f'\t(via (at {nx:.3f} {ny:.3f}) (size {vsz}) (drill {vdr}) (layers "F.Cu" "B.Cu") '
               f'(tenting (front yes) (back yes) ) (net "{net}") (uuid "{uuid.uuid4()}") )')
    add.append(f'\t(segment (start {px:.3f} {py:.3f}) (end {nx:.3f} {ny:.3f}) (width {fw}) '
               f'(layer "F.Cu") (net "{net}") (uuid "{uuid.uuid4()}") )')
for ref, pid, net, px, py, qref, qpid, qx, qy, fw, mf, dist in links:
    add.append(f'\t(segment (start {px:.3f} {py:.3f}) (end {qx:.3f} {qy:.3f}) (width {fw}) '
               f'(layer "F.Cu") (net "{net}") (uuid "{uuid.uuid4()}") )')
for ref, pid, net, px, py, ex, ey, nx, ny, vsz, vdr, fw, mg, tot in escapes:
    add.append(f'\t(via (at {nx:.3f} {ny:.3f}) (size {vsz}) (drill {vdr}) (layers "F.Cu" "B.Cu") '
               f'(tenting (front yes) (back yes) ) (net "{net}") (uuid "{uuid.uuid4()}") )')
    add.append(f'\t(segment (start {px:.3f} {py:.3f}) (end {ex:.3f} {ey:.3f}) (width {fw}) '
               f'(layer "F.Cu") (net "{net}") (uuid "{uuid.uuid4()}") )')
    add.append(f'\t(segment (start {ex:.3f} {ey:.3f}) (end {nx:.3f} {ny:.3f}) (width {fw}) '
               f'(layer "F.Cu") (net "{net}") (uuid "{uuid.uuid4()}") )')
head = s.rstrip()
assert head.endswith(')')
open(BRD, 'w').write(head[:-1].rstrip() + '\n' + '\n'.join(add) + '\n)\n')
print(f'\nvias placed: {len(plan) + len(escapes)}, feeds to own net: {len(links)}, '
      f'outward escapes: {len(escapes)}')
print('backup .bakPlanePads')
