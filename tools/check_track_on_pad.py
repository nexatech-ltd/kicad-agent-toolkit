#!/usr/bin/env python3
"""Tracks running over pads of other nets.

Gap in verify.py: its "no copper overlap between different nets" check compares
tracks with tracks. A track over a foreign PAD is not covered, and in the DRC
report it shows up as 'Items shorting two nets' with no visible cause - easy to
misread as an unexplained cluster of errors around a group of resistors.

The track-to-pad clearance of the board's .kicad_dru is used (default 0.15 mm):
an approach closer than that clearance is a violation, not only actual overlap.

    python3 check_track_on_pad.py [board.kicad_pcb]
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import re, sys, os, math, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py lives next to this file
import paths
import project
from sexp import blocks

HERE = os.path.dirname(os.path.abspath(__file__))
BRD  = sys.argv[1] if len(sys.argv) > 1 else paths.BOARD
SIG  = ['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']
CLEAR_PAD = project.rule_clearance('Track', 'Pad', BRD, default=0.15)  # track-to-pad from the .kicad_dru

def rot(px, py, deg):
    a = math.radians(deg); c, si = math.cos(a), math.sin(a)
    return px * c + py * si, -px * si + py * c

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

s = open(BRD).read()

pads = []          # (net, layers, x0, y0, x1, y1, ref, pid)
for a, b in blocks(s, 'footprint'):
    blk = s[a:b]
    rm = re.search(r'\(property "Reference" "([^"]*)"', blk)
    am = re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', blk)
    if not (rm and am): continue
    ref = rm.group(1)
    fx, fy, fr = float(am.group(1)), float(am.group(2)), float(am.group(3) or 0)
    for p in _sub(blk, 'pad'):
        pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', p)
        sz = re.search(r'\(size ([\d.]+) ([\d.]+)\)', p)
        nm = re.search(r'\(net "([^"]*)"\)', p)
        pid = re.search(r'\(pad "([^"]*)"', p)
        if not (pa and sz and nm): continue
        # The pad angle is the third argument of its (at ...), and when absent it
        # is ZERO, not the footprint angle: KiCad stores the pad angle in board
        # axes and omits it when zero. Falling back to the footprint angle swapped
        # the extents of every pad on rotated footprints. Verified against DRC:
        # measured pad-to-track and pad-to-via clearances match to the fourth
        # decimal only with this rule (accounting for the roundrect corner radius).
        pang = float(pa.group(3)) if pa.group(3) is not None else 0.0
        dx, dy = rot(float(pa.group(1)), float(pa.group(2)), fr)
        w, h = float(sz.group(1)), float(sz.group(2))
        if abs(pang % 180) > 45: w, h = h, w
        lys = re.search(r'\(layers ([^)]*)\)', p)
        lay = re.findall(r'"([^"]+)"', lys.group(1)) if lys else []
        cu = list(SIG) if any(l == '*.Cu' for l in lay) else [l for l in lay if l.endswith('.Cu')]
        pads.append((nm.group(1), cu, fx + dx - w/2, fy + dy - h/2, fx + dx + w/2, fy + dy + h/2,
                     ref, pid.group(1) if pid else '?'))
assert len(pads) > 0, f'pads parsed: {len(pads)}'

segs = []
for a, b in blocks(s, 'segment'):
    blk = s[a:b]
    nm = re.search(r'\(net "([^"]*)"\)', blk)
    st = re.search(r'\(start (-?[\d.]+) (-?[\d.]+)\)', blk)
    en = re.search(r'\(end (-?[\d.]+) (-?[\d.]+)\)', blk)
    ly = re.search(r'\(layer "([^"]+)"\)', blk)
    wd = re.search(r'\(width ([\d.]+)\)', blk)
    if not (nm and st and en and ly and wd): continue
    segs.append((a, b, nm.group(1), ly.group(1), float(wd.group(1)),
                 float(st.group(1)), float(st.group(2)), float(en.group(1)), float(en.group(2))))
assert len(segs) > 0, f'segments parsed: {len(segs)}'
print(f'parsed: pads {len(pads)}, segments {len(segs)}')

def d_seg_rect(x1, y1, x2, y2, r):
    """Distance from a segment to a rectangle (0 when they intersect)."""
    rx0, ry0, rx1, ry1 = r
    def d_pt(px, py):
        dx = max(rx0 - px, 0, px - rx1); dy = max(ry0 - py, 0, py - ry1)
        return math.hypot(dx, dy)
    best = min(d_pt(x1, y1), d_pt(x2, y2))
    for t in range(1, 20):                       # sampling along the segment
        u = t / 20.0
        best = min(best, d_pt(x1 + (x2-x1)*u, y1 + (y2-y1)*u))
    return best

CELL = 2.0
grid = collections.defaultdict(list)
for k, p in enumerate(pads):
    for i in range(int((p[2]-1)//CELL), int((p[4]+1)//CELL)+1):
        for j in range(int((p[3]-1)//CELL), int((p[5]+1)//CELL)+1):
            grid[(i, j)].append(k)

hits = []
for a, b, net, ly, w, x1, y1, x2, y2 in segs:
    base = net.split('/')[-1]
    seen = set()
    for i in range(int((min(x1,x2)-1)//CELL), int((max(x1,x2)+1)//CELL)+1):
        for j in range(int((min(y1,y2)-1)//CELL), int((max(y1,y2)+1)//CELL)+1):
            for k in grid.get((i, j), ()):
                if k in seen: continue
                seen.add(k)
                p = pads[k]
                if ly not in p[1]: continue
                if p[0] == net: continue
                if p[0] == '' : continue
                d = d_seg_rect(x1, y1, x2, y2, p[2:6]) - w/2
                if d < CLEAR_PAD:
                    hits.append((d, base, p[0].split('/')[-1], p[6], p[7], ly, x1, y1, x2, y2, a, b))

hits.sort()
if not hits:
    print('\nno tracks over foreign pads')
    sys.exit(0)
print(f'\napproaches closer than {CLEAR_PAD} mm: {len(hits)}  (overlaps: {sum(1 for h in hits if h[0] <= 0)})')
for d, net, pnet, ref, pid, ly, x1, y1, x2, y2, a, b in hits:
    kind = 'OVERLAP  ' if d <= 0 else f'{d:.4f} mm  '
    print(f'  {kind}  {net:12s} on {ref}.{pid} [{pnet:14s}] {ly:6s}  ({x1:.1f},{y1:.1f})->({x2:.1f},{y2:.1f})')
