#!/usr/bin/env python3
"""Silkscreen: bring it up to fab minimums and move reference texts off copper and outlines.

Two tasks, the first more important than the second:

  1. Line thickness. Reference texts drawn at e.g. 0.12 mm are below a typical fab
     minimum (~0.15 mm): thinner ink does not hold and the text does not print.
     So texts must not be shrunk to resolve conflicts - only moved apart. A 0.8 mm
     height is a typical fab minimum too, so it is not reduced either.

  2. Texts on exposed copper (they hinder soldering: ink on a pad) and texts over
     other footprints' outlines (they make the board hard to read during assembly).

The position is searched around the original one: the text box must not overlap
pads, other footprints' silk objects or the board edge. Texts that cannot be placed
stay where they are and are listed - reference designators must not be hidden
silently, the board is serviced by them.

Usage:   python3 fix_silk.py <drc.json> [--board B.kicad_pcb] [--apply] [--thickness-only]
                 [--min-thick 0.15] [--clear-cu 0.15] [--clear-silk 0.10]
The board comes from --board, else the BRD environment variable, else paths.BOARD.
"""
import json, re, sys, os, math, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py lives next to this file
from sexp import blocks
import paths

# Pass --board (or BRD) explicitly whenever several boards exist, so the right one is edited.
_argv = sys.argv[1:]
BRD = os.environ.get('BRD') or paths.BOARD
if '--board' in _argv:
    _i = _argv.index('--board'); BRD = _argv[_i + 1]; _argv = _argv[:_i] + _argv[_i + 2:]
BRD = os.path.abspath(BRD)

def _num_opt(name, default):
    """Numeric option '--name VALUE', removed from _argv."""
    global _argv
    if name in _argv:
        i = _argv.index(name); v = float(_argv[i + 1]); _argv = _argv[:i] + _argv[i + 2:]
        return v
    return default

MIN_THICK = _num_opt('--min-thick', 0.15)    # typical fab minimum line width ~0.15 mm
CLEAR_CU = _num_opt('--clear-cu', 0.15)      # text-to-copper clearance
CLEAR_SILK = _num_opt('--clear-silk', 0.10)  # text-to-foreign-silk clearance
GLYPH = 0.78            # fraction of the size per glyph - approximation of KiCad font metrics
APPLY = '--apply' in _argv
THICK_ONLY = '--thickness-only' in _argv
args = [a for a in _argv if not a.startswith('--')]
DRC = args[0] if args else None
print('board: %s' % BRD)

_lck = os.path.join(os.path.dirname(BRD), '~' + os.path.basename(BRD) + '.lck')
if os.path.exists(_lck) and APPLY:
    raise SystemExit(f'REFUSED: the board is open in the KiCad editor ({_lck}).')

# Candidate thin lines: 0.10..0.149 mm; only those below MIN_THICK are thickened.
_THIN = re.compile(r'\(thickness (0\.1[0-4]\d*)\)')

def _thicken(m):
    return f'(thickness {MIN_THICK})' if float(m.group(1)) < MIN_THICK else m.group(0)

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

s = open(BRD).read()

# --- 1. text line thickness ----------------------------------------------------
thin = sum(1 for m in _THIN.finditer(s) if float(m.group(1)) < MIN_THICK)
print(f'texts thinner than {MIN_THICK} mm: {thin}')

# --- parsing for the position search -------------------------------------------
pads = []          # (x0,y0,x1,y1) on F.Cu
silk = []          # (x0,y0,x1,y1, ref) foreign silkscreen
refs = {}          # ref -> (property block position, x, y, angle, text, size)
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
        lys = re.search(r'\(layers ([^)]*)\)', p)
        if not (pa and sz): continue
        lay = re.findall(r'"([^"]+)"', lys.group(1)) if lys else []
        if not any(l in ('F.Cu', '*.Cu') for l in lay): continue
        pang = float(pa.group(3)) if pa.group(3) is not None else 0.0
        dx, dy = rot(float(pa.group(1)), float(pa.group(2)), fr)
        w, h = float(sz.group(1)), float(sz.group(2))
        if abs(pang % 180) > 45: w, h = h, w
        pads.append((fx + dx - w/2, fy + dy - h/2, fx + dx + w/2, fy + dy + h/2))
    for tag in ('fp_line', 'fp_rect', 'fp_circle', 'fp_poly', 'fp_arc'):
        for g in _sub(blk, tag):
            # In the file the layer is named F.SilkS - F.Silkscreen is only its
            # user name in the board header. Searching for the latter finds zero
            # objects, and the overlap check silently becomes empty.
            if '"F.SilkS"' not in g: continue
            pts = [(float(x), float(y)) for x, y in
                   re.findall(r'\((?:xy|start|end|center|mid) (-?[\d.]+) (-?[\d.]+)\)', g)]
            if not pts: continue
            xs = []; ys = []
            for px, py in pts:
                dx, dy = rot(px, py, fr); xs.append(fx + dx); ys.append(fy + dy)
            silk.append((min(xs), min(ys), max(xs), max(ys), ref))
    for pr in _sub(blk, 'property'):
        if not pr.startswith('(property "Reference"'): continue
        pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', pr)
        sz = re.search(r'\(size ([\d.]+) ([\d.]+)\)', pr)
        if not (pa and sz): continue
        refs[ref] = (a + blk.index(pr), a + blk.index(pr) + len(pr),
                     float(pa.group(1)), float(pa.group(2)), float(pa.group(3) or 0),
                     ref, float(sz.group(2)), fx, fy, fr)
# The text boxes themselves are obstacles too: without them the moves do not see
# each other and leave text-on-text overlaps (most of the remaining conflicts).
print(f'parsed: F.Cu pads {len(pads)}, silk objects {len(silk)}, texts {len(refs)}')
assert len(pads) > 0 and len(refs) > 0, 'parse incomplete'

edge = []
for tag in ('gr_line', 'gr_rect', 'gr_arc', 'gr_poly'):
    for a, b in blocks(s, tag):
        g = s[a:b]
        if 'Edge.Cuts' not in g: continue
        pts = [(float(x), float(y)) for x, y in
               re.findall(r'\((?:xy|start|end|center|mid) (-?[\d.]+) (-?[\d.]+)\)', g)]
        for k in range(len(pts) - 1):
            edge.append((pts[k], pts[k+1]))

def text_box(cx, cy, ang, txt, size, thick):
    w = len(txt) * size * GLYPH + thick
    h = size + thick
    if abs(ang % 180) > 45: w, h = h, w
    return (cx - w/2, cy - h/2, cx + w/2, cy + h/2)

def overlap(a, b, gap):
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or
                a[3] + gap < b[1] or b[3] + gap < a[1])

def free(box, ref):
    for p in pads:
        if overlap(box, p, CLEAR_CU): return False
    for x0, y0, x1, y1, r2 in silk:
        if r2 == ref: continue
        if overlap(box, (x0, y0, x1, y1), CLEAR_SILK): return False
    for r2, tb in textbox.items():
        if r2 == ref: continue
        if overlap(box, tb, CLEAR_SILK): return False
    for (p1, p2) in edge:
        ex = (min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))
        if overlap(box, ex, CLEAR_SILK): return False
    return True

# --- 2. which texts to move ----------------------------------------------------
targets = set()
if DRC and not THICK_ONLY:
    d = json.load(open(DRC))
    for x in d.get('violations', []):
        if x['type'] not in ('silk_over_copper', 'silk_overlap', 'silk_edge_clearance'): continue
        for it in x.get('items', []):
            m = re.search(r'(?:Reference field|Footprint text) of (\S+)', it.get('description', ''))
            if m: targets.add(m.group(1))
print(f'texts in conflicts: {len(targets)}')

# current boxes of all texts (updated as moves are made)
textbox = {}
for r2, (ba2, bb2, rx2, ry2, ra2, txt2, sz2, fx2, fy2, fr2) in refs.items():
    ax2, ay2 = rot(rx2, ry2, fr2)
    textbox[r2] = text_box(fx2 + ax2, fy2 + ay2, ra2 + fr2, txt2, sz2, MIN_THICK)

moves = []; stuck = []
for ref in sorted(targets):
    if ref not in refs: continue
    ba, bb, rx, ry, rang, txt, size, fx, fy, fr = refs[ref]
    ax, ay = rot(rx, ry, fr)
    cx, cy = fx + ax, fy + ay
    if free(text_box(cx, cy, rang + fr, txt, size, MIN_THICK), ref):
        continue                      # still free after thickening
    best = None
    for r in [x * 0.1 for x in range(3, 31)]:
        for k in range(24):
            th = 2 * math.pi * k / 24
            nx, ny = round(cx + r * math.cos(th), 3), round(cy + r * math.sin(th), 3)
            if free(text_box(nx, ny, rang + fr, txt, size, MIN_THICK), ref):
                best = (r, nx, ny); break
        if best: break
    if best:
        r, nx, ny = best
        lx, ly = rot(nx - fx, ny - fy, -fr)
        moves.append((ref, ba, bb, rx, ry, round(lx, 3), round(ly, 3), r))
        textbox[ref] = text_box(nx, ny, rang + fr, txt, size, MIN_THICK)
    else:
        stuck.append(ref)

print(f'\ncan move: {len(moves)} | failed: {len(stuck)}')
for ref, ba, bb, ox, oy, nx, ny, r in moves[:12]:
    print(f'   {ref:6s} ({ox},{oy}) -> ({nx},{ny}), by {r:.1f} mm')
if len(moves) > 12: print(f'   … and {len(moves)-12} more')
if stuck:
    print('   no room found:', ', '.join(stuck[:14]) + (' …' if len(stuck) > 14 else ''))

if not APPLY:
    print(f'\nplan shown. To write: python3 fix_silk.py {DRC or ""} --apply')
    sys.exit(0)

open(BRD + '.bakSilk', 'w').write(s)
# thickness - across the whole board
out = _THIN.sub(_thicken, s)
# moves - from the end of the file to the start so offsets stay valid
for ref, ba, bb, ox, oy, nx, ny, r in sorted(moves, key=lambda m: -m[1]):
    blk = s[ba:bb]
    new = re.sub(r'\(at -?[\d.]+ -?[\d.]+', f'(at {nx} {ny}', blk, count=1)
    new = _THIN.sub(_thicken, new)
    idx = out.find(blk)
    if idx < 0:
        blk2 = _THIN.sub(_thicken, blk)
        idx = out.find(blk2)
        if idx < 0: continue
        out = out[:idx] + new + out[idx+len(blk2):]
    else:
        out = out[:idx] + new + out[idx+len(blk):]
open(BRD, 'w').write(out)
print(f'\nthickness raised to {MIN_THICK} mm, texts moved: {len(moves)}')
print('backup .bakSilk')
