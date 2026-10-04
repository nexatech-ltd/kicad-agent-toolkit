#!/usr/bin/env python3
"""Audit copper against the current rules: where routing is narrower than its class width.

Why a separate script: a typical .kicad_dru has no track-width rules at all - only
clearances. The required width lives in the net classes of the .kicad_pro, and DRC
does not check it. So a track routed before its net was assigned to a class looks like
ordinary copper and carries no "done wrong" mark.

This is how two phases of a motor survived several routing rounds running in parallel
0.15 mm apart for ~30 mm against a required 0.4/0.25. Step-by-step routing preserved
them: every round all existing copper is handed to the router as protected, i.e.
"do not touch".

Usage:   python3 audit_widths.py            - report
         python3 audit_widths.py --strip    - remove the offending segments, print the
                                              affected nets (to be re-routed)
"""
import json, re, sys, os, math, fnmatch, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py lives next to this file
from sexp import blocks
import paths, project

# --board (or BRD) matters because --strip WRITES to the board: be explicit about which
# board is edited, and allow comparing two states.
BRD = os.environ.get('BRD') or paths.BOARD
if '--board' in sys.argv:
    BRD = sys.argv[sys.argv.index('--board') + 1]
BRD = os.path.abspath(BRD)
PRO = paths.sibling(BRD, '.kicad_pro')

# Same refusal as in stitch_gnd.py and ses_import.py: an open editor is a second writer
# of the same file and can silently roll back work.
_lck = os.path.join(os.path.dirname(BRD), '~' + os.path.basename(BRD) + '.lck')
if os.path.exists(_lck) and '--strip' in sys.argv:
    raise SystemExit(f'REFUSED: the board is open in the KiCad editor ({_lck}).')

s   = open(BRD).read()
pro = json.load(open(PRO))
ns  = pro['net_settings']
WIDTH = {c['name']: float(c['track_width']) for c in ns['classes']}
pats  = ns.get('netclass_patterns', [])

def netclass(n):
    base = n.split('/')[-1]
    for p in pats:
        if fnmatch.fnmatch(base, p['pattern']):
            return p['netclass']
    return 'Default'

# Tolerance: KiCad stores widths to the micron, and the router rounds.
# 0.005 mm is half a grid step; below that a difference means nothing.
TOL = 0.005

# How far from a pad of its own net a narrowing is still legitimate. Same value as
# KEEP_NARROW in widen_net.py: you cannot escape a 0.4 mm-pitch package at full class
# width, and demanding it forbids escaping the package at all. A defect is a narrow
# run in open field with BOTH ends farther than KEEP_NARROW from pads of its own net.
KEEP_NARROW = 3.0

def rot(px, py, deg):
    """Rotation in KiCad's frame (Y down)."""
    a = math.radians(deg); c, si = math.cos(a), math.sin(a)
    return px * c + py * si, -px * si + py * c

def _sub(blk, tag):
    """Nested (tag ...) blocks inside a footprint block."""
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

# ---------- pad centres per net ----------
pad_of = collections.defaultdict(list)
for a, b in blocks(s, 'footprint'):
    blk = s[a:b]
    am = re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', blk)
    if not am: continue
    fx, fy, fr = float(am.group(1)), float(am.group(2)), float(am.group(3) or 0)
    for pblk in _sub(blk, 'pad'):
        pa = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)', pblk)
        nm = re.search(r'\(net "([^"]*)"\)', pblk)
        if not (pa and nm): continue
        dx, dy = rot(float(pa.group(1)), float(pa.group(2)), fr)
        pad_of[nm.group(1)].append((fx + dx, fy + dy))
npads = sum(len(v) for v in pad_of.values())
print(f'pads with nets: {npads}')
assert npads > 0, f'parsed {npads} pads - the check is meaningless'

def near_pad(net, x1, y1, x2, y2):
    """How far a segment strays from the pads of its own net.

    The FARTHER of the two ends is used: a package escape is a segment lying
    entirely within KEEP_NARROW of a pad. Looking at the nearer end would accept as
    a legitimate escape e.g. a 0.15 mm power track (required 1.2 mm) that leaves its
    pad and runs 6 mm between two resistors.
    """
    ps = pad_of.get(net) or []
    if not ps: return 99.0
    d1 = min(math.hypot(px - x1, py - y1) for px, py in ps)
    d2 = min(math.hypot(px - x2, py - y2) for px, py in ps)
    return max(d1, d2)

rows, bad = [], []
for a, b in blocks(s, 'segment'):
    blk = s[a:b]
    nm = re.search(r'\(net "([^"]*)"\)', blk)
    wm = re.search(r'\(width ([\d.]+)\)', blk)
    sm = re.search(r'\(start ([-\d.]+) ([-\d.]+)\)', blk)
    em = re.search(r'\(end ([-\d.]+) ([-\d.]+)\)', blk)
    ly = re.search(r'\(layer "([^"]*)"\)', blk)
    if not (nm and wm and sm and em):
        continue
    net, w = nm.group(1), float(wm.group(1))
    cls = netclass(net)
    need = WIDTH.get(cls, 0.2)
    L = math.hypot(float(em.group(1)) - float(sm.group(1)),
                   float(em.group(2)) - float(sm.group(2)))
    rows.append((net, cls, w, need, L))
    if w < need - TOL:
        x1, y1 = float(sm.group(1)), float(sm.group(2))
        x2, y2 = float(em.group(1)), float(em.group(2))
        # A package escape is a SHORT segment near its own pad. Proximity alone is not
        # enough: power nets have pads all over the board, so the far end of any run
        # lands near some pad of its own. That is how a 6 mm narrow power run passed
        # the audit as "legitimate".
        esc = L <= KEEP_NARROW and near_pad(net, x1, y1, x2, y2) < KEEP_NARROW
        bad.append((a, b, net, cls, w, need, L, ly.group(1) if ly else '?', esc))

print(f'segments parsed: {len(rows)}')
assert len(rows) > 0, 'suspiciously few parsed - check the parser'

# Nets poured as planes: dsn_export.py hands them to Freerouting with an empty pin
# list, i.e. the router does not route them. Removing their track loses the
# connection for good: nothing would re-route it. Their defects are listed
# separately, for widening in place (widen_net.py).
PLANE_NETS = project.plane_nets()

defects = [x for x in bad if not x[8] and x[2].split('/')[-1] not in PLANE_NETS]
onplane = [x for x in bad if not x[8] and x[2].split('/')[-1] in PLANE_NETS]
escapes = [x for x in bad if x[8]]

per = collections.defaultdict(lambda: [0, 0.0, 0, 0.0])   # class -> [total, mm, defects, defect mm]
for net, cls, w, need, L in rows:
    per[cls][0] += 1; per[cls][1] += L
for a, b, net, cls, w, need, L, ly, esc in defects:
    per[cls][2] += 1; per[cls][3] += L

print(f'\nnarrow copper total: {len(bad)} segments - package escapes {len(escapes)}, '
      f'defects {len(defects)}, defects in plane nets {len(onplane)}\n')
print(f'{"class":12s} {"needed":>7s} {"segments":>9s} {"defects":>9s} {"length":>10s}')
for cls in sorted(per, key=lambda c: -per[c][3]):
    n, L, nb, Lb = per[cls]
    mark = '  <-- ' if nb else ''
    print(f'{cls:12s} {WIDTH.get(cls,0.2):6.2f}m {n:9d} {nb:11d} {Lb:9.1f} mm{mark}')

if defects:
    print(f'\ndefects: {len(defects)} segments, '
          f'{sum(x[6] for x in defects):.1f} mm in {len({x[2] for x in defects})} nets')
    print('\nworst by length:')
    for a, b, net, cls, w, need, L, ly, esc in sorted(defects, key=lambda x: -x[6])[:12]:
        print(f'  {w:.2f} instead of {need:.2f} mm - {L:6.1f} mm  {net.split("/")[-1]:14s} {cls:10s} {ly}')
else:
    print('\nno narrow copper in open field - everything narrow sits at pins')

if onplane:
    print(f'\nin plane nets (cannot be removed, widen in place): '
          f'{len(onplane)} segments, {sum(x[6] for x in onplane):.1f} mm')
    for a, b, net, cls, w, need, L, ly, esc in sorted(onplane, key=lambda x: -x[6]):
        print(f'  {w:.2f} instead of {need:.2f} mm - {L:6.1f} mm  {net.split("/")[-1]:8s} {ly}')

if '--strip' in sys.argv and defects:
    open(BRD + '.bakAudit', 'w').write(s)
    out, prev = [], 0
    for a, b, *_ in sorted(defects, key=lambda x: x[0]):
        out.append(s[prev:a]); prev = b
    out.append(s[prev:])
    open(BRD, 'w').write(''.join(out))
    nets = sorted({x[2].split('/')[-1] for x in defects})
    print(f'\nremoved {len(defects)} segments, backup .bakAudit')
    print(f'nets affected: {len(nets)}')
    print('  ' + ', '.join(nets[:20]) + (' …' if len(nets) > 20 else ''))
