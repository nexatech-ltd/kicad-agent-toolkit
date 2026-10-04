#!/usr/bin/env python3
"""Rigidly move a group of footprints (re-placement): shift/rotate about an anchor, check footprint overlaps.
  python3 tools/move_group.py --board B --spec floorplan.json [--apply] [--strip]
floorplan.json: [{"name":"motor driver","refs":["U1","C1","R1"],"dx":52,"dy":6},
                 {"name":"logic","refs":[...],"to":[160,105],"anchor":"U2","rot":90}]
--strip: remove the copper of every net that has pads in the moved footprints (otherwise tracks stay
at the old place; for a full re-layout it is easier to strip all copper separately).
Report: courtyard overlaps between footprints and parts outside the board outline. DRC after writing is the judge."""
import sys, json, math, argparse, collections, pcbnew
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--spec', required=True)
ap.add_argument('--apply', action='store_true'); ap.add_argument('--strip', action='store_true')
a = ap.parse_args(); MM = 1e6
b = pcbnew.LoadBoard(a.board)
spec = json.load(open(a.spec))
byref = {f.GetReference(): f for f in b.GetFootprints()}
edge = b.GetBoardEdgesBoundingBox()
EX0, EY0, EX1, EY1 = edge.GetLeft() / MM, edge.GetTop() / MM, edge.GetRight() / MM, edge.GetBottom() / MM

def rot_pt(x, y, cx, cy, deg):
    r = math.radians(-deg); dx, dy = x - cx, y - cy
    return cx + dx * math.cos(r) - dy * math.sin(r), cy + dx * math.sin(r) + dy * math.cos(r)

moved = set()
for g in spec:
    refs = [r for r in g['refs'] if r in byref]
    miss = [r for r in g['refs'] if r not in byref]
    if miss: print('  %s: not on the board: %s' % (g.get('name', '?'), ' '.join(miss)))
    if not refs: continue
    rot = float(g.get('rot', 0))
    if 'to' in g:
        anc = byref[g.get('anchor', refs[0])]; ax, ay = anc.GetPosition().x / MM, anc.GetPosition().y / MM
        dx, dy = g['to'][0] - ax, g['to'][1] - ay
    else:
        dx, dy = float(g.get('dx', 0)), float(g.get('dy', 0))
        anc = byref[g.get('anchor', refs[0])]; ax, ay = anc.GetPosition().x / MM, anc.GetPosition().y / MM
    for r in refs:
        f = byref[r]; p = f.GetPosition(); x, y = p.x / MM, p.y / MM
        if rot:
            x, y = rot_pt(x, y, ax, ay, rot)
            f.SetOrientationDegrees(f.GetOrientationDegrees() + rot)
        f.SetPosition(pcbnew.VECTOR2I(int(round((x + dx) * MM)), int(round((y + dy) * MM))))
        moved.add(r)
    print('  %-22s footprints %2d: shift (%+.1f, %+.1f)%s' % (g.get('name', '?'), len(refs), dx, dy, ', rotation %+.0f deg' % rot if rot else ''))

if a.strip:
    nets = set()
    for r in moved:
        for pd in byref[r].Pads():
            if pd.GetNetname(): nets.add(pd.GetNetname())
    dead = [t for t in b.GetTracks() if t.GetNetname() in nets]
    for t in dead: b.Remove(t)
    print('removed copper of moved nets: %d objects (%d nets)' % (len(dead), len(nets)))

# check: courtyard overlaps and parts outside the outline
def cy_box(f):
    bb = f.GetCourtyard(pcbnew.F_CrtYd).BBox()
    if bb.GetWidth() == 0: bb = f.GetBoundingBox(False, False)
    return bb.GetLeft() / MM, bb.GetTop() / MM, bb.GetRight() / MM, bb.GetBottom() / MM
boxes = {f.GetReference(): cy_box(f) for f in b.GetFootprints()}
bad = []
items = list(boxes.items())
for i, (r1, b1) in enumerate(items):
    if r1 not in moved: continue
    if b1[0] < EX0 or b1[1] < EY0 or b1[2] > EX1 or b1[3] > EY1: bad.append(('outside-outline', r1, ''))
    for r2, b2 in items:
        if r2 == r1: continue
        ox = min(b1[2], b2[2]) - max(b1[0], b2[0]); oy = min(b1[3], b2[3]) - max(b1[1], b2[1])
        if ox > 0.01 and oy > 0.01: bad.append(('overlap %.1fx%.1f mm' % (ox, oy), r1, r2))
seen = set(); uniq = []
for k, r1, r2 in bad:
    key = tuple(sorted([r1, r2])) + (k.split()[0],)
    if key in seen: continue
    seen.add(key); uniq.append((k, r1, r2))
print('overlaps / outside outline: %d' % len(uniq))
for k, r1, r2 in uniq[:25]: print('   %-22s %s %s' % (k, r1, r2))
if a.apply:
    pcbnew.SaveBoard(a.board, b); print('written', a.board)
else:
    print('(dry run; use --apply to write)')
