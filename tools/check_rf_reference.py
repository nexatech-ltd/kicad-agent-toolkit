#!/usr/bin/env python3
"""Continuity of the reference plane under an RF feed, plus an audit of signal tracks on
ground-plane layers.

Typical requirement: a 50 Ohm microstrip on F.Cu referenced to the first inner layer;
that layer must be continuous under the whole feed, with no splits and no other nets in
the corridor (at h = 0.1 mm the fringing field decays within ~0.3 mm). Neither DRC nor ERC
knows about this: a split in the reference violates no clearance.

The corridor is twice as wide as the computed field decay: track width plus 0.6 mm on each
side. Inside the corridor the reference layer must carry a ground fill and no copper of
other nets.

Part two audits signal tracks on the ground-plane layers. A routing policy may allow them
(fewer unrouted breaks), but forbids them near the feed and inside antenna keepouts; the
check enforces that policy, not the absence of tracks.

Config keys (kicad-agent.json): rf_net_prefixes, rf_reference_layer, rf_plane_layers,
rf_fringe_mm.

    python3 check_rf_reference.py [board.kicad_pcb]
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import os
import sys

import numpy as np
import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import raster
import paths
import project

MM = 1e6
FRINGE = float(project.get('rf_fringe_mm', 0.6))   # twice the computed 0.3 mm decay at h = 0.1 mm
REF_LAYER = project.get('rf_reference_layer', 'In1.Cu')   # reference of the F.Cu microstrip
PLANE_LAYERS = tuple(project.get('rf_plane_layers', [REF_LAYER]))
# Net base-name prefixes that identify RF nets (e.g. "RF_", "Net-(ANT").
RF_PREFIXES = tuple(project.get('rf_net_prefixes', ['RF_', 'Net-(ANT']))


def rf_nets(board):
    out = set()
    for t in board.GetTracks():
        n = t.GetNetname()
        b = n.split('/')[-1]
        if b.startswith(RF_PREFIXES):
            out.add(n)
    return out


def main():
    brd = sys.argv[1] if len(sys.argv) > 1 else paths.BOARD
    board = pcbnew.LoadBoard(brd)
    g = raster.Grid(board, step=0.05)          # finer step: the corridor is only ~1.4 mm wide
    nets = rf_nets(board)
    assert nets, 'no RF nets found on the board: the check is meaningless'
    print('RF nets: %s' % ', '.join(sorted(n.split('/')[-1] for n in nets)))

    fcu = board.GetLayerID('F.Cu')
    feed = g.empty()
    nseg = 0
    widths = set()
    for t in board.GetTracks():
        if t.GetClass() == 'PCB_VIA' or t.GetNetname() not in nets:
            continue
        if t.GetLayer() != fcu:
            continue
        w = t.GetWidth() / MM
        widths.add(round(w, 2))
        raster.fill_segment(g, feed, t.GetStart().x / MM, t.GetStart().y / MM,
                            t.GetEnd().x / MM, t.GetEnd().y / MM, w + 2 * FRINGE)
        nseg += 1
    assert nseg > 0, 'no RF copper found on F.Cu'
    print('feed segments on F.Cu: %d, widths %s mm, corridor %.0f mm2'
          % (nseg, sorted(widths), g.area(feed)))

    fill, nz = raster.zone_fill_mask(board, g, REF_LAYER, net='GND')
    ko_ref = raster.keepout_mask(board, g, REF_LAYER)[0]
    # Inside the antenna keepout there is NO copper ON PURPOSE: that is the radiator, not
    # the line. Continuity applies to the feed up to the keepout edge, not inside it; a
    # first version of the check did not distinguish this and flagged area that lay
    # entirely inside the keepout.
    # The feed's own vias (F.Cu <-> B.Cu transitions and matching-network vias) cut the
    # reference with their antipads by necessity: that is a line transition, not a
    # reference defect. They are counted separately and subtracted from the gap; only
    # copper of OTHER nets cutting the reference remains a defect.
    own_via = g.empty(); n_own = 0
    zclr = 0.0
    for z in board.Zones():
        if z.GetNetname() == 'GND' and z.IsOnLayer(board.GetLayerID(REF_LAYER)):
            zclr = max(zclr, z.GetLocalClearance() / MM)
    for t in board.GetTracks():
        if t.GetClass() == 'PCB_VIA' and t.GetNetname() in nets:
            p = t.GetPosition(); n_own += 1
            raster.fill_disc(g, own_via, p.x / MM, p.y / MM, t.GetWidth() / MM / 2 + max(zclr, 0.2) + 0.05)
    # Same for THROUGH-HOLE pads of the RF nets themselves: a connector pin pierces the
    # reference, and its antipad is as unavoidable as a via's. Without this the check flagged
    # a few hundredths of mm2 under an RF connector pin, i.e. the line's own required geometry.
    # SMD pads are NOT included: they do not cut the reference, so a gap under them is real.
    n_pad = 0
    for fp in board.GetFootprints():
        for pd in fp.Pads():
            if pd.GetNetname() not in nets: continue
            if pd.GetDrillSizeX() <= 0: continue
            q = pd.GetPosition(); n_pad += 1
            r = max(pd.GetSizeX(), pd.GetSizeY()) / MM / 2 + max(zclr, 0.2) + 0.05
            raster.fill_disc(g, own_via, q.x / MM, q.y / MM, r)
    own_cut = feed & own_via & ~fill & ~ko_ref
    gap = feed & ~fill & ~ko_ref & ~own_via
    inside_ko = feed & ko_ref
    print('   feed transitions: %d vias and %d through-hole RF pads, their antipads in the corridor %.2f mm2 (by design of the line)'
          % (n_own, n_pad, g.area(own_cut)))
    print('\n1. ground continuity on %s under the corridor' % REF_LAYER)
    print('   GND fills on the layer: %d, area %.0f mm2' % (nz, g.area(fill)))
    print('   corridor inside the antenna keepout: %.2f mm2 (%.0f %%), no copper there'
          ' by design' % (g.area(inside_ko), 100 * g.area(inside_ko) / max(g.area(feed), 1e-9)))
    if gap.any():
        ys, xs = np.nonzero(gap)
        print('   REFERENCE GAP outside the keepout: %.3f mm2, %d cells, first at (%.2f, %.2f)'
              % (g.area(gap), gap.sum(), g.x(xs[0]), g.y(ys[0])))
    else:
        print('   reference is continuous up to the keepout edge: no gaps')

    cu, st = raster.copper_mask(board, g, REF_LAYER, nets_skip={'GND'} | set(nets))   # own RF copper (via barrels) is not foreign
    intr = feed & cu & ~ko_ref
    print('\n2. foreign copper on %s inside the corridor' % REF_LAYER)
    if intr.any():
        ys, xs = np.nonzero(intr)
        print('   FOUND: %.3f mm2, first at (%.2f, %.2f)'
              % (g.area(intr), g.x(xs[0]), g.y(ys[0])))
    else:
        print('   no foreign copper in the corridor')

    print('\n3. signal tracks on ground-plane layers (routing policy)')
    ko = {}
    for L in PLANE_LAYERS:
        ko[L] = raster.keepout_mask(board, g, L)[0]
    bad = []
    for L in PLANE_LAYERS:
        lid = board.GetLayerID(L)
        cnt = 0
        length = 0.0
        for t in board.GetTracks():
            if t.GetClass() == 'PCB_VIA' or t.GetLayer() != lid:
                continue
            if t.GetNetname() == 'GND':
                continue
            cnt += 1
            length += t.GetLength() / MM
            probe = g.empty()
            raster.fill_segment(g, probe, t.GetStart().x / MM, t.GetStart().y / MM,
                                t.GetEnd().x / MM, t.GetEnd().y / MM,
                                t.GetWidth() / MM)
            why = []
            if L == REF_LAYER and (probe & feed).any():
                why.append('in the RF feed corridor')
            if (probe & ko[L]).any():
                why.append('in a keepout')
            if why:
                bad.append((L, t.GetNetname().split('/')[-1],
                            t.GetStart().x / MM, t.GetStart().y / MM, '; '.join(why)))
        print('   %s: %d tracks, %.0f mm' % (L, cnt, length))
    if bad:
        print('   POLICY VIOLATIONS: %d' % len(bad))
        for L, net, x, y, why in bad[:12]:
            print('      %-5s %-22s (%7.2f, %7.2f)  %s' % (L, net, x, y, why))
    else:
        print('   policy holds: no track in the feed corridor or keepouts')

    ok = (not gap.any()) and (not intr.any()) and not bad
    print('\nRESULT: %s' % ('passed' if ok else 'ISSUES FOUND'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
