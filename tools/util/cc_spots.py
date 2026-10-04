#!/usr/bin/env python3
"""Free positions for an 0402 capacitor near a given pin, on the same side of the board.

Usage: python3 tools/util/cc_spots.py BOARD.kicad_pcb U1 28 --nets NET,GND --radius 4

Some datasheets require a capacitor on the same side as the IC, close to the pin, with no
via between pin and capacitor (typical for USB-C CC-line capacitors of PD controllers). So the
search is not "anywhere with room" but a spot within a few millimetres of the pin, on the pin's
side, reachable by a track on that copper layer. Occupancy follows the same rule as other
placement searches: FOREIGN copper (pads, tracks, vias of other nets) and other parts' bodies
block; a GND pour does not, because it pulls back on refill and the capacitor's second pad
connects to it anyway. Prints the nearest positions with the rotation to use.
"""
import argparse
import math
import sys

import pcbnew

MM = 1e6


def mm(v):
    return v / MM


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('board')
    ap.add_argument('ref')
    ap.add_argument('pin')
    ap.add_argument('--nets', default='', help='net of pad 1 and net of pad 2, comma-separated '
                    '(e.g. NET,GND): each pad has its own set of allowed nets')
    ap.add_argument('--radius', type=float, default=4.0, help='maximum distance from the pin, mm')
    ap.add_argument('--pad', type=float, nargs=2, default=(0.62, 0.72),
                    help='0402 pad: width and height including clearance, mm')
    ap.add_argument('--pitch', type=float, default=0.92, help='distance between 0402 pad centres, mm')
    ap.add_argument('--step', type=float, default=0.1)
    ap.add_argument('--top', type=int, default=12)
    ap.add_argument('--ignore', default='', help='references not treated as obstacles '
                    '(e.g. the already placed capacitor whose position is being chosen)')
    a = ap.parse_args()

    b = pcbnew.LoadBoard(a.board)
    parts = [n.strip() for n in a.nets.split(',') if n.strip()]
    own1 = {parts[0]} if parts else set()
    own2 = {parts[1]} if len(parts) > 1 else set()
    own = own1 | own2
    fp = [f for f in b.GetFootprints() if f.GetReference() == a.ref][0]
    pad = [p for p in fp.Pads() if p.GetNumber() == a.pin][0]
    px, py = mm(pad.GetPosition().x), mm(pad.GetPosition().y)
    layer = 'B.Cu' if fp.IsFlipped() else 'F.Cu'
    lid = b.GetLayerID(layer)
    print('pin %s.%s net %s at (%.3f, %.3f), side %s'
          % (a.ref, a.pin, pad.GetNetname().split('/')[-1], px, py, layer))

    def foreign(name, allowed=None):
        return name.split('/')[-1] not in (own if allowed is None else allowed)

    # Foreign copper on this layer: pad rectangles, track segments, via circles.
    # Copper is stored WITH ITS NET: the signal pad and the GND pad have different allowed nets,
    # so a single "foreign" list does not work. Treating the signal track as "own" for the whole
    # position lets the GND pad land on it - a short.
    rects, segs, holes = [], [], []
    for f in b.GetFootprints():
        if f.GetReference() in {x.strip() for x in a.ignore.split(',') if x.strip()}:
            continue
        for p in f.Pads():
            if not p.IsOnLayer(lid):
                continue
            bb = p.GetBoundingBox()
            rects.append((mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom()),
                          p.GetNetname().split('/')[-1]))
    for t in b.GetTracks():
        name = t.GetNetname().split('/')[-1]
        if t.GetClass() == 'PCB_VIA':
            v = t.Cast()
            holes.append((mm(v.GetPosition().x), mm(v.GetPosition().y), mm(v.GetWidth()) / 2, name))
        elif t.IsOnLayer(lid):
            segs.append((mm(t.GetStart().x), mm(t.GetStart().y),
                         mm(t.GetEnd().x), mm(t.GetEnd().y), mm(t.GetWidth()) / 2, name))
    # Space taken by bodies, by courtyard - including the target IC itself: excluding it
    # ("it cannot block itself") lets the search propose positions UNDER the IC. An obstacle
    # is an area, not an ownership question.
    crt = b.GetLayerID('B.CrtYd' if fp.IsFlipped() else 'F.CrtYd')
    bodies = []
    skip = {x.strip() for x in a.ignore.split(',') if x.strip()}
    for f in b.GetFootprints():
        if f.GetReference() in skip:
            continue
        # A courtyard is often drawn as FOUR LINES, and each line's bbox is a thin strip. Taken
        # separately, the interior of the rectangle stays "free" and the search proposes a spot
        # UNDER the body (e.g. inside a QFN). Merge all courtyard items of a part into one box.
        xs, ys = [], []
        for d in f.GraphicalItems():
            if d.GetLayer() == crt:
                bb = d.GetBoundingBox()
                xs += [mm(bb.GetLeft()), mm(bb.GetRight())]
                ys += [mm(bb.GetTop()), mm(bb.GetBottom())]
        if xs:
            bodies.append((min(xs), min(ys), max(xs), max(ys)))
        else:            # no courtyard: fall back to the pads+graphics bbox
            bb = f.GetBoundingBox(False, False)
            bodies.append((mm(bb.GetLeft()), mm(bb.GetTop()),
                           mm(bb.GetRight()), mm(bb.GetBottom())))

    def seg_dist(x, y, s):
        x1, y1, x2, y2, w = s[:5]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)) - w

    def seg_hits_rect(s, x0, y0, x1r, y1r):
        # s = (ax, ay, bx, by, half_width, net)
        """Whether the segment (including its half-width) intersects the rectangle.

        Sampling points of the outline is NOT enough: a track can run straight through the pad
        rectangle without touching its centre, corners or edge midpoints, and DRC then reports
        `shorting_items`. So the rectangle is grown by the track half-width and the segment is
        clipped properly (Liang-Barsky), which also covers endpoints inside.
        """
        ax, ay, bx, by, hw = s[:5]
        x0, y0, x1r, y1r = x0 - hw, y0 - hw, x1r + hw, y1r + hw
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, ax - x0), (dx, x1r - ax), (-dy, ay - y0), (dy, y1r - ay)):
            if p == 0:
                if q < 0:
                    return False
                continue
            r = q / p
            if p < 0:
                if r > t1:
                    return False
                t0 = max(t0, r)
            else:
                if r < t0:
                    return False
                t1 = min(t1, r)
        return t0 <= t1

    def free_rect(cx, cy, w, h, allowed):
        """Whether a pad that may touch only nets in `allowed` fits here."""
        x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        for rx0, ry0, rx1, ry1, name in rects:
            if name in allowed:
                continue
            if not (x1 < rx0 or x0 > rx1 or y1 < ry0 or y0 > ry1):
                return False
        for bx0, by0, bx1, by1 in bodies:
            if not (x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1):
                return False
        for hx, hy, hr, name in holes:
            if name in allowed:
                continue
            if x0 - hr < hx < x1 + hr and y0 - hr < hy < y1 + hr:
                return False
        for s in segs:
            if s[5] in allowed:
                continue
            if seg_hits_rect(s, x0, y0, x1, y1):
                return False
        return True

    pw, ph = a.pad
    out = []
    n = int(a.radius / a.step)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            cx, cy = px + i * a.step, py + j * a.step
            d = math.hypot(cx - px, cy - py)
            if d > a.radius:
                continue
            # Where pad 1 points for each rotation - measured on a board (place_at.py prints
            # pad coordinates): rot 0 -> -x, 90 -> +y, 180 -> +x, 270 -> -y.
            # The (dx, dy) pairs below are SUBTRACTED: pad 1 goes to (cx - dx, cy - dy), hence
            # -pitch/2 for rot 90. Assuming pad 1 goes to -y at 90 swaps the pads and puts the
            # signal feed onto the GND pad - a short.
            for rot, (dx, dy) in ((0, (a.pitch / 2, 0)), (90, (0, -a.pitch / 2)),
                                  (180, (-a.pitch / 2, 0)), (270, (0, a.pitch / 2))):
                w, h = (pw, ph) if rot % 180 == 0 else (ph, pw)
                # pad 1 (net own1) at (cx-dx, cy-dy), pad 2 (own2) opposite
                if free_rect(cx - dx, cy - dy, w, h, own1) and \
                   free_rect(cx + dx, cy + dy, w, h, own2):
                    out.append((d, cx, cy, rot))
    out.sort()
    print('positions found: %d' % len(out))
    shown, used = 0, []
    for d, cx, cy, rot in out:
        if any(math.hypot(cx - ux, cy - uy) < 0.8 for ux, uy, _ in used):
            continue
        used.append((cx, cy, rot))
        print('  (%.2f, %.2f) rotation %d, %.2f mm from the pin' % (cx, cy, rot, d))
        shown += 1
        if shown >= a.top:
            break
    return 0


if __name__ == '__main__':
    sys.exit(main())
