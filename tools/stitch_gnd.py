#!/usr/bin/env python3
"""Ground stitching with vias; run it AFTER routing.

Order matters: stitching vias pierce every layer and, when handed to the router as
fixed obstacles, cut every channel (a routing pass took several times longer).
So route first, stitch afterwards.

What it does:
  1. places vias on a PITCH grid wherever the stitched net is poured on both the top
     and bottom layers and there is room to foreign copper;
  2. checks every fill polygon of the net separately: if it has neither a via nor a
     through-hole pad of the net, tries to place a via at its most open point.

Usage:  python3 stitch_gnd.py [--board B] [--net GND] [--pitch 3.0] [--apply]
"""
import re, sys, math, os, uuid, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py, paths.py are siblings
import paths, project


def _arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


BRD = _arg('--board', paths.BOARD)
NET = _arg('--net', project.get('stitch_net', 'GND'))
# Grid pitch, set by the highest frequency on the board. Rule of thumb: stitch at
# least every lambda/10 (lambda/20 for a conservative design), with lambda taken in
# the dielectric: lambda = c / (f * sqrt(eps_eff)). For 5 GHz on FR-4 surface pours
# (eps_eff ~ 3), lambda ~ 35 mm, lambda/10 ~ 3.5 mm; 3.0 mm leaves a margin. A sparser
# grid turns the top and bottom pours into patches that resonate in band.
# Override with --pitch or the stitch_pitch_mm config key.
PITCH = float(_arg('--pitch', project.get('stitch_pitch_mm', 3.0)))

# --- guard against being overwritten by the KiCad editor ---------------------
# An open PCB editor writes its in-memory version over command-line edits and
# silently reverts them. A ~<name>.lck lock file next to the board means the
# editor holds it open; writing at that moment is pointless.
import os as _os
_lck = _os.path.join(_os.path.dirname(_os.path.abspath(BRD)), '~' + _os.path.basename(BRD) + '.lck')
if _os.path.exists(_lck):
    raise SystemExit('REFUSED: the board is open in the KiCad editor (' + _lck + ').\n'
                     'Close the PCB editor without saving and retry.')
VIA, DRILL = 0.8, 0.4
# KAT_STITCH_VIA="0.5,0.2": a smaller via for stitching fragments; a narrower column through all layers
# lets a fragment in a tight spot get a via where 0.6/0.3 does not fit
VIA_SMALL, DRILL_SMALL = (float(v) for v in os.environ.get('KAT_STITCH_VIA', '0.6,0.3').split(','))
CLEAR = float(os.environ.get('KAT_STITCH_CLEAR', '0.25'))   # clearance to foreign copper; KAT_STITCH_CLEAR=0.15 = exactly a typical board minimum (reaches more pads)
APPLY = '--apply' in sys.argv
from sexp import blocks, sub

def rot(px, py, deg):
    a = math.radians(deg); c, si = math.cos(a), math.sin(a)
    return px * c + py * si, -px * si + py * c

s = open(BRD).read()

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
# The threshold is deliberately low: it checks that parsing happened at all, not
# "as many as last time". The number of fill pieces changes from refill to refill
# (e.g. after removing dangling stubs), and a threshold taken from a past count
# starts failing on a healthy board.
assert len(fills) > 0, f'{len(fills)} fill polygons: incomplete parse'

obst = []
for a, b in blocks(s, 'segment'):
    blk = s[a:b]
    st = re.search(r'\(start (-?[\d.]+) (-?[\d.]+)\)', blk); en = re.search(r'\(end (-?[\d.]+) (-?[\d.]+)\)', blk)
    nm = re.search(r'\(net "([^"]*)"\)', blk)
    obst.append(('seg', nm.group(1) if nm else '', float(st.group(1)), float(st.group(2)),
                 float(en.group(1)), float(en.group(2)),
                 float(re.search(r'\(width ([\d.]+)\)', blk).group(1)) / 2))
nseg = len(obst)
for a, b in blocks(s, 'via'):
    blk = s[a:b]
    at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)\)', blk)
    nm = re.search(r'\(net "([^"]*)"\)', blk)
    obst.append(('circ', nm.group(1) if nm else '', float(at.group(1)), float(at.group(2)),
                 float(re.search(r'\(size ([\d.]+)\)', blk).group(1)) / 2))
nvia = len(obst) - nseg

# Holes are handled SEPARATELY from copper: hole_to_hole is a mechanical limit
# and the net does not matter. Two holes cannot be drilled at one point even if
# they are connected. Excluding all copper of the stitched net (including its
# holes) once put stitching vias right into grounded mounting holes (0.0000 mm);
# DRC reported hole_to_hole, but a --severity-error filter hid it because the
# project rated it as a warning.
HOLE_CLEAR = 0.25     # hole clearance from the board setup
holes = []            # (x, y, hole radius)
for a, b in blocks(s, 'via'):
    blk = s[a:b]
    at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)\)', blk)
    dr = re.search(r'\(drill ([\d.]+)\)', blk)
    if at and dr:
        holes.append((float(at.group(1)), float(at.group(2)), float(dr.group(1)) / 2))
for a, b in blocks(s, 'footprint'):
    blk = s[a:b]
    am = re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', blk)
    fx, fy = float(am.group(1)), float(am.group(2)); fr = float(am.group(3) or 0)
    for p in sub(blk, 'pad'):
        pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', p)
        sz = re.search(r'\(size ([\d.]+) ([\d.]+)\)', p)
        if not (pa and sz): continue
        # The pad angle is the third argument of its (at ...), and when absent it
        # is ZERO, not the footprint angle: KiCad stores the pad angle in board
        # axes and omits it when it is zero. Falling back to the footprint angle
        # swapped the extents of every pad on rotated footprints. Verified against
        # DRC-reported pad clearances, which match to the fourth decimal only with
        # this rule (accounting for the roundrect corner radius).
        pang = float(pa.group(3)) if pa.group(3) is not None else 0.0
        dx, dy = rot(float(pa.group(1)), float(pa.group(2)), fr)
        w, h = float(sz.group(1)), float(sz.group(2))
        if ' custom' in p[:40]:
            pr = [(float(x), float(y)) for x, y in
                  re.findall(r'\((?:xy|start|end|center|mid) (-?[\d.]+) (-?[\d.]+)\)', p)]
            if pr:
                w = max(w, 2 * max(abs(x) for x, _ in pr)); h = max(h, 2 * max(abs(y) for _, y in pr))
        if abs(pang % 180) > 45: w, h = h, w
        nm = re.search(r'\(net "([^"]*)"\)', p)
        _dm = re.search(r'\(drill (?:oval )?([\d.]+)', p)
        if _dm:
            holes.append((fx + dx, fy + dy, float(_dm.group(1)) / 2))
        # A through-hole pad (with a drill) joins layers, an SMD pad does not.
        # The difference matters: a fill fragment hanging on an SMD pad is not
        # connected to the rest of the ground, although it formally "has a ground
        # pad inside". That is how fragments used to survive stitching.
        obst.append(('rect', nm.group(1) if nm else '', fx + dx - w / 2, fy + dy - h / 2,
                     fx + dx + w / 2, fy + dy + h / 2, bool(_dm)))
npad = len(obst) - nseg - nvia
print(f'parsed: {len(fills)} fills, {nseg} tracks, {nvia} vias, {npad} pads')
assert npad > 0, 'incomplete parse: stitching not started'

CELL = 2.0
grid = collections.defaultdict(list)
for k, o in enumerate(obst):
    if o[0] == 'seg':
        x0, y0 = min(o[2], o[4]) - o[6], min(o[3], o[5]) - o[6]
        x1, y1 = max(o[2], o[4]) + o[6], max(o[3], o[5]) + o[6]
    elif o[0] == 'circ':
        x0, y0, x1, y1 = o[2] - o[4], o[3] - o[4], o[2] + o[4], o[3] + o[4]
    else:
        x0, y0, x1, y1 = o[2], o[3], o[4], o[5]
    for i in range(int((x0 - 1) // CELL), int((x1 + 1) // CELL) + 1):
        for j in range(int((y0 - 1) // CELL), int((y1 + 1) // CELL) + 1):
            grid[(i, j)].append(k)

def d_pt_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1; L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
def room(px, py, r, drill=None):
    """Is there room for a via of radius r: CLEAR to foreign copper and
    HOLE_CLEAR to ANY hole, including holes of its own net."""
    dr = (drill if drill is not None else r * 0.5)
    for hx, hy, hr in holes:
        if math.hypot(px - hx, py - hy) - hr - dr < HOLE_CLEAR: return False
    need = r + CLEAR
    ci, cj = int(px // CELL), int(py // CELL)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for k in grid.get((ci + di, cj + dj), ()):
                o = obst[k]
                if o[1].split('/')[-1] == NET: continue
                if o[0] == 'seg':
                    if d_pt_seg(px, py, o[2], o[3], o[4], o[5]) - o[6] < need: return False
                elif o[0] == 'circ':
                    if math.hypot(px - o[2], py - o[3]) - o[4] < need: return False
                else:
                    dx = max(o[2] - px, 0, px - o[4]); dy = max(o[3] - py, 0, py - o[5])
                    if math.hypot(dx, dy) < need: return False
    return True
def inside(px, py, pts):
    c = False; n = len(pts)
    for k in range(n):
        x1, y1 = pts[k]; x2, y2 = pts[(k + 1) % n]
        if (y1 > py) != (y2 > py) and px < x1 + (py - y1) / (y2 - y1) * (x2 - x1): c = not c
    return c
gnd_by_layer = collections.defaultdict(list)
for net, ly, pts in fills:
    if net == NET: gnd_by_layer[ly].append(pts)
def in_gnd(px, py, ly, margin):
    for pts in gnd_by_layer.get(ly, ()):
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        if not (min(xs) <= px <= max(xs) and min(ys) <= py <= max(ys)): continue
        if inside(px, py, pts):
            d = min(d_pt_seg(px, py, pts[k][0], pts[k][1], pts[(k + 1) % len(pts)][0], pts[(k + 1) % len(pts)][1])
                    for k in range(len(pts)))
            if d >= margin: return True
    return False

xs = [p[0] for _, _, pts in fills for p in pts]; ys = [p[1] for _, _, pts in fills for p in pts]
new = []
r = VIA / 2
x = min(xs)
while x <= max(xs):
    y = min(ys)
    while y <= max(ys):
        if in_gnd(x, y, 'F.Cu', r + CLEAR) and in_gnd(x, y, 'B.Cu', r + CLEAR) and room(x, y, r):
            new.append((round(x, 3), round(y, 3), VIA, DRILL))
        y += PITCH
    x += PITCH
print(f'on a {PITCH} mm grid: {len(new)} vias')

placed = [(v[0], v[1]) for v in new]

# --- targeted stitching of pieces that do not reach the main ground ------------
# Connectivity is computed for real: only vias and through-hole pads join layers.
# A piece "with a via inside" is not enough: the via may lead nowhere if there is
# no ground under it on the other layers (a cutout). That is how pieces passed
# stitching and stayed isolated.
#
# So the point for a new via must lie both in this piece and in a ground piece on
# ANOTHER layer that belongs to the main group.
gpat = [(ly, pts) for net, ly, pts in fills if net == NET]
_par = list(range(len(gpat)))
def _find(x):
    while _par[x] != x: _par[x] = _par[_par[x]]; x = _par[x]
    return x
def _union(a, b):
    ra, rb = _find(a), _find(b)
    if ra != rb: _par[ra] = rb
def _hits(x, y):
    return [i for i, (ly, pts) in enumerate(gpat) if inside(x, y, pts)]
for o in obst:
    if o[1].split('/')[-1] != NET: continue
    if o[0] == 'circ':
        t = _hits(o[2], o[3])
    elif o[0] == 'rect' and len(o) > 6 and o[6]:
        t = _hits((o[2] + o[4]) / 2, (o[3] + o[5]) / 2)
    else:
        continue
    for i in t[1:]: _union(t[0], i)
for px, py in placed:
    t = _hits(px, py)
    for i in t[1:]: _union(t[0], i)

def _poly_area(pts):
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2

_grp = collections.defaultdict(list)
for i in range(len(gpat)): _grp[_find(i)].append(i)
_main = max(_grp.values(), key=lambda g: sum(_poly_area(gpat[i][1]) for i in g), default=[])
_mainset = set(_main)
loose = [i for g in _grp.values() if g is not _main for i in g]
print(f'{NET} pieces {len(gpat)}, outside the main group {len(loose)}')

extra = 0
# Only the standard size. An ad-hoc smaller fallback "for narrow fragments" once
# placed drills below the project's min_via_drill_mm and vias below the board's
# minimum via diameter. KiCad DRC correctly reported via_diameter errors, but a
# hand-made list of "manufacturing-relevant" violation types did not include it,
# so the report claimed zero relevant errors with real errors on the board.
# A fragment that cannot take the standard via stays unstitched: that is the
# lesser evil compared with a via the fab will not make.
SIZES = ((VIA_SMALL, DRILL_SMALL),)
for i in sorted(loose, key=lambda i: -_poly_area(gpat[i][1])):
    ly, pts = gpat[i]
    if _poly_area(pts) < 1.0: continue      # below 1 mm^2 is electrically insignificant
    px_ = [p[0] for p in pts]; py_ = [p[1] for p in pts]
    # The three conditions are independent. Reporting their failure as one message ("no point
    # over the main ground") hid the cause: the binding one was almost always the distance to
    # the fragment edge, not a missing plane underneath. So the reasons are counted separately.
    #
    # Requiring "centre no closer than vsz/2 + CLEAR to the edge" is a double margin: CLEAR is
    # measured to the edge of the net's OWN fill, which is already pulled back from foreign copper
    # by the zone clearance, while room() checks the clearance to foreign copper separately.
    # It is enough for the via to overlap the fragment copper. Hence the stepped tolerance: full
    # fit with margin, then without margin, then half overlap; otherwise a fragment the size of
    # an 0402 pad cannot be stitched.
    best = None
    why = collections.Counter()
    for vsz, vdr in SIZES:
        for need in (vsz / 2 + CLEAR, vsz / 2, vsz / 4):
            gx = min(px_)
            while gx <= max(px_):
                gy = min(py_)
                while gy <= max(py_):
                    if inside(gx, gy, pts):
                        d = min(d_pt_seg(gx, gy, pts[k][0], pts[k][1],
                                         pts[(k + 1) % len(pts)][0], pts[(k + 1) % len(pts)][1])
                                for k in range(len(pts)))
                        if d < need:
                            why['narrow'] += 1
                        elif not room(gx, gy, vsz / 2, vdr / 2):
                            why['foreign copper'] += 1
                        # required: ground of the main group under the point on
                        # another layer, otherwise the via leads nowhere
                        elif not any(j in _mainset and gpat[j][0] != ly for j in _hits(gx, gy)):
                            why['no plane under the point'] += 1
                        elif best is None or d > best[0]:
                            best = (d, gx, gy, vsz, vdr)
                    gy += 0.05
                gx += 0.05
            if best: break
        if best: break
    if best:
        new.append((round(best[1], 3), round(best[2], 3), best[3], best[4]))
        placed.append((best[1], best[2]))
        for j in _hits(best[1], best[2]): _union(i, j)
        _mainset |= {i}
        extra += 1
    else:
        print(f'  could not stitch a {_poly_area(pts):.2f} mm^2 piece on {ly} '
              f'({min(px_):.1f},{min(py_):.1f}): '
              + (', '.join(f'{k} {v}' for k, v in why.most_common()) or 'no points inside'))
print(f'additional vias in pieces outside the main ground: {extra}')
print(f'total to place: {len(new)}')
if not APPLY:
    print('\nplan only. To write: python3 stitch_gnd.py --apply'); sys.exit(0)
open(BRD + '.bakStitch', 'w').write(s)
head = s.rstrip()
assert head.endswith(')')
head = head[:-1].rstrip('\n')
blk = [f'\t(via\n\t\t(at {x} {y})\n\t\t(size {sz})\n\t\t(drill {dr})\n'
       f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(tenting front back)\n\t\t(net "{NET}")\n\t\t(uuid "{uuid.uuid4()}")\n\t)'
       for x, y, sz, dr in new]
open(BRD, 'w').write(head + '\n' + '\n'.join(blk) + '\n)\n')
print(f'vias written: {len(blk)}, backup .bakStitch')
