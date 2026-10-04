#!/usr/bin/env python3
"""Debug probe pads: open the solder mask directly on a track of important nets (bare copper).
  python3 tools/probe_pads.py --board B [--d 1.2] [--nets A,B] [--apply] [--csv map.csv]

For each important net, find a point on its outer-layer track that is clear of foreign copper
(radius d/2 + clearance) and not under a package, and draw a circle on the mask layer there: no mask is
applied at that spot, so a probe lands directly on the track. No pads or footprints are added, so the BOM
and assembly do not change.

Skipped: nets that already reach a connector (`J*`/`X*`) or an existing test point (`TP*`); a probe can
be attached there without a dedicated window. The list of important nets (fnmatch patterns) comes from
`--nets`, else the `probe_nets` config key, else a generic default of supplies, clocks, resets, buses
and driver control lines.
"""
import sys, os, math, csv, argparse, fnmatch, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcbnew
import project

MM = 1e6
DEFAULT_PATTERNS = project.get('probe_nets', [
    '+3V3', '+5V', '+1V8*', 'VBUS*', 'VCC_*', 'VDDA', 'VCAP',
    'HSE_*', 'NRST', 'BOOT0', 'PG_*',
    '*_TX*', '*_RX*', '*_SDA', '*_SCL', '*_INT',
    '*_DIAG', '*_EN', '*_STEP', '*_DIR',
])

ap = argparse.ArgumentParser()
ap.add_argument('--board', required=True)
ap.add_argument('--d', type=float, default=1.2, help='mask window diameter, mm')
ap.add_argument('--clr', type=float, default=0.25, help='required clearance to foreign copper, mm')
ap.add_argument('--nets', default='', help='custom net list (comma-separated patterns)')
ap.add_argument('--min-len', type=float, default=2.0, help='minimum candidate segment length, mm')
ap.add_argument('--side', default='both', choices=['top', 'bottom', 'both'],
                help='which side to open windows on; the bottom is free of packages with single-sided assembly')
ap.add_argument('--csv', default='')
ap.add_argument('--apply', action='store_true')
a = ap.parse_args()
pats = [p for p in a.nets.split(',') if p] or DEFAULT_PATTERNS
b = pcbnew.LoadBoard(a.board)
F = b.GetLayerID('F.Cu')
BC = b.GetLayerID('B.Cu')
FM = b.GetLayerID('F.Mask')
BM = b.GetLayerID('B.Mask')
SIDES = {'top': [(F, FM, 'F.Cu')], 'bottom': [(BC, BM, 'B.Cu')], 'both': [(BC, BM, 'B.Cu'), (F, FM, 'F.Cu')]}[a.side]
R = a.d / 2.0

# access via a connector or an existing test point: no window needed
have_access = set()
courtyards = []
for f in b.GetFootprints():
    ref = f.GetReference()
    cy = f.GetCourtyard(pcbnew.F_CrtYd).BBox()
    bb = cy if cy.GetWidth() else f.GetBoundingBox(False, False)
    courtyards.append((bb.GetLeft() / MM, bb.GetTop() / MM, bb.GetRight() / MM, bb.GetBottom() / MM))
    if ref[:1] in ('J', 'X') and not ref.startswith('JP') or ref.startswith('TP'):
        for pd in f.Pads():
            if pd.GetNetname():
                have_access.add(pd.GetNetname().split('/')[-1])

def want(net):
    base = net.split('/')[-1]
    return any(fnmatch.fnmatch(base, p) for p in pats)

tracks_by = {F: [], BC: []}
others = []
for t in b.GetTracks():
    n = t.GetNetname().split('/')[-1]
    if t.GetClass() == 'PCB_VIA':
        p = t.GetPosition()
        others.append(('v', p.x / MM, p.y / MM, t.GetWidth() / MM / 2, n))
        continue
    s, e = t.GetStart(), t.GetEnd()
    item = ('t', s.x / MM, s.y / MM, e.x / MM, e.y / MM, t.GetWidth() / MM / 2, n)
    others.append(item)
    if t.GetLayer() in tracks_by:
        tracks_by[t.GetLayer()].append(item)
for f in b.GetFootprints():
    for pd in f.Pads():
        p = pd.GetPosition(); sz = pd.GetSize()
        others.append(('v', p.x / MM, p.y / MM, math.hypot(sz.x / MM, sz.y / MM) / 2, pd.GetNetname().split('/')[-1]))


def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def free_at(x, y, net, cu_layer):
    """no foreign copper closer than R+clr around the point; on the top side also not under a package"""
    for o in others:
        if o[-1] == net:
            continue
        if o[0] == 'v':
            if math.hypot(x - o[1], y - o[2]) - o[3] < R + a.clr:
                return False
        else:
            if dseg(x, y, o[1], o[2], o[3], o[4]) - o[5] < R + a.clr:
                return False
    if cu_layer == F:
        for (x0, y0, x1, y1) in courtyards:
            if x0 - R <= x <= x1 + R and y0 - R <= y <= y1 + R:
                return False
    return True


by_net = collections.defaultdict(lambda: collections.defaultdict(list))
for cu in (F, BC):
    for it in tracks_by[cu]:
        by_net[it[-1]][cu].append(it)
wanted = [n for n in by_net if want(n) and n not in have_access]
placed = []
for net in sorted(wanted):
    spot = None
    for cu, mask, cu_name in SIDES:
        segs = sorted(by_net[net][cu], key=lambda s: -math.hypot(s[3] - s[1], s[4] - s[2]))
        for (_, x1, y1, x2, y2, hw, _n) in segs:
            L = math.hypot(x2 - x1, y2 - y1)
            if L < a.min_len:
                continue
            steps = max(3, int(L / 0.25))
            for k in range(1, steps):
                t = k / steps
                x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
                if free_at(x, y, net, cu):
                    spot = (x, y, hw * 2, mask, cu_name)
                    break
            if spot:
                break
        if spot:
            break
    if spot:
        placed.append((net, spot[0], spot[1], spot[2], spot[3], spot[4]))

print('important nets without connector access: %d, windows found: %d' % (
    sum(1 for n in by_net if want(n) and n not in have_access), len(placed)))
for net, x, y, w, mask, cu_name in placed:
    print('  %-14s %-5s (%7.2f, %7.2f) track %.2f mm' % (net, cu_name, x, y, w))
if a.csv:
    with open(a.csv, 'w', newline='') as fh:
        wr = csv.writer(fh)
        wr.writerow(['net', 'x_mm', 'y_mm', 'track_w_mm', 'window_d_mm', 'layer'])
        for net, x, y, w, mask, cu_name in placed:
            wr.writerow([net, round(x, 2), round(y, 2), round(w, 2), a.d, cu_name])
    print('probe map ->', a.csv)
if a.apply and placed:
    for net, x, y, w, mask, cu_name in placed:
        c = pcbnew.PCB_SHAPE(b)
        c.SetShape(pcbnew.SHAPE_T_CIRCLE)
        c.SetLayer(mask)
        c.SetCenter(pcbnew.VECTOR2I(int(x * MM), int(y * MM)))
        c.SetEnd(pcbnew.VECTOR2I(int((x + R) * MM), int(y * MM)))
        c.SetFilled(True)
        c.SetWidth(0)
        b.Add(c)
    pcbnew.SaveBoard(a.board, b)
    print('mask windows added: %d, written %s' % (len(placed), a.board))
