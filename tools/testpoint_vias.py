#!/usr/bin/env python3
"""Signal test points: remove the solder mask from vias.

Why a via and not a pad. With single-sided assembly (all packages on the top side) the
bottom of the board is entirely free. A through via with the mask removed on BOTH sides is
reachable by a probe from the top and the bottom, takes no surface area and needs neither a
schematic edit nor a new symbol: parity does not see it, because it is net copper, not a part.

Not every via qualifies. Exposed copper next to foreign exposed copper invites a solder
bridge, so a clearance to copper of other nets is required, and vias under the footprint of
connectors and card holders (`testpoint_blocking_refs`) or inside keepouts are rejected.

Signals: `--nets`, else the `testpoint_nets` config key (in order; the order sets the TPS1...
numbering).

    python3 tools/testpoint_vias.py            # report
    python3 tools/testpoint_vias.py --apply
"""
import argparse
import json
import math
import os
import re
import sys

import numpy as np
import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project
import raster
from sexp import blocks

MM = 1e6
MIN_CLEAR = 0.30        # clearance from exposed copper to copper of another net, mm
# Signals to expose, in order; the order sets the TPS1... numbering.
SIGNALS = list(project.get('testpoint_nets', []))
# Parts whose bottom-side footprint a probe cannot reach under (connectors, card holders, batteries)
BLOCKING = tuple(project.get('testpoint_blocking_refs', []))


def base(net):
    return net.split('/')[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--out', default=paths.work('testpoint_vias.json'))
    ap.add_argument('--add-vias', action='store_true',
                    help='for nets without a usable via, place a new one on their track')
    ap.add_argument('--nets', default='',
                    help='comma-separated signal nets (default: the testpoint_nets config key)')
    a = ap.parse_args()
    signals = [n for n in a.nets.split(',') if n] or SIGNALS
    if not signals:
        print('no signals: pass --nets or set testpoint_nets in kicad-agent.json')
        return 1

    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=0.1)
    ko_f, _ = raster.keepout_mask(board, g, 'F.Cu')
    ko_b, _ = raster.keepout_mask(board, g, 'B.Cu')
    # Mask of all board holes with clearance: a new through via must keep its distance
    # from other holes, or the fab rejects it on hole-to-hole. Skipping this check once
    # put stitching vias right into mounting holes.
    holes, nholes = raster.hole_mask(board, g, extra=MIN_CLEAR)

    # forbidden rectangles under connectors and holders
    blocked = []
    for fp in board.GetFootprints():
        if fp.GetReference() in BLOCKING:
            bb = fp.GetBoundingBox(False, False)
            blocked.append((bb.GetLeft() / MM, bb.GetTop() / MM,
                            bb.GetRight() / MM, bb.GetBottom() / MM))

    # vias by net
    vias = {}
    for t in board.GetTracks():
        if t.GetClass() != 'PCB_VIA':
            continue
        v = t.Cast()
        vias.setdefault(base(v.GetNetname()), []).append(v)

    want = [s for s in signals]
    result, missing = [], []
    for i, sig in enumerate(want, 1):
        cands = vias.get(sig, [])
        if not cands:
            missing.append((sig, 'the net has no vias'))
            continue
        # foreign copper mask on both outer sides, computed once per net
        cu_f, _ = raster.copper_mask(board, g, 'F.Cu', nets_skip={n for n in
                                     {v.GetNetname() for v in cands}})
        cu_b, _ = raster.copper_mask(board, g, 'B.Cu', nets_skip={n for n in
                                     {v.GetNetname() for v in cands}})
        best = None
        for v in cands:
            x, y = v.GetPosition().x / MM, v.GetPosition().y / MM
            try:
                w = v.GetWidth(board.GetLayerID('F.Cu'))
            except TypeError:
                w = v.GetWidth()
            r = w / MM / 2.0
            if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in blocked):
                continue
            c, rr = g.col(x), g.row(y)
            if not (0 <= c < g.w and 0 <= rr < g.h):
                continue
            if ko_f[rr, c] or ko_b[rr, c]:
                continue
            # clearance check: no foreign copper within r+MIN_CLEAR
            probe = g.empty()
            raster.fill_disc(g, probe, x, y, r + MIN_CLEAR)
            if (probe & (cu_f | cu_b)).any():
                continue
            score = r        # the larger the annular ring, the easier for the probe
            if best is None or score > best[0]:
                best = (score, x, y, r, v.m_Uuid.AsString() if hasattr(v, 'm_Uuid') else '')
        if best is None:
            missing.append((sig, 'all vias of the net are under a connector, in a keepout or lack clearance'))
            continue
        _, x, y, r, uuid = best
        result.append({'tp': 'TPS%d' % i, 'net': sig, 'x': round(x, 3), 'y': round(y, 3),
                       'ring_mm': round(r * 2, 2), 'uuid': uuid})
        print('  %-6s %-16s (%7.2f, %7.2f)  ring %.2f mm' % ('TPS%d' % i, sig, x, y, r * 2))

    # Fallback: the net has no usable via but has a track, so place a new one.
    # A via on a track of its own net is electrically neutral and gives probe access from
    # both sides. Clearance is checked on ALL copper layers: a through via pierces them all,
    # and clearance to foreign copper on the inner layers is just as mandatory.
    added = []
    if a.add_vias and missing:
        # Two sizes: first 0.8/0.4 (larger ring, easier to probe), then 0.6/0.3 when space
        # is short. Prefer via sizes already used on the board over introducing a new one;
        # an ad-hoc size can violate the board's min_via_drill_mm.
        SIZES = ((0.8, 0.4), (0.6, 0.3))
        cu_all = {}
        still = []
        for sig, why in missing:
            segs = [t for t in board.GetTracks()
                    if t.GetClass() != 'PCB_VIA' and base(t.GetNetname()) == sig
                    and board.GetLayerName(t.GetLayer()) in ('F.Cu', 'B.Cu')]
            if not segs:
                still.append((sig, 'the net has no tracks on outer layers (not routed)'))
                continue
            nets_own = {t.GetNetname() for t in segs}
            key = tuple(sorted(nets_own))
            if key not in cu_all:
                cu_all[key] = [raster.copper_mask(board, g, L, nets_skip=nets_own)[0]
                               for L in raster.SIGNAL]
            others = cu_all[key]
            spot = None
            for vd, drill in SIZES:
                for t in sorted(segs, key=lambda z: -z.GetLength()):
                    x1, y1 = t.GetStart().x / MM, t.GetStart().y / MM
                    x2, y2 = t.GetEnd().x / MM, t.GetEnd().y / MM
                    L = math.hypot(x2 - x1, y2 - y1)
                    if L < vd + 0.5:
                        continue
                    steps = max(3, int(L / 0.4))
                    for k in range(steps + 1):
                        f = k / steps
                        x, y = x1 + (x2 - x1) * f, y1 + (y2 - y1) * f
                        c, rr = g.col(x), g.row(y)
                        if not (0 <= c < g.w and 0 <= rr < g.h):
                            continue
                        if ko_f[rr, c] or ko_b[rr, c]:
                            continue
                        if any(x0 <= x <= x1b and y0 <= y <= y1b
                               for x0, y0, x1b, y1b in blocked):
                            continue
                        probe = g.empty()
                        raster.fill_disc(g, probe, x, y, vd / 2 + MIN_CLEAR)
                        if any((probe & m).any() for m in others):
                            continue
                        if (probe & holes).any():
                            continue
                        spot = (x, y, t.GetNetname(), vd, drill)
                        break
                    if spot:
                        break
                if spot:
                    break
            if spot:
                added.append({'net': sig, 'x': round(spot[0], 3), 'y': round(spot[1], 3),
                              'netname': spot[2], 'ring_mm': spot[3], 'drill': spot[4]})
                print('  + %-16s new via %.1f mm (%7.2f, %7.2f)'
                      % (sig, spot[3], spot[0], spot[1]))
            else:
                still.append((sig, 'no room for a via with clearance on the net tracks'))
        missing = still

    print('\ntest points found: %d of %d (new vias among them: %d)'
          % (len(result) + len(added), len(want), len(added)))
    if missing:
        print('without a test point (%d):' % len(missing))
        for s, why in missing:
            print('    %-16s %s' % (s, why))
    with open(a.out, 'w') as f:
        json.dump({'found': result, 'new_vias': added, 'missing': missing},
                  f, ensure_ascii=False, indent=1)

    if not a.apply:
        print('report only; add --apply to remove the mask')
        return 0

    # mask removal: a text edit of the tenting token in the (via ...) block
    txt = open(a.board).read()
    open(a.board + '.bakTent', 'w').write(txt)
    keys = {(round(r['x'], 3), round(r['y'], 3)) for r in result}
    out, prev, done = [], 0, 0
    for s, e in blocks(txt, 'via'):
        blk = txt[s:e]
        m = re.search(r'\(at ([-\d.]+) ([-\d.]+)\)', blk)
        if not m:
            continue
        key = (round(float(m.group(1)), 3), round(float(m.group(2)), 3))
        if key not in keys:
            continue
        # The tenting block is replaced WHOLE, by counting brackets. The regex
        # `\(tenting[^)]*(?:\([^)]*\)[^)]*)*\)` is greedy in the wrong place: on a block
        # with two sub-expressions it closed after the first one and left a dangling
        # `(back yes))` tail in the file. The board then failed to load entirely:
        # "Expecting blind, micro, at, size... Got 'back'".
        i = blk.find('(tenting')
        if i >= 0:
            j, d, q = i, 0, False
            while j < len(blk):
                ch = blk[j]
                if q:
                    if ch == '"':
                        q = False
                elif ch == '"':
                    q = True
                elif ch == '(':
                    d += 1
                elif ch == ')':
                    d -= 1
                    if d == 0:
                        break
                j += 1
            nb = (blk[:i] + '(tenting\n\t\t\t(front no)\n\t\t\t(back no)\n\t\t)'
                  + blk[j + 1:])
        else:
            nb = blk
        if nb == blk:                     # no tenting block: add one before (net
            nb = blk.replace('\n\t\t(net ',
                             '\n\t\t(tenting\n\t\t\t(front no)\n\t\t\t(back no)\n\t\t)\n\t\t(net ', 1)
        out.append(txt[prev:s]); out.append(nb); prev = e; done += 1
    open(a.board, 'w').write(''.join(out) + txt[prev:])
    print('mask removed from %d vias, backup .bakTent' % done)
    if added:
        import uuid as _u
        txt = open(a.board).read()
        head = txt.rstrip()
        assert head.endswith(')')
        head = head[:-1].rstrip('\n')
        blocks_new = []
        for v in added:
            blocks_new.append(
                '\t(via\n\t\t(at %.4f %.4f)\n\t\t(size %s)\n\t\t(drill %s)\n'
                '\t\t(layers "F.Cu" "B.Cu")\n\t\t(tenting\n\t\t\t(front no)\n'
                '\t\t\t(back no)\n\t\t)\n\t\t(net "%s")\n\t\t(uuid "%s")\n\t)'
                % (v['x'], v['y'], v['ring_mm'], v['drill'], v['netname'], _u.uuid4()))
        open(a.board, 'w').write(head + '\n' + '\n'.join(blocks_new) + '\n)\n')
        print('new vias added: %d' % len(added))
    return 0


if __name__ == '__main__':
    sys.exit(main())
