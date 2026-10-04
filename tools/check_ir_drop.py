#!/usr/bin/env python3
"""Voltage drop and conductor cross-section of rails, from the ACTUAL copper geometry.

Neither DRC nor ERC knows about this: an undersized track violates no clearance, and voltage drop
is not a rule. Yet this is exactly the defect class that makes a first board "reboot under load"
and takes weeks to find.

Two separate questions are answered.

**Cross-section.** IPC-2221: I = k * dT^0.44 * A^0.725, A in mil^2, k = 0.048 for external
layers and 0.024 for internal. This gives the required width for a given current and temperature
rise. Copper thickness should come from the board stack-up (config keys below), not a guess.

**Drop.** A copper graph of the net is built: nodes are segment ends, pads and vias merged by
coordinate; edges are segments with R = rho*L/(w*t) and vias with R of the barrel wall. Then the
resistance from the source pin to every load pin is computed (Dijkstra over resistance) and the
drop at the given current.

Poured rails (plane nets) cannot and need not be computed this way: a pour has resistance orders
of magnitude lower, and its drop is not set by tracks. Rails routed as tracks are checked - and
those are exactly the highest-current non-plane nets.

Config keys (kicad-agent.json):
  ir_rails          {net: [continuous_A, peak_A or null, "where the number comes from"]}
  rail_nominal_v    {net: volts}               - to judge droop in percent
  rail_vmin         {net: [volts, "source"]}   - datasheet minimum at the load pins
  copper_outer_um, copper_inner_um             - copper thickness from the stack-up

    python3 check_ir_drop.py [board.kicad_pcb]
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import heapq
import math
import os
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project

MM = 1e6
RHO = 1.72e-8              # copper resistivity, ohm*m
T_OUTER = float(project.get('copper_outer_um', 35.0)) * 1e-6   # outer copper thickness (stack-up)
T_INNER = float(project.get('copper_inner_um', 17.5)) * 1e-6
PLATING = 20e-6            # via barrel plating thickness
DT = 10.0                  # allowed temperature rise, deg C

# Rail -> (continuous current A, peak current A or None, where it comes from).
#
# Take the current of the LOAD, not the converter rating: using a converter's "5 V / 3 A" rating
# declares the rail several times undersized and predicts hundreds of mV of drop. That is what the
# chip can deliver, not what flows on the rail. Use the load budget (thermal/power analysis).
#
# For a pulsed load the cross-section is sized by the AVERAGE current: the thermal time constant
# of a track is orders of magnitude longer than a sub-millisecond burst, and the peak is supplied
# by the bulk capacitor at the load (that is its purpose). The peak is checked for droop, not heat.
RAILS = {k: (float(v[0]), None if v[1] is None else float(v[1]), str(v[2]) if len(v) > 2 else '')
         for k, v in (project.get('ir_rails', {}) or {}).items()}
ESCAPE_MM = 3.0     # a track may be narrow this close to its own pad (as in audit_widths.py)
PLANES = project.plane_nets()
# Nominal rail voltage - to judge droop in percent rather than millivolts: 228 mV on 20 V does
# not matter, on 3.8 V it is 6 % and already a question.
NOMINAL = {k: float(v) for k, v in (project.get('rail_nominal_v', {}) or {}).items()}
DROOP_PCT = 5.0      # droop above this needs review
# Datasheet minimum at the load pins, where known. Droop alone decides nothing: 6 % of 3.8 V is
# fine if the load minimum is 3.4 V.
VMIN = {k: (float(v[0]), str(v[1]) if len(v) > 1 else '')
        for k, v in (project.get('rail_vmin', {}) or {}).items()}


def need_width_mm(current, t_m, external):
    """Required width per IPC-2221 for a given copper thickness."""
    k = 0.048 if external else 0.024
    a_mils2 = (current / (k * DT ** 0.44)) ** (1 / 0.725)
    a_m2 = a_mils2 * (25.4e-6 ** 2)
    return a_m2 / t_m * 1000.0


def seg_r(length_mm, width_mm, t_m):
    if width_mm <= 0 or length_mm <= 0:
        return 1e-9
    return RHO * (length_mm / 1000.0) / (width_mm / 1000.0 * t_m)


def via_r(drill_mm, height_m=1.6e-3):
    """Barrel resistance: a cylindrical wall PLATING thick."""
    circ = math.pi * (drill_mm / 1000.0)
    return RHO * height_m / (circ * PLATING)


def key(x, y):
    return (round(x, 2), round(y, 2))


def main():
    brd = sys.argv[1] if len(sys.argv) > 1 else paths.BOARD
    board = pcbnew.LoadBoard(brd)
    outer = {board.GetLayerName(board.GetLayerID(n)) for n in ('F.Cu', 'B.Cu')}

    print('copper thickness: outer %.0f um, inner %.1f um, temperature rise %g deg'
          % (T_OUTER * 1e6, T_INNER * 1e6, DT))
    print('\n=== required width per IPC-2221 ===')
    for rail, (cur, peak, src) in sorted(RAILS.items(), key=lambda kv: -kv[1][0]):
        pk = '' if peak is None else ', peak %.2f A' % peak
        print('  %-11s %5.2f A%-12s outer %.2f mm, inner %.2f mm   (%s)'
              % (rail, cur, pk, need_width_mm(cur, T_OUTER, True),
                 need_width_mm(cur, T_INNER, False), src))

    # copper per net
    tracks, vias = {}, {}
    for t in board.GetTracks():
        b = t.GetNetname().split('/')[-1]
        if b not in RAILS:
            continue
        if t.GetClass() == 'PCB_VIA':
            vias.setdefault(b, []).append(t.Cast())
        else:
            tracks.setdefault(b, []).append(t)
    pads = {}
    for fp in board.GetFootprints():
        for p in fp.Pads():
            b = p.GetNetname().split('/')[-1]
            if b in RAILS:
                pads.setdefault(b, []).append((fp.GetReference(), p.GetNumber(),
                                               p.GetPosition().x / MM,
                                               p.GetPosition().y / MM,
                                               p.GetDrillSizeX() > 0))

    print('\n=== actual copper and bottlenecks ===')
    problems = []
    for rail, (cur, peak, _) in sorted(RAILS.items(), key=lambda kv: -kv[1][0]):
        tk = tracks.get(rail, [])
        if not tk:
            print('  %-11s no copper - net not routed' % rail)
            continue
        widths = []
        for t in tk:
            ln = board.GetLayerName(t.GetLayer())
            ext = ln in outer
            w = t.GetWidth() / MM
            widths.append((w, ln, ext, t.GetLength() / MM))
        # A narrow segment at its own pad is an escape and may neck down: a 0.5 mm-pitch
        # package cannot be left any wider. Only the copper of the run between pads counts.
        own_pads = [(x, y) for _, _, x, y, _ in pads.get(rail, [])]
        def is_escape(t):
            for pt in (t.GetStart(), t.GetEnd()):
                px, py = pt.x / MM, pt.y / MM
                for ox, oy in own_pads:
                    if (px - ox) ** 2 + (py - oy) ** 2 <= ESCAPE_MM ** 2:
                        return True
            return False
        runs = [(w, ln, e, L, t) for (w, ln, e, L), t in zip(widths, tk)
                if not is_escape(t)]
        total = sum(z[3] for z in widths)
        wmin_all = min(widths, key=lambda z: z[0])[0]
        if not runs:
            print('  %-11s segments %3d, %6.1f mm; all copper is pad escapes'
                  ' (narrowest %.2f mm)' % (rail, len(tk), total, wmin_all))
        else:
            wmin, lname, ext, _, _ = min(runs, key=lambda z: z[0])
            need = need_width_mm(cur, T_OUTER if ext else T_INNER, ext)
            flag = '' if wmin >= need else '  <-- NARROW: need %.2f mm' % need
            print('  %-11s segments %3d, %6.1f mm; narrowest on the run %.2f mm on %s'
                  ' (with escapes %.2f)%s'
                  % (rail, len(tk), total, wmin, lname, wmin_all, flag))
            if wmin < need:
                problems.append('%s: run %.2f mm where %.2f mm is required on %s'
                                % (rail, wmin, need, lname))

        # graph and drop from the source to the farthest load
        g = {}
        def add(a, b, r):
            g.setdefault(a, []).append((b, r))
            g.setdefault(b, []).append((a, r))
        for t in tk:
            ln = board.GetLayerName(t.GetLayer())
            tm = T_OUTER if ln in outer else T_INNER
            a = key(t.GetStart().x / MM, t.GetStart().y / MM)
            b = key(t.GetEnd().x / MM, t.GetEnd().y / MM)
            add(a, b, seg_r(t.GetLength() / MM, t.GetWidth() / MM, tm))
        for v in vias.get(rail, []):
            k1 = key(v.GetPosition().x / MM, v.GetPosition().y / MM)
            add(k1, ('v',) + k1, via_r(v.GetDrill() / MM))
        # pads snap to the nearest graph node within 0.3 mm
        nodes = [n for n in g if not isinstance(n[0], str)]
        pin_nodes = {}
        for ref, num, x, y, th in pads.get(rail, []):
            best = None
            for n in nodes:
                d = (n[0] - x) ** 2 + (n[1] - y) ** 2
                if best is None or d < best[0]:
                    best = (d, n)
            if best and best[0] <= 0.09:
                pin_nodes[(ref, num)] = best[1]
        if len(pin_nodes) < 2:
            print('               graph: %d connected pins - drop not computed'
                  % len(pin_nodes))
            continue
        src = sorted(pin_nodes)[0]
        dist = {pin_nodes[src]: 0.0}
        pq = [(0.0, pin_nodes[src])]
        while pq:
            d, n = heapq.heappop(pq)
            if d > dist.get(n, 1e18):
                continue
            for m, r in g.get(n, ()):
                nd = d + r
                if nd < dist.get(m, 1e18):
                    dist[m] = nd
                    heapq.heappush(pq, (nd, m))
        worst = None
        for pin, n in pin_nodes.items():
            if pin == src:
                continue
            r = dist.get(n)
            if r is None:
                continue
            if worst is None or r > worst[0]:
                worst = (r, pin)
        if worst:
            drop = worst[0] * cur
            line = ('               worst path %s.%s -> %s.%s: %.1f mohm, drop %.0f mV'
                    ' at %.2f A' % (src[0], src[1], worst[1][0], worst[1][1],
                                     worst[0] * 1000, drop * 1000, cur))
            if peak:
                line += '; at peak %.2f A droop %.0f mV' % (peak, worst[0] * peak * 1000)
            print(line)
            nom = NOMINAL.get(rail)
            if nom:
                vm = VMIN.get(rail)
                for label, amps in (('working', cur), ('peak', peak or 0)):
                    if not amps:
                        continue
                    pct = worst[0] * amps / nom * 100
                    if pct <= DROOP_PCT:
                        continue
                    if vm:
                        at_load = nom - worst[0] * amps
                        margin = at_load - vm[0]
                        print('               at %s current the load pins see %.3f V,'
                              ' minimum %.2f V, margin %.0f mV  (%s)'
                              % (label, at_load, vm[0], margin * 1000, vm[1]))
                        if margin > 0:
                            continue      # droop is large, but within the datasheet minimum
                        problems.append('%s: %.3f V at the pins with minimum %.2f V'
                                        % (rail, at_load, vm[0]))
                    else:
                        problems.append(
                            '%s: droop %.1f %% of %.1f V at %s current %.2f A'
                            % (rail, pct, nom, label, amps))

    print('\nRESULT: %s' % ('no findings' if not problems
                          else 'findings %d' % len(problems)))
    for p in problems:
        print('   ', p)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
