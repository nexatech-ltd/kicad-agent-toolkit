#!/usr/bin/env python3
"""Widen the tracks of one net to its net-class width, as far as clearances allow.

Meant for plane nets (e.g. GND, +3V3): they are handed to the router with an empty
pin list, so their copper cannot be ripped up and re-routed - nothing would route it
again. Widening in place is the only option.

The half-width is found from the actual clearances to foreign copper, honouring the
board's .kicad_dru: the track-to-track and track-to-pad rules plus per-net-class
track-to-track clearances.

Usage:   python3 widen_net.py +3V3            - plan
         python3 widen_net.py +3V3 --apply    - write
         python3 widen_net.py +3V3 --planes   - also widen segments on plane layers

The board comes from the BRD environment variable (default: paths.BOARD).
"""
import json, re, sys, os, math, fnmatch, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py lives next to this file
from sexp import blocks
import paths, project

BRD = os.environ.get('BRD') or paths.BOARD
PRO = paths.sibling(BRD, '.kicad_pro')

if len(sys.argv) < 2 or sys.argv[1].startswith('--'):
    raise SystemExit(__doc__)
WANT = sys.argv[1]
APPLY = '--apply' in sys.argv
STEP = 0.05

_lck = os.path.join(os.path.dirname(os.path.abspath(BRD)), '~' + os.path.basename(BRD) + '.lck')
if os.path.exists(_lck) and APPLY:
    raise SystemExit(f'REFUSED: the board is open in the KiCad editor ({_lck}).')

s = open(BRD).read()
ns = json.load(open(PRO))['net_settings']
WIDTH = {c['name']: float(c['track_width']) for c in ns['classes']}
pats = ns.get('netclass_patterns', [])

def netclass(n):
    base = n.split('/')[-1]
    for p in pats:
        if fnmatch.fnmatch(base, p['pattern']): return p['netclass']
    return 'Default'

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

# Track-to-track clearance is set by .kicad_dru rules per net CLASS, and a condition like
# "A.NetClass == 'X' && A.Type == 'Track' && B.Type == 'Track'" is checked by KiCad for the
# pair in both directions. So the class of EITHER participant decides, not only our own:
# widening a 0.15 mm-class net next to a 0.25 mm-class rail violates the rail's rule.
# Take the maximum of both nets' requirements.
TRACK_CLEAR = project.rule_clearance('Track', 'Track', BRD, default=0.15)
CLASS_CLEAR = project.class_track_clearance(BRD)
CLEAR_OWN = CLASS_CLEAR.get(netclass(WANT), TRACK_CLEAR)

def clear_track(other):
    return max(CLEAR_OWN, CLASS_CLEAR.get(netclass(other), TRACK_CLEAR))

CLEAR_PAD = project.rule_clearance('Track', 'Pad', BRD, default=0.15)
# A segment on a plane layer is a routing artifact: it already cuts the pour, and widening
# cuts deeper. Clearances stay legal (the pour backs off by itself), so the calculation
# below allows it; a plane split into islands shows up only by comparing the pour
# polygons (tools/util/zone_islands.py). A widened track on a plane layer can cut a small
# island off the plane and leave a net unconnected. So plane layers are skipped by
# default; --planes allows them, but then an island check before and after is MANDATORY.
PLANE_LAYERS = set(project.get('plane_layers', ['In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu']))
PLANES = '--planes' in sys.argv

pads = []
for a, b in blocks(s, 'footprint'):
    blk = s[a:b]
    am = re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', blk)
    if not am: continue
    fx, fy, fr = float(am.group(1)), float(am.group(2)), float(am.group(3) or 0)
    for p in _sub(blk, 'pad'):
        pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', p)
        sz = re.search(r'\(size ([\d.]+) ([\d.]+)\)', p)
        nm = re.search(r'\(net \d+ "([^"]*)"\)', p) or re.search(r'\(net "([^"]*)"\)', p)
        if not (pa and sz): continue
        # Pad angle is the third argument of (at ...); ZERO when absent.
        pang = float(pa.group(3)) if pa.group(3) is not None else 0.0
        dx, dy = rot(float(pa.group(1)), float(pa.group(2)), fr)
        w, h = float(sz.group(1)), float(sz.group(2))
        if abs(pang % 180) > 45: w, h = h, w
        lys = re.search(r'\(layers ([^)]*)\)', p)
        lay = re.findall(r'"([^"]+)"', lys.group(1)) if lys else []
        cu = ['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu'] if any(l == '*.Cu' for l in lay) \
             else [l for l in lay if l.endswith('.Cu')]
        cx, cy = fx + dx, fy + dy
        pads.append((nm.group(1) if nm else '', tuple(cu),
                     cx - w/2, cy - h/2, cx + w/2, cy + h/2))
assert len(pads) > 0, f'parsed {len(pads)} pads'

segs = []
for a, b in blocks(s, 'segment'):
    blk = s[a:b]
    nm = re.search(r'\(net \d+ "([^"]*)"\)', blk) or re.search(r'\(net "([^"]*)"\)', blk)
    st = re.search(r'\(start (-?[\d.]+) (-?[\d.]+)\)', blk)
    en = re.search(r'\(end (-?[\d.]+) (-?[\d.]+)\)', blk)
    wd = re.search(r'\(width ([\d.]+)\)', blk)
    ly = re.search(r'\(layer "([^"]+)"\)', blk)
    if not (nm and st and en and wd and ly): continue
    segs.append((a, b, nm.group(1), ly.group(1), float(wd.group(1)),
                 float(st.group(1)), float(st.group(2)), float(en.group(1)), float(en.group(2))))
vias = []
for a, b in blocks(s, 'via'):
    blk = s[a:b]
    at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)\)', blk)
    nm = re.search(r'\(net \d+ "([^"]*)"\)', blk) or re.search(r'\(net "([^"]*)"\)', blk)
    sz = re.search(r'\(size ([\d.]+)\)', blk)
    if at and sz:
        vias.append((nm.group(1) if nm else '', float(at.group(1)), float(at.group(2)),
                     float(sz.group(1)) / 2))
print(f'parsed: {len(pads)} pads, {len(segs)} segments, {len(vias)} vias')

def d_seg_seg(a1, a2, b1, b2):
    def d_pt_seg(px, py, x1, y1, x2, y2):
        dx, dy = x2-x1, y2-y1; L = dx*dx + dy*dy
        t = 0 if L == 0 else max(0, min(1, ((px-x1)*dx + (py-y1)*dy)/L))
        return math.hypot(px-(x1+t*dx), py-(y1+t*dy))
    return min(d_pt_seg(*a1, *b1, *b2), d_pt_seg(*a2, *b1, *b2),
               d_pt_seg(*b1, *a1, *a2), d_pt_seg(*b2, *a1, *a2))

def d_seg_rect(x1, y1, x2, y2, r):
    def dp(px, py):
        return math.hypot(max(r[0]-px, 0, px-r[2]), max(r[1]-py, 0, py-r[3]))
    return min(dp(x1+(x2-x1)*t/60, y1+(y2-y1)*t/60) for t in range(61))

# Only real defects are targets, not pad entries: widening an entry beyond the pad
# itself is pointless - the copper would stick out of it. Same criterion as in
# audit_widths.py: a package escape is a segment lying ENTIRELY within 3 mm of a pad
# of its own net.
KEEP_NARROW = 3.0
own_pads = [((p[2]+p[4])/2, (p[3]+p[5])/2) for p in pads if p[0].split('/')[-1] == WANT]

def is_escape(x1, y1, x2, y2):
    if not own_pads: return False
    if math.hypot(x2-x1, y2-y1) > KEEP_NARROW: return False
    d1 = min(math.hypot(px-x1, py-y1) for px, py in own_pads)
    d2 = min(math.hypot(px-x2, py-y2) for px, py in own_pads)
    return max(d1, d2) < KEEP_NARROW

narrow = [k for k, g in enumerate(segs)
          if g[2].split('/')[-1] == WANT and g[4] < WIDTH.get(netclass(g[2]), 0.2) - 0.005
          and not is_escape(g[5], g[6], g[7], g[8])]
on_plane = [k for k in narrow if segs[k][3] in PLANE_LAYERS]
targets = narrow if PLANES else [k for k in narrow if k not in set(on_plane)]
need = WIDTH.get(netclass(WANT), 0.2)
print(f'net {WANT}: class {netclass(WANT)}, required {need} mm, narrow segments {len(narrow)}')
if on_plane:
    print(f'  on plane layers {len(on_plane)}: %s' % ('widened (--planes) - check zone islands'
          if PLANES else 'skipped so as not to split the pour (--planes allows)'))

changes = []
for k in targets:
    _, _, net, ly, w, x1, y1, x2, y2 = segs[k]
    room = 99.0
    for j, g in enumerate(segs):
        if j == k or g[3] != ly or g[2].split('/')[-1] == WANT: continue
        d = d_seg_seg((x1, y1), (x2, y2), (g[5], g[6]), (g[7], g[8])) - g[4]/2 - clear_track(g[2])
        room = min(room, d)
    for p in pads:
        if p[0].split('/')[-1] == WANT or ly not in p[1]: continue
        room = min(room, d_seg_rect(x1, y1, x2, y2, p[2:6]) - CLEAR_PAD)
    for vn, vx, vy, vr in vias:
        if vn.split('/')[-1] == WANT: continue
        room = min(room, d_seg_seg((x1, y1), (x2, y2), (vx, vy), (vx, vy)) - vr - clear_track(vn))
    neww = min(need, math.floor(max(room, 0) * 2 / STEP + 1e-9) * STEP)
    if neww > w + 1e-9:
        neww = round(neww, 3)
        changes.append((k, w, neww))
        mark = '' if neww >= need - 1e-9 else '  (hit clearances, required width unreachable)'
        print(f'  {w} -> {neww} mm  {ly}  ({x1},{y1})->({x2},{y2}){mark}')
    else:
        print(f'  {w} mm kept: free half-width {max(room,0):.3f} mm  {ly}')

if not changes:
    print('\nnothing to widen'); sys.exit(0)
if not APPLY:
    print(f'\nplan shown. To write: python3 widen_net.py {WANT} --apply'); sys.exit(0)

open(BRD + '.bakWidenNet', 'w').write(s)
# Substitute by REGEX, not by the string f'(width {old})': pcbnew in KiCad 10 writes the
# width as "0.1500" while parsing yields float 0.15, so an exact string never matched -
# the file stayed unchanged while the script reported "widened N segments". Every
# replacement is now checked, and a missed one aborts the write.
out, prev, done = [], 0, 0
for k, old, new in sorted(changes, key=lambda c: segs[c[0]][0]):
    a, b = segs[k][0], segs[k][1]
    blk, n = re.subn(r'\(width [\d.]+\)', '(width %g)' % new, s[a:b], count=1)
    if n != 1:
        sys.exit('ERROR: no (width ...) in segment block %d - write cancelled' % k)
    done += 1
    out.append(s[prev:a]); out.append(blk); prev = b
out.append(s[prev:])
open(BRD, 'w').write(''.join(out))
chk = open(BRD).read()
if chk.count('(width %g)' % need) < done:
    sys.exit('ERROR: after writing, the file has fewer widened segments than expected')
print(f'\nwidened {done} segments, backup .bakWidenNet')
