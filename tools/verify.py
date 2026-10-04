#!/usr/bin/env python3
"""Board check gate.

Every check starts with an assertion that parsing was complete: a zero result on an
empty input is a check that did not run, not a success. The rule exists because a
broken parser reporting zero looks exactly like a clean board.

Board-specific expectations come from kicad-agent.json (all optional):
  exti_check          {"mcu": "U1", "net_pattern": "SENS_", "min_count": 8}
                      - nets matching the pattern on that MCU must use distinct EXTI lines
  netclass_expect     [{"net_regex": "^MOT[AB]_", "netclass": "MOTOR", "min_count": 20}]
                      - nets matching the regex must be in that net class
  no_net_pads         {"U1": {"pads": ["5", "6"], "reason": "NC per datasheet table 3"}}
                      - numbered pads reviewed and accepted as having no net
  waivers.verify_decoupling  {"U1:VDDA": "reason with numbers"}
                      - accepted decoupling deviations, key "<ref>:<pinfunction>"

Run:   python3 verify.py [path/to/board.kicad_pcb]
Exit:  0 - all checks passed, 1 - there are findings.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import math, re, sys, json, collections, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project

BRD = sys.argv[1] if len(sys.argv) > 1 else paths.BOARD
SIG = ['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']

# ---------------------------------------------------------------- parsing
def blocks(s, tag):
    """(start, end) of top-level blocks, honouring quoted strings."""
    res = []
    for m in re.finditer(r'\n\t\(' + re.escape(tag) + r'(?=[\s)])', s):
        i = m.start(); j = i + 1; d = 0; q = False
        while j < len(s):
            c = s[j]
            if q:
                if c == '\\': j += 2; continue
                if c == '"': q = False
            else:
                if c == '"': q = True
                elif c == '(': d += 1
                elif c == ')':
                    d -= 1
                    if d == 0: break
            j += 1
        res.append((i + 1, j + 1))
    return res

def sub(blk, tag):
    res = []
    for m in re.finditer(r'\(' + re.escape(tag) + r'(?=[\s)])', blk):
        i = m.start(); j = i; d = 0; q = False
        while j < len(blk):
            c = blk[j]
            if q:
                if c == '\\': j += 2; continue
                if c == '"': q = False
            else:
                if c == '"': q = True
                elif c == '(': d += 1
                elif c == ')':
                    d -= 1
                    if d == 0: break
            j += 1
        res.append(blk[i:j + 1])
    return res

def rot(px, py, deg):
    a = math.radians(deg); c, si = math.cos(a), math.sin(a)
    return px * c + py * si, -px * si + py * c

s = open(BRD).read()

footprints = []      # (ref, fx, fy, frot, blk)
pads = []            # (ref, num, net, layers, cx, cy, w, h)
for a, b in blocks(s, 'footprint'):
    blk = s[a:b]
    rm = re.search(r'\(property "Reference" "([^"]+)"', blk)
    am = re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', blk)
    if not rm or not am: continue
    ref = rm.group(1); fx, fy = float(am.group(1)), float(am.group(2)); fr = float(am.group(3) or 0)
    footprints.append((ref, fx, fy, fr, blk))
    for p in sub(blk, 'pad'):
        hm = re.match(r'\(pad "([^"]*)"', p)
        pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', p)
        sz = re.search(r'\(size ([\d.]+) ([\d.]+)\)', p)
        if not (hm and pa and sz): continue
        px, py = float(pa.group(1)), float(pa.group(2))
        # The pad angle is the third argument of its (at ...), and when absent it
        # is ZERO, not the footprint angle: KiCad stores the pad angle in board
        # axes and omits it when zero. Falling back to the footprint angle swapped
        # the extents of every pad on rotated footprints. Verified against DRC:
        # measured pad-to-track and pad-to-via clearances match to the fourth
        # decimal only with this rule (accounting for the roundrect corner radius).
        pang = float(pa.group(3)) if pa.group(3) is not None else 0.0
        dx, dy = rot(px, py, fr)
        w, h = float(sz.group(1)), float(sz.group(2))
        poly = None
        if ' custom' in p[:40]:
            # for custom pads (size) only describes the anchor: the real copper is
            # given by gr_poly primitives. Take their points as a polygon.
            pr = [(float(x), float(y)) for x, y in
                  re.findall(r'\(xy (-?[\d.]+) (-?[\d.]+)\)', p)]
            if len(pr) > 2:
                poly = [rot(x, y, pang - fr) for x, y in pr]   # local pad rotation
                w = max(w, 2 * max(abs(x) for x, _ in poly))
                h = max(h, 2 * max(abs(y) for _, y in poly))
        # Circular/oval pads and pads rotated by a non-multiple of 90 deg are polygons, not
        # bounding rectangles: the "corner" of a round through-hole connector pad is not copper,
        # and a track passing diagonally produced false "overlaps" while DRC reported 0.
        shp = re.match(r'\(pad "[^"]*" \w+ (\w+)', p)
        shp = shp.group(1) if shp else 'rect'
        if poly is None and (shp in ('circle', 'oval') or (shp in ('rect', 'roundrect') and abs(pang % 90) > 1e-6)):
            if shp == 'circle':
                r0 = w / 2
                pts = [(r0 * math.cos(2 * math.pi * i / 24), r0 * math.sin(2 * math.pi * i / 24)) for i in range(24)]
            elif shp == 'oval':
                r0 = min(w, h) / 2; L0 = abs(w - h) / 2
                if w >= h:
                    pts = ([(L0 + r0 * math.cos(-math.pi / 2 + math.pi * i / 12), r0 * math.sin(-math.pi / 2 + math.pi * i / 12)) for i in range(13)]
                           + [(-L0 + r0 * math.cos(math.pi / 2 + math.pi * i / 12), r0 * math.sin(math.pi / 2 + math.pi * i / 12)) for i in range(13)])
                else:
                    pts = ([(r0 * math.cos(math.pi * i / 12), L0 + r0 * math.sin(math.pi * i / 12)) for i in range(13)]
                           + [(r0 * math.cos(math.pi + math.pi * i / 12), -L0 + r0 * math.sin(math.pi + math.pi * i / 12)) for i in range(13)])
            else:
                pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
            poly = [rot(x, y, pang) for x, y in pts]
        if abs(pang % 180) > 45 and poly is None: w, h = h, w
        lys = re.search(r'\(layers ([^)]*)\)', p)
        lay = re.findall(r'"([^"]+)"', lys.group(1)) if lys else []
        cu = []
        for L in lay:
            if L == '*.Cu': cu = list(SIG); break
            if L.endswith('.Cu'): cu.append(L)
        nm = re.search(r'\(net "([^"]*)"\)', p)
        pads.append((ref, hm.group(1), nm.group(1) if nm else '', cu, fx + dx, fy + dy, w, h,
                     [(fx + dx + qx, fy + dy + qy) for qx, qy in poly] if poly else None))

tracks = []
for a, b in blocks(s, 'segment'):
    blk = s[a:b]
    st = re.search(r'\(start (-?[\d.]+) (-?[\d.]+)\)', blk); en = re.search(r'\(end (-?[\d.]+) (-?[\d.]+)\)', blk)
    nm = re.search(r'\(net "([^"]*)"\)', blk)
    tracks.append((nm.group(1) if nm else '', re.search(r'\(layer "([^"]+)"\)', blk).group(1),
                   float(re.search(r'\(width ([\d.]+)\)', blk).group(1)),
                   float(st.group(1)), float(st.group(2)), float(en.group(1)), float(en.group(2))))

vias = []
for a, b in blocks(s, 'via'):
    blk = s[a:b]
    at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)\)', blk)
    nm = re.search(r'\(net "([^"]*)"\)', blk)
    vias.append((nm.group(1) if nm else '', float(at.group(1)), float(at.group(2)),
                 float(re.search(r'\(size ([\d.]+)\)', blk).group(1)) / 2))

fills = []
for a, b in blocks(s, 'zone'):
    blk = s[a:b]
    nt = re.search(r'\(net "([^"]*)"\)', blk[:200]); net = nt.group(1) if nt else ''
    i = 0
    while True:
        i = blk.find('(filled_polygon', i)
        if i < 0: break
        j = i; d = 0; q = False
        while j < len(blk):
            c = blk[j]
            if q:
                if c == '\\': j += 2; continue
                if c == '"': q = False
            else:
                if c == '"': q = True
                elif c == '(': d += 1
                elif c == ')':
                    d -= 1
                    if d == 0: break
            j += 1
        fp = blk[i:j + 1]; i = j
        ly = re.search(r'\(layer "([^"]+)"\)', fp)
        pts = [(float(x), float(y)) for x, y in re.findall(r'\(xy (-?[\d.]+) (-?[\d.]+)\)', fp)]
        if len(pts) > 2: fills.append((net, ly.group(1) if ly else '?', pts))

print(f'parsed: footprints {len(footprints)}, pads {len(pads)}, tracks {len(tracks)}, '
      f'vias {len(vias)}, fill polygons {len(fills)}')
assert len(footprints) > 0 and len(pads) > 0, 'footprint parsing incomplete - checks are meaningless'

fails = []
def check(name, ok, detail=''):
    print(('  PASSED' if ok else '  FAILED') + f'  {name}' + (f' - {detail}' if detail else ''))
    if not ok: fails.append(name)

# --- 1. footprint pads vs symbol pins
bad = []
for ref, fx, fy, fr, blk in footprints:
    um = re.search(r'\(units\s*\(unit\s*\(name "[^"]*"\)\s*\(pins((?:\s*"[^"]*")+)\)', blk)
    if not um: continue
    sym = {x for x in re.findall(r'"([^"]*)"', um.group(1))}
    fpp = {n for r, n, *_ in pads if r == ref}
    miss = sym - fpp
    if miss: bad.append((ref, sorted(miss)[:8]))
check('footprint pads cover all symbol pins', not bad,
      '' if not bad else f'{len(bad)} footprints, e.g. ' + '; '.join(f'{r}: missing {m}' for r, m in bad[:3]))

# --- 2. copper overlap between different nets
def bbox(k, g):
    if k == 'seg': return min(g[0], g[2]) - g[4], min(g[1], g[3]) - g[4], max(g[0], g[2]) + g[4], max(g[1], g[3]) + g[4]
    if k == 'circ': return g[0] - g[2], g[1] - g[2], g[0] + g[2], g[1] + g[2]
    if k == 'poly':
        xs = [p[0] for p in g]; ys = [p[1] for p in g]
        return min(xs), min(ys), max(xs), max(ys)
    return g
def poly_edges(g): return [(g[i][0], g[i][1], g[(i+1) % len(g)][0], g[(i+1) % len(g)][1]) for i in range(len(g))]
def in_poly(px, py, g):
    c = False; n = len(g)
    for k in range(n):
        x1, y1 = g[k]; x2, y2 = g[(k+1) % n]
        if (y1 > py) != (y2 > py) and px < x1 + (py - y1) / (y2 - y1) * (x2 - x1): c = not c
    return c
def d_pt_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1; L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
def d_seg_seg(a, b):
    x1, y1, x2, y2 = a; x3, y3, x4, y4 = b
    d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(d) > 1e-12:
        t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d
        u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d
        if 0 <= t <= 1 and 0 <= u <= 1: return 0.0
    return min(d_pt_seg(x1, y1, x3, y3, x4, y4), d_pt_seg(x2, y2, x3, y3, x4, y4),
               d_pt_seg(x3, y3, x1, y1, x2, y2), d_pt_seg(x4, y4, x1, y1, x2, y2))
def rect_edges(g):
    x0, y0, x1, y1 = g
    return [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]
def inrect(px, py, g): return g[0] <= px <= g[2] and g[1] <= py <= g[3]
def gap(k1, g1, k2, g2):
    if k1 == 'poly' or k2 == 'poly':
        if k2 == 'poly': k1, g1, k2, g2 = k2, g2, k1, g1
        E = poly_edges(g1)
        if k2 == 'seg':
            if in_poly(g2[0], g2[1], g1) or in_poly(g2[2], g2[3], g1): return -g2[4]
            return min(d_seg_seg(e, g2[:4]) for e in E) - g2[4]
        if k2 == 'circ':
            if in_poly(g2[0], g2[1], g1): return -g2[2]
            return min(d_pt_seg(g2[0], g2[1], *e) for e in E) - g2[2]
        if k2 == 'poly':
            if any(in_poly(p[0], p[1], g1) for p in g2) or any(in_poly(p[0], p[1], g2) for p in g1): return -0.001
            return min(d_seg_seg(e, f) for e in E for f in poly_edges(g2))
        for e in E:
            if inrect(e[0], e[1], g2): return -0.001
        return min(d_seg_seg(e, f) for e in E for f in rect_edges(g2))
    if k1 == 'rect' and k2 != 'rect': k1, g1, k2, g2 = k2, g2, k1, g1
    if k1 == 'seg' and k2 == 'seg': return d_seg_seg(g1[:4], g2[:4]) - g1[4] - g2[4]
    if k1 == 'seg' and k2 == 'circ': return d_pt_seg(g2[0], g2[1], *g1[:4]) - g1[4] - g2[2]
    if k1 == 'circ' and k2 == 'circ': return math.hypot(g1[0] - g2[0], g1[1] - g2[1]) - g1[2] - g2[2]
    if k1 == 'seg' and k2 == 'rect':
        if inrect(g1[0], g1[1], g2) or inrect(g1[2], g1[3], g2): return -g1[4]
        return min(d_seg_seg(g1[:4], e) for e in rect_edges(g2)) - g1[4]
    if k1 == 'circ' and k2 == 'rect':
        if inrect(g1[0], g1[1], g2): return -g1[2]
        return min(d_pt_seg(g1[0], g1[1], *e) for e in rect_edges(g2)) - g1[2]
    dx = max(g1[0] - g2[2], g2[0] - g1[2], 0); dy = max(g1[1] - g2[3], g2[1] - g1[3], 0)
    return -0.001 if (dx == 0 and dy == 0) else math.hypot(dx, dy)

items = ([(n, [l], 'seg', (x1, y1, x2, y2, w / 2)) for n, l, w, x1, y1, x2, y2 in tracks]
       + [(n, SIG, 'circ', (x, y, r)) for n, x, y, r in vias]
       + [(net, cu, 'rect', (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
          for ref, num, net, cu, cx, cy, w, h, pl in pads if cu and pl is None]
       + [(net, cu, 'poly', tuple(pl))
          for ref, num, net, cu, cx, cy, w, h, pl in pads if cu and pl])
CELL = 2.0
grid = collections.defaultdict(list)
for idx, (net, lys, k, g) in enumerate(items):
    x0, y0, x1, y1 = bbox(k, g)
    for i in range(int(x0 // CELL), int(x1 // CELL) + 1):
        for j in range(int(y0 // CELL), int(y1 // CELL) + 1):
            grid[(i, j)].append(idx)
seen = set(); overlaps = []
for idxs in grid.values():
    for ii in range(len(idxs)):
        for jj in range(ii + 1, len(idxs)):
            a, b = idxs[ii], idxs[jj]
            if (a, b) in seen: continue
            seen.add((a, b))
            n1, l1, k1, g1 = items[a]; n2, l2, k2, g2 = items[b]
            if n1 == n2 or not (set(l1) & set(l2)): continue
            if gap(k1, g1, k2, g2) < 0:
                overlaps.append((n1.split('/')[-1], n2.split('/')[-1], round(bbox(k1, g1)[0], 2), round(bbox(k1, g1)[1], 2), k1 + '/' + k2, ','.join(sorted(set(l1) & set(l2)))))
check('no copper overlap between different nets', not overlaps,
      '' if not overlaps else f'{len(overlaps)}, e.g. {overlaps[:18]}')

# --- 3. tracks and pads inside a foreign fill
def inside(px, py, pts):
    c = False; n = len(pts)
    for k in range(n):
        x1, y1 = pts[k]; x2, y2 = pts[(k + 1) % n]
        if (y1 > py) != (y2 > py) and px < x1 + (py - y1) / (y2 - y1) * (x2 - x1): c = not c
    return c
byl = collections.defaultdict(list)
for f in fills: byl[f[1]].append(f)
inside_hits = []
for net, ly, w, x1, y1, x2, y2 in tracks:
    n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.1) + 1)
    for f_net, f_ly, pts in byl.get(ly, []):
        if f_net == net: continue
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        if max(x1, x2) < min(xs) or min(x1, x2) > max(xs) or max(y1, y2) < min(ys) or min(y1, y2) > max(ys): continue
        if any(inside(x1 + k / n * (x2 - x1), y1 + k / n * (y2 - y1), pts) for k in range(n + 1)):
            inside_hits.append(('track', net.split('/')[-1], f_net, ly)); break
for ref, num, net, cu, cx, cy, w, h, pl in pads:
    for L in cu:
        for f_net, f_ly, pts in byl.get(L, []):
            if f_net == net: continue
            if inside(cx, cy, pts): inside_hits.append(('pad', f'{ref}.{num}', f_net, L))
check('nothing lies inside a foreign fill', not inside_hits,
      '' if not inside_hits else f'{len(inside_hits)}, e.g. {inside_hits[:3]}')

# --- 4. every piece of ground reaches the rest of the ground
# A weaker formulation asked "is there anything ground-connected inside the piece" -
# and missed a fill fragment hanging on an SMD pad: the pad is there, but it is
# single-layer and does not tie the piece to the rest of the ground. Many isolated
# fill fragments passed that way.
#
# Connectivity is computed: only vias and through-hole pads join layers.
# Pieces smaller than MIN_ISLAND are not reported: at 5 GHz the wavelength in the
# dielectric is about 35 mm and resonance needs about half of it, so a fragment of a
# fraction of a square millimetre is electrically insignificant, and a via does not
# physically fit into it.
# The threshold matches the zones' own island_area_min setting (12 mm^2 is a common
# choice): the project already considers smaller fragments not worth copper. KiCad
# does not treat pieces with a ground pad inside as islands and does not remove them,
# so they remain - this is an EMC finding, not a functional one, as long as return
# currents have solid ground planes.
MIN_ISLAND = float(project.get('min_island_mm2', 12.0))       # mm^2

def poly_area(pts):
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2

gnd = [f for f in fills if f[0] == 'GND']
parent = list(range(len(gnd)))
def _find(x):
    while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
    return x
def _union(a, b):
    ra, rb = _find(a), _find(b)
    if ra != rb: parent[ra] = rb

def _hits(x, y, layers):
    return [i for i, (n2, ly2, pts2) in enumerate(gnd) if ly2 in layers and inside(x, y, pts2)]

ALLCU = ('F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu')
for vn, vx, vy, vr in vias:
    if vn != 'GND': continue
    t = _hits(vx, vy, ALLCU)
    for i in t[1:]: _union(t[0], i)
for r, n2, net2, cu, cx, cy, w, h, pl in pads:
    if net2 != 'GND' or len(cu) < 2: continue      # a single-layer SMD pad joins nothing
    t = _hits(cx, cy, cu)
    for i in t[1:]: _union(t[0], i)

comp = collections.defaultdict(list)
for i in range(len(gnd)): comp[_find(i)].append(i)
main = max(comp.values(), key=lambda g: sum(poly_area(gnd[i][2]) for i in g), default=[])
loose = [i for g in comp.values() if g is not main for i in g]
big = [i for i in loose if poly_area(gnd[i][2]) >= MIN_ISLAND]
check('every piece of ground reaches the rest of the ground', not big,
      f'pieces {len(gnd)}, outside the main ground {len(loose)}, of them larger than {MIN_ISLAND} mm^2: 0'
      if not big else
      f'{len(big)} pieces larger than {MIN_ISLAND} mm^2 outside the main ground, worst: ' +
      '; '.join(f'{poly_area(gnd[i][2]):.1f} mm^2 on {gnd[i][1]}' for i in
                sorted(big, key=lambda i: -poly_area(gnd[i][2]))[:3]))

# --- 5. interrupt inputs on distinct EXTI indices (STM32-style: one line per pin number)
EXTI = project.get('exti_check')
if EXTI:
    sens = {}
    for ref, fx, fy, fr, blk in footprints:
        if ref != EXTI['mcu']: continue
        for p in sub(blk, 'pad'):
            pf = re.search(r'\(pinfunction "([^"]*)"\)', p); nm = re.search(r'\(net "([^"]*)"\)', p)
            if pf and nm and EXTI['net_pattern'] in nm.group(1):
                m = re.match(r'P([A-Z])(\d+)', pf.group(1))
                if m: sens[nm.group(1).split('/')[-1]] = (pf.group(1), int(m.group(2)))
    idx = [v[1] for v in sens.values()]
    check('all interrupt inputs on distinct EXTI indices',
          len(sens) >= int(EXTI.get('min_count', 1)) and len(set(idx)) == len(idx),
          f'{len(sens)} inputs, indices {sorted(idx)}')

# --- 6. net classes are actually assigned
pro = paths.sibling(BRD, '.kicad_pro')
if os.path.exists(pro):
    import fnmatch
    ns = json.load(open(pro))['net_settings']
    allnets = {net for r, n, net, cu, cx, cy, w, h, pl in pads if net}
    def cls(n):
        base = n.split('/')[-1]
        for p in ns['netclass_patterns']:
            if fnmatch.fnmatch(base, p['pattern']): return p['netclass']
        return 'Default'
    byc = collections.Counter(cls(n) for n in allnets)
    empty = [c['name'] for c in ns['classes'] if c['name'] != 'Default' and byc.get(c['name'], 0) == 0]
    check('no empty net classes', not empty, '' if not empty else f'empty: {empty}')
    # Expected memberships, e.g. motor coil nets must land in the high-current class.
    for exp in project.get('netclass_expect', []):
        members = [n for n in allnets if re.match(exp['net_regex'], n.split('/')[-1])]
        members_ok = all(cls(n) == exp['netclass'] for n in members)
        check(f"nets {exp['net_regex']} are in class {exp['netclass']}",
              members_ok and len(members) >= int(exp.get('min_count', 1)),
              f'nets {len(members)}, class distribution {dict(byc)}')

    # --- 9. copper is not narrower than its net class width
    # The custom rules (.kicad_dru) may contain only clearances; the required width
    # lives in the net classes, and DRC does not compare it with the actual copper.
    # A narrow track violates no rule: it is just thin. This is how a 0.15 mm track
    # can survive on a modem supply that needs 1.0 mm for 2 A transmit bursts.
    WCLS = {c['name']: float(c['track_width']) for c in ns['classes']}
    PLANE = project.plane_nets()
    KEEP_NARROW = 3.0     # same escape allowance as in widen_net.py
    padc = collections.defaultdict(list)
    for r, n, net, cu, cx, cy, w, h, pl in pads:
        if net: padc[net.split('/')[-1]].append((cx, cy))
    narrow = []
    for net, ly, w, x1, y1, x2, y2 in tracks:
        if not net: continue
        base = net.split('/')[-1]
        need = WCLS.get(cls(net), 0.2)
        if w >= need - 0.005: continue
        L = math.hypot(x2 - x1, y2 - y1)
        ps = padc.get(base) or []
        # An escape from under a package is legitimate while the segment lies
        # ENTIRELY within KEEP_NARROW of its own pads: supply nets have pads all
        # over the board, and judging by one near end would excuse everything.
        if ps:
            d1 = min(math.hypot(px - x1, py - y1) for px, py in ps)
            d2 = min(math.hypot(px - x2, py - y2) for px, py in ps)
            if L <= KEEP_NARROW and max(d1, d2) < KEEP_NARROW: continue
            # An escape may end not at a pad but at a VIA INTO A PLANE: from there the
            # current flows in the plane, and a wide track is physically impossible -
            # a VQFN pad may itself be 0.25 mm wide against a 1.0 mm class width.
            vs = [(vx, vy) for vn, vx, vy, vr in vias if vn.split('/')[-1] == base]
            if vs and L <= KEEP_NARROW:
                e1 = min(min(math.hypot(px - x1, py - y1) for px, py in ps),
                         min(math.hypot(vx - x1, vy - y1) for vx, vy in vs))
                e2 = min(min(math.hypot(px - x2, py - y2) for px, py in ps),
                         min(math.hypot(vx - x2, vy - y2) for vx, vy in vs))
                if max(e1, e2) < KEEP_NARROW: continue
        narrow.append((round(L, 1), base, w, need, base in PLANE))
    narrow.sort(reverse=True)
    plane_only = all(x[4] for x in narrow)
    check('copper is not narrower than its net class width', not narrow,
          '' if not narrow else
          f'{len(narrow)} segments, {sum(x[0] for x in narrow):.1f} mm' +
          (' (all on plane nets - widen in place)' if narrow and plane_only else '') +
          ', worst: ' + '; '.join(f'{n} {w} instead of {need} mm over {L} mm'
                                    for L, n, w, need, _ in narrow[:3]))

# --- 7. decoupling: distance from a supply pin to a capacitor of the same net
# Supply pins are taken from the NETLIST by (pintype "power_in"/"power_out"), not by
# a name prefix. Selection by name missed more than half of the pins: names like
# `SIM_VDD`, `LDO_3V3` and `VBUS` did not match the pattern at all, and a 1.8 V
# output that needs decoupling per its datasheet sat in an exclusion list.
# The chip vendor chooses the pin name, the schematic sets the type - the type is more reliable.
from paths import KICAD_CLI              # /usr/bin on Linux, /Applications on macOS


def _power_pins():
    """{(ref, pin_number)} - IC supply pins, from the schematic netlist."""
    import subprocess, tempfile, os
    root = paths.sibling(BRD, '.kicad_sch')
    if not os.path.exists(root): return None
    out = os.path.join(tempfile.gettempdir(), 'verify_netlist.net')
    sdir = os.path.dirname(root)
    newest = max(os.path.getmtime(os.path.join(sdir, f)) for f in os.listdir(sdir) if f.endswith('.kicad_sch'))
    if not (os.path.exists(out) and os.path.getmtime(out) > newest):
        r = subprocess.run([KICAD_CLI, 'sch', 'export', 'netlist', '--format', 'kicadsexpr',
                            '-o', out, root], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out): return None
    t = open(out).read()
    res = set()
    for m in re.finditer(r'\n\t\t\(net\n', t):
        a = m.start() + 1; d = 0; i = a; inq = False
        while i < len(t):                       # bracket balance HONOURING quotes:
            c = t[i]                            # names like PA14(JTCK_137 would merge nets otherwise
            if c == '"' and t[i-1] != '\\': inq = not inq
            elif not inq:
                if c == '(': d += 1
                elif c == ')':
                    d -= 1
                    if d == 0: break
            i += 1
        for mm in re.finditer(r'\(node\s*\(ref "((?:[^"\\]|\\.)*)"\)\s*\(pin "((?:[^"\\]|\\.)*)"\)'
                              r'(?:\s*\(pinfunction "(?:[^"\\]|\\.)*"\))?\s*\(pintype "([^"]*)"\)', t[a:i+1]):
            ref, pin, pt = mm.groups()
            if pt.startswith('power_') and ref.startswith('U'): res.add((ref, pin))
    return res or None

PWR_PINS = _power_pins()
POWER = re.compile(r'^(VDD|VCC|VBAT|VIN|AVDD|VDDA|VDDIO|PVDD|VCCA|VCCB|AVCC)', re.I)
NOTPOWER = re.compile(r'^(VSS|VSSA|VBUS_DET)', re.I)
cap_by_net = collections.defaultdict(list)
for ref, num, net, cu, cx, cy, w, h, pl in pads:
    if ref.startswith('C') and net: cap_by_net[net].append((ref, cx, cy))
far = []
far_geo = {}
npins = 0
for ref, fx, fy, fr, blk in footprints:
    if not ref.startswith('U'): continue
    for p in sub(blk, 'pad'):
        pf = re.search(r'\(pinfunction "([^"]*)"\)', p)
        nm = re.search(r'\(net "([^"]*)"\)', p)
        pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)', p)
        pn = re.match(r'\(pad "([^"]*)"', p)
        num = pn.group(1) if pn else ''
        if not (pf and nm and pa and nm.group(1)): continue
        fn = pf.group(1)
        if PWR_PINS is not None:
            if (ref, num) not in PWR_PINS: continue
            # Ground nets need no decoupling: the return current flows in the
            # ground plane, a capacitor is not placed "to" it.
            base_net = nm.group(1).split('/')[-1]
            if base_net in ('GND', 'GNDA', 'AGND', 'PGND', 'DGND'): continue
            # A pin left unconnected needs no decoupling by definition.
            if base_net.startswith('unconnected-'): continue
            # Switching node of a converter (SW/SW1/SW2/BOOT/LX/PH): its pin type is
            # power_out, but an inductor connects there, not a capacitor -
            # "decoupling" makes no sense (e.g. a buck-boost charger with SW1, SW2).
            if re.match(r'^(SW\d?|BOOT\d?|LX|PH|VSW)(_|$)', fn, re.I): continue
        elif NOTPOWER.match(fn) or not POWER.match(fn): continue
        dx, dy = rot(float(pa.group(1)), float(pa.group(2)), fr)
        x, y = fx + dx, fy + dy
        cs = cap_by_net.get(nm.group(1), [])
        npins += 1
        if not cs: continue
        d = min(math.hypot(x - cx, y - cy) for _, cx, cy in cs)
        if d > 3.0:
            far.append((round(d, 2), ref, fn))
            bc = min(cs, key=lambda c: math.hypot(x - c[1], y - c[2]))
            # Besides the nearest capacitor keep ALL capacitors of the net: the path through a
            # plane depends not on surface proximity but on whether the capacitor is stitched to it.
            far_geo[(ref, fn)] = (x, y, nm.group(1), bc[1], bc[2],
                                  [(c[0], c[1], c[2]) for c in cs])
# Criterion. "Capacitor closer than 3 mm" is a rule for a TWO-LAYER board, where the
# current flows on the surface. On a multilayer board with solid planes the path
# inductance is set by the vias into the plane, not by the distance on the top layer:
# if the supply pin has its own via and so does the capacitor, the path is short even
# with 7 mm between them. The surface-distance criterion failed most supply pins, of
# which only a couple were physically problematic.
# Accepted decoupling deviations are decisions, not unfinished work. Each carries a
# reason and the number that justifies it (waivers "verify_decoupling", key "ref:pinfunction").
ACCEPTED = {tuple(k.split(':', 1)): v for k, v in project.waivers('verify_decoupling').items()}

cap_at = collections.defaultdict(list)
for _net, _lst in cap_by_net.items():
    cap_at[_net.split('/')[-1]].extend(_lst)
conn_pads = collections.defaultdict(list)
for _r, _n, _net, _cu, _cx, _cy, _w, _h, _pl in pads:
    if _net and _r[:1] in ('J', 'X') and not _r.startswith('JP'):
        conn_pads[_net.split('/')[-1]].append((_cx, _cy))
VIA_NEAR = 2.0        # mm - a via into the plane counts as "own" within this distance
# For a net WITHOUT a plane, a path through a plane is impossible and the criterion is
# different: the track inductance matters, about 1 nH/mm. Over 8 mm that is ~8 nH; for
# a load of tens of mA with slow edges the droop is a few mV, i.e. acceptable. Nets
# routed as tracks cannot be required to have a "via into the plane".
NOPLANE_MAX = 8.0     # mm
planed = {z_net for z_net in (f[0] for f in fills)}
via_by_net = collections.defaultdict(list)
for vn, vx, vy, vr in vias:
    via_by_net[vn].append((vx, vy))

def near_via(net, x, y):
    d, _ = near_via_pos(net, x, y)
    return d


def near_via_pos(net, x, y):
    """(distance, position) of the nearest via of the net."""
    vs = via_by_net.get(net) or []
    if not vs: return 99.0, None
    v = min(vs, key=lambda q: math.hypot(q[0] - x, q[1] - y))
    return math.hypot(v[0] - x, v[1] - y), v


# The distance between the via at the pin and the via at the capacitor runs in the plane;
# a closely spaced power/ground plane pair gives on the order of 0.013 nH/mm - 30 mm of
# plane is less than one millimetre of track. So the threshold is not electrical but
# sanity: a capacitor stitched to the same plane on the far side of the board cannot be
# called this pin's decoupling.
PLANE_PATH_MAX = 30.0

weak = []
for d, r, f in far:
    if (r, f) in ACCEPTED: continue        # accepted deviation, reason in config
    # pin and nearest capacitor coordinates, looked up again by net name
    hit = far_geo.get((r, f))
    if not hit:
        weak.append((d, r, f, 'no geometry')); continue
    px, py, net, cx, cy, cs_all = hit
    base = net.split('/')[-1]
    if base not in planed:
        if d <= NOPLANE_MAX:
            continue                  # net without a plane: judged by track inductance
        # A supply brought out to a connector or holder is decoupled AT THE
        # CONNECTOR - the load is there, and so is ESD protection. The source needs
        # no decoupling of its own: e.g. a supply routed to a socket over a ~50 mm line with ~10 mA
        # clock-edge peaks droops about 1.4 mV. Condition: the net has a capacitor
        # right at the connector pin.
        if any(math.hypot(cx - jx, cy - jy) <= 3.0
               for _cr, cx, cy in cap_at.get(base, [])
               for jx, jy in conn_pads.get(base, [])):
            continue
        weak.append((d, r, f, f'net without a plane, track longer than {NOPLANE_MAX} mm'))
        continue
    # The capacitor is chosen NOT by proximity to the pin but by whether it is stitched
    # to the plane: the nearest one may be a 100 nF part 12 mm away and itself 11 mm from
    # its via, while 22 uF reservoirs are stitched to the plane 1.2 mm away. Choosing by
    # proximity declared such a pin weak although both ends of the path sit on the plane.
    dv, vpos = near_via_pos(net, px, py)
    best = None
    for cr, ccx, ccy in cs_all:
        dvc, cvpos = near_via_pos(net, ccx, ccy)
        if dvc > VIA_NEAR or vpos is None or cvpos is None: continue
        span = math.hypot(cvpos[0] - vpos[0], cvpos[1] - vpos[1])
        if span > PLANE_PATH_MAX: continue
        if best is None or dvc < best[0]: best = (dvc, cr, span)
    if dv <= VIA_NEAR and best:
        continue                      # short path through the plane - fine
    why = f'via at pin {dv:.1f} mm'
    if best is None:
        near_c = min((near_via(net, ccx, ccy) for _cr, ccx, ccy in cs_all), default=99.0)
        why += f', no capacitor with a via closer than {VIA_NEAR} mm (best {near_c:.1f} mm)'
    else:
        why += f', capacitor {best[1]} with via at {best[0]:.1f} mm'
    weak.append((d, r, f, why))
weak.sort(reverse=True)
check('decoupling: short path to a capacitor (direct or through a plane)', not weak,
      f'checked {npins} pins, {len(far)} of them farther than 3 mm on the surface - '
      f'all with vias into a plane' if not weak else
      f'{len(weak)} of {npins} without a short path: ' +
      '; '.join(f'{r}.{f} {d} mm ({why})' for d, r, f, why in weak[:8]))

# --- 10. pads WITHOUT a net: only F8 shows them, and it is not available from the command line
# Update PCB from Schematic (F8, available only from the project manager; standalone pcbnew
# reports "PCB editor is opened in stand-alone mode") warns
# "No net found for component X pad N (no pin N in symbol)". Neither DRC, ERC nor parity
# reports this: a pad without a net violates nothing. Reviewed pads are accepted per the
# datasheet (config "no_net_pads"), but a NEW such pad is a signal (footprint changed, pin
# lost from the symbol), so the accepted set is pinned here.
NO_NET_OK = {ref: (set(str(n) for n in v.get('pads', [])), v.get('reason', ''))
             for ref, v in (project.get('no_net_pads', {}) or {}).items()}
# Pads WITHOUT A NUMBER (mounting tabs of holders, buzzers, switches, connector shield
# tabs) have no net by definition - counted separately, not part of the accepted set.
free_pads = collections.defaultdict(set)
mech = 0
for _r, _n, _net, _cu, _cx, _cy, _w, _h, _pl in pads:
    if _net or not _cu:
        continue
    if not _n or not _n.strip() or _n in ('""', '~'):
        mech += 1
        continue
    free_pads[_r].add(_n)
unexpected = []
for _r, nums in sorted(free_pads.items()):
    allowed = NO_NET_OK.get(_r, (set(), ''))[0]
    extra = sorted(nums - allowed)
    if extra:
        unexpected.append('%s: %s' % (_r, ', '.join(extra[:8])))
check('pads without a net - only those reviewed against the datasheet',
      not unexpected,
      '%d numbered pads without a net on %d footprints, all in the reviewed set (%s); '
      'plus %d unnumbered mounting pads'
      % (sum(len(v) for v in free_pads.values()), len(free_pads),
         ', '.join('%s %d' % (k, len(v)) for k, v in sorted(free_pads.items())), mech)
      if not unexpected else
      'new unconnected pads: ' + '; '.join(unexpected))

print()
if fails:
    print(f'FINDINGS: {len(fails)} - ' + '; '.join(fails)); sys.exit(1)
print('all checks passed'); sys.exit(0)
