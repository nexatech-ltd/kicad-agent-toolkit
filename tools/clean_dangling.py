#!/usr/bin/env python3
"""Remove dangling tracks and vias.

Stubs are left behind after ripping copper for rerouting: a track with one end
connected to nothing is electrically useless, and a via to nowhere is an extra
drill hit in the order and a potential antenna at RF frequencies.

Connectivity is computed by coincident points: track ends, via centres, pad
centres. An item dangles if at least one of its ends meets nothing. Removal is
iterative: once a stub is removed, the neighbouring segment becomes a stub, so
several passes are made until nothing changes.

Fills are not treated as a generic connection: a track ending inside a polygon
of its own net is connected through it; such ends are checked separately and
are not considered stubs.

Usage:  python3 clean_dangling.py [--board B] [--apply]
"""
import re, sys, os, math, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py is a sibling
from sexp import blocks

import paths
BRD = sys.argv[sys.argv.index('--board') + 1] if '--board' in sys.argv else paths.BOARD
APPLY = '--apply' in sys.argv
EPS = 0.01      # point coincidence tolerance, mm

_lck = os.path.join(os.path.dirname(os.path.abspath(BRD)), '~' + os.path.basename(BRD) + '.lck')
if os.path.exists(_lck) and APPLY:
    raise SystemExit(f'REFUSED: the board is open in the KiCad editor ({_lck}).')

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

def key(x, y):
    return (round(x / EPS), round(y / EPS))

def load():
    s = open(BRD).read()
    # copper layers of this board, for pads on "*.Cu"
    hdr = s[:s.find('(setup')] if '(setup' in s else s[:20000]
    cu_all = re.findall(r'\(\d+ "([A-Za-z0-9_]+\.Cu)"', hdr) or ['F.Cu', 'B.Cu']
    # anchor points: pads (with their extent) and filled areas
    anchors = collections.defaultdict(set)      # net -> {points}
    padrect = collections.defaultdict(list)     # net -> [(x0,y0,x1,y1)]
    for a, b in blocks(s, 'footprint'):
        blk = s[a:b]
        am = re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', blk)
        if not am: continue
        fx, fy, fr = float(am.group(1)), float(am.group(2)), float(am.group(3) or 0)
        for p in _sub(blk, 'pad'):
            pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', p)
            sz = re.search(r'\(size ([\d.]+) ([\d.]+)\)', p)
            nm = re.search(r'\(net \d+ "([^"]*)"\)', p) or re.search(r'\(net "([^"]*)"\)', p)
            if not (pa and sz and nm): continue
            pang = float(pa.group(3)) if pa.group(3) is not None else 0.0
            dx, dy = rot(float(pa.group(1)), float(pa.group(2)), fr)
            w, h = float(sz.group(1)), float(sz.group(2))
            if abs(pang % 180) > 45: w, h = h, w
            cx, cy = fx + dx, fy + dy
            net = nm.group(1)
            lys = re.search(r'\(layers ([^)]*)\)', p)
            lay = re.findall(r'"([^"]+)"', lys.group(1)) if lys else []
            cu = list(cu_all) if any(l == '*.Cu' for l in lay) \
                 else [l for l in lay if l.endswith('.Cu')]
            anchors[net].add(key(cx, cy))
            padrect[net].append((cx - w/2, cy - h/2, cx + w/2, cy + h/2, tuple(cu)))
    # filled areas: an end inside a polygon of its own net counts as connected
    fills = collections.defaultdict(list)        # net -> [(layer, polygon)]
    for a, b in blocks(s, 'zone'):
        blk = s[a:b]
        nm = re.search(r'\(net \d+ "([^"]*)"\)', blk) or re.search(r'\(net "([^"]*)"\)', blk)
        if not nm: continue
        for fp in _sub(blk, 'filled_polygon'):
            flm = re.search(r'\(layer "([^"]+)"\)', fp)
            pts = [(float(x), float(y)) for x, y in
                   re.findall(r'\(xy (-?[\d.]+) (-?[\d.]+)\)', fp)]
            if len(pts) > 2 and flm: fills[nm.group(1)].append((flm.group(1), pts))

    objs = []       # (a, b, kind, net, [points])
    for a, b in blocks(s, 'segment'):
        blk = s[a:b]
        st = re.search(r'\(start (-?[\d.]+) (-?[\d.]+)\)', blk)
        en = re.search(r'\(end (-?[\d.]+) (-?[\d.]+)\)', blk)
        nm = re.search(r'\(net \d+ "([^"]*)"\)', blk) or re.search(r'\(net "([^"]*)"\)', blk)
        if not (st and en and nm): continue
        ly = re.search(r'\(layer "([^"]+)"\)', blk)
        objs.append((a, b, 'track', nm.group(1),
                     [(float(st.group(1)), float(st.group(2))),
                      (float(en.group(1)), float(en.group(2)))],
                     ly.group(1) if ly else '?'))
    for a, b in blocks(s, 'via'):
        blk = s[a:b]
        at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)\)', blk)
        nm = re.search(r'\(net \d+ "([^"]*)"\)', blk) or re.search(r'\(net "([^"]*)"\)', blk)
        if not (at and nm): continue
        objs.append((a, b, 'via', nm.group(1), [(float(at.group(1)), float(at.group(2)))], 'VIA'))
    return s, objs, anchors, padrect, fills

def inside(pts, x, y):
    c = False; n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]; x2, y2 = pts[(i+1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1 + 1e-18):
            c = not c
    return c

def pad_layers(padrect, net, x, y):
    out = set()
    for x0, y0, x1, y1, cu in padrect.get(net, ()):
        if x0 - EPS <= x <= x1 + EPS and y0 - EPS <= y <= y1 + EPS: out |= set(cu)
    return out

def fill_layers(fills, net, x, y):
    return {ly for ly, pts in fills.get(net, ()) if inside(pts, x, y)}

def in_pad(padrect, net, x, y):
    return bool(pad_layers(padrect, net, x, y))

def in_fill(fills, net, x, y):
    return bool(fill_layers(fills, net, x, y))

s, objs, anchors, padrect, fills = load()
print(f'parsed: {len(objs)} copper items')
assert objs or ('(segment' not in s and '(via' not in s), 'incomplete parse'

alive = set(range(len(objs)))
removed_total = []
for rnd in range(1, 12):
    cnt = collections.Counter()
    for k in alive:
        for pt in objs[k][4]:
            cnt[key(*pt)] += 1
    # which copper layers arrive at each point
    at_pt = collections.defaultdict(set)
    for k in alive:
        o = objs[k]
        if o[2] != 'track': continue
        for pt in o[4]:
            at_pt[key(*pt)].add(o[5])

    drop = set()
    for k in alive:
        a, b, kind, net, pts, ly = objs[k]
        if kind == 'via':
            x, y = pts[0]
            kk = key(x, y)
            # Only ISOLATED vias are removed: no track arrives at them and they
            # do not lie inside a pad or fill of their own net.
            #
            # The stricter criterion "a via must connect copper on two layers"
            # is right in principle, but in a track-via-track chain removing one
            # link makes the next one dangle, and the iteration eats the whole
            # net: in practice it removed almost twice as many items, including
            # fully routed nets, and stopped converging.
            # Vias with copper left on only one layer are reported by DRC as
            # via_dangling; those are left for manual review.
            if not at_pt.get(kk) and kk not in anchors.get(net, ()) \
               and not in_pad(padrect, net, x, y) and not in_fill(fills, net, x, y):
                drop.add(k)
            continue
        if abs(pts[0][0] - pts[1][0]) < EPS and abs(pts[0][1] - pts[1][1]) < EPS:
            drop.add(k); continue          # degenerate zero-length segment
        for x, y in pts:
            kk = key(x, y)
            if cnt[kk] > 1: continue                    # meets other copper
            if kk in anchors.get(net, ()): continue     # centre of its own pad
            if in_pad(padrect, net, x, y): continue     # inside its own pad
            if in_fill(fills, net, x, y): continue      # inside its own fill
            drop.add(k); break
    if not drop: break
    alive -= drop
    removed_total.append((rnd, len(drop)))
    print(f'  pass {rnd}: dangling {len(drop)}')

dead = [k for k in range(len(objs)) if k not in alive]
kinds = collections.Counter(objs[k][2] for k in dead)
nets = collections.Counter(objs[k][3].split('/')[-1] for k in dead)
print(f'\ntotal to remove: {len(dead)}  {dict(kinds)}')
if nets:
    print('  nets:', ', '.join(f'{n}x{c}' for n, c in nets.most_common(10)))
if not dead:
    print('no dangling copper'); sys.exit(0)
if not APPLY:
    print('\nplan only. To write: python3 clean_dangling.py --apply')
    sys.exit(0)

open(BRD + '.bakDangle', 'w').write(s)
out, prev = [], 0
for k in sorted(dead, key=lambda k: objs[k][0]):
    a, b = objs[k][0], objs[k][1]
    out.append(s[prev:a]); prev = b
out.append(s[prev:])
open(BRD, 'w').write(''.join(out))
print(f'\nremoved {len(dead)} items, backup .bakDangle')
