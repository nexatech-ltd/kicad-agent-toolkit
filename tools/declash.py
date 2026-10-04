#!/usr/bin/env python3
"""Push apart overlapping small footprints after re-placement: for every clashing passive/test point the nearest
free position is searched (spiral with a 0.5 mm step, radius up to --r mm); large footprints are not moved.
  python3 tools/declash.py --board B [--r 10] [--max-area 30] [--keep U1,J1] [--apply]
Free = the courtyard does not intersect any other (gap --gap), inside the board outline and outside keepouts.
NOTE: it works on courtyards only and ignores copper - a moved part can land on other nets' tracks.
Check the result with DRC and restore critical parts from a snapshot if needed."""
import sys, math, argparse, pcbnew
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--r', type=float, default=10.0)
ap.add_argument('--gap', type=float, default=0.1); ap.add_argument('--max-area', type=float, default=30.0)
ap.add_argument('--keep', default=''); ap.add_argument('--grow', type=float, default=0.0)
ap.add_argument('--near', default='')
ap.add_argument('--apply', action='store_true')
a = ap.parse_args(); MM = 1e6
b = pcbnew.LoadBoard(a.board)
KEEP = set(x for x in a.keep.split(',') if x)
edge = b.GetBoardEdgesBoundingBox(); EX0, EY0, EX1, EY1 = edge.GetLeft()/MM, edge.GetTop()/MM, edge.GetRight()/MM, edge.GetBottom()/MM
KO = []
for z in b.Zones():
    if z.GetIsRuleArea():
        bb = z.GetBoundingBox(); KO.append((bb.GetLeft()/MM, bb.GetTop()/MM, bb.GetRight()/MM, bb.GetBottom()/MM))
def box(f, at=None):
    cy = f.GetCourtyard(pcbnew.F_CrtYd).BBox()
    bb = cy if cy.GetWidth() else f.GetBoundingBox(False, False)
    x0, y0, x1, y1 = bb.GetLeft()/MM, bb.GetTop()/MM, bb.GetRight()/MM, bb.GetBottom()/MM
    if at is not None:
        p = f.GetPosition(); dx, dy = at[0] - p.x/MM, at[1] - p.y/MM
        x0 += dx; x1 += dx; y0 += dy; y1 += dy
    return x0, y0, x1, y1
def area(f):
    x0, y0, x1, y1 = box(f); return (x1-x0)*(y1-y0)
FPS = list(b.GetFootprints())
boxes = {f.GetReference(): box(f) for f in FPS}
def gapof(b1, b2):
    dx = max(b1[0] - b2[2], b2[0] - b1[2]); dy = max(b1[1] - b2[3], b2[1] - b1[3])
    if dx <= 0 and dy <= 0: return 0.0
    if dx <= 0: return dy
    if dy <= 0: return dx
    return math.hypot(dx, dy)


def hits(ref, bx):
    # neighbours closer than allowed: overlap (--gap) or clearance below --grow
    lim = max(a.gap, a.grow)
    out = []
    for r2, b2 in boxes.items():
        if r2 == ref: continue
        if gapof(bx, b2) < lim - 1e-9: out.append(r2)
    return out
def bad_place(bx):
    if bx[0] < EX0 + 0.3 or bx[1] < EY0 + 0.3 or bx[2] > EX1 - 0.3 or bx[3] > EY1 - 0.3: return True
    for k in KO:
        if min(bx[2], k[2]) - max(bx[0], k[0]) > 0 and min(bx[3], k[3]) - max(bx[1], k[1]) > 0: return True
    return False
NEAR = set(x for x in a.near.split(',') if x)
clash = sorted({f.GetReference() for f in FPS if hits(f.GetReference(), boxes[f.GetReference()])})
if NEAR:
    clash = [r for r in clash if any(gapof(boxes[r], boxes[n]) < a.grow + 0.001 for n in NEAR if n in boxes)]
print('footprints in overlaps: %d' % len(clash))
byref = {f.GetReference(): f for f in FPS}
moved = 0
for ref in clash:
    f = byref[ref]
    if ref in KEEP or area(f) > a.max_area: continue
    if not hits(ref, boxes[ref]): continue
    p = f.GetPosition(); px, py = p.x/MM, p.y/MM
    best = None
    rad = 0.5
    while rad <= a.r and best is None:
        n = max(8, int(2 * math.pi * rad / 0.5))
        for i in range(n):
            ang = 2 * math.pi * i / n
            nx, ny = px + rad * math.cos(ang), py + rad * math.sin(ang)
            bx = box(f, (nx, ny))
            if bad_place(bx) or hits(ref, bx): continue
            best = (nx, ny); break
        rad += 0.5
    if best:
        f.SetPosition(pcbnew.VECTOR2I(int(round(best[0]*MM)), int(round(best[1]*MM))))
        boxes[ref] = box(f); moved += 1
        print('  %-6s -> (%.2f, %.2f), moved %.1f mm' % (ref, best[0], best[1], math.hypot(best[0]-px, best[1]-py)))
    else:
        print('  %-6s no free place within %.0f mm' % (ref, a.r))
left = sorted({r for r in boxes if hits(r, boxes[r])})
print('moved %d; still overlapping: %d %s' % (moved, len(left), ' '.join(left[:12])))
if a.apply: pcbnew.SaveBoard(a.board, b); print('written', a.board)
