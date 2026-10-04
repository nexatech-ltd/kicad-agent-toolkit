"""Net geometry: copper per layer, vias, pads and detour ratio.

Usage: python3 tools/util/net_geom.py BOARD.kicad_pcb NET [NET ...]

For each net prints the total copper length, a per-layer breakdown with widths, the via count,
pad coordinates, the straight line between the two farthest pads and the detour (copper length
relative to that line). Useful for spotting a feed or bus that wanders far off its direct path.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, math, pcbnew
MM = 1e6
b = pcbnew.LoadBoard(sys.argv[1])
want = sys.argv[2:]
short = lambda s: s.split('/')[-1]

pads = {}
for fp in b.GetFootprints():
    for pd in fp.Pads():
        n = short(pd.GetNetname())
        if n in want:
            p = pd.GetPosition()
            pads.setdefault(n, []).append((fp.GetReference() + '.' + pd.GetNumber(), p.x / MM, p.y / MM))

segs, vias = {}, {}
for t in b.GetTracks():
    n = short(t.GetNetname())
    if n not in want: continue
    if t.GetClass() == 'PCB_VIA':
        p = t.GetPosition()
        vias.setdefault(n, []).append((p.x / MM, p.y / MM, t.GetWidth() / MM, t.GetDrill() / MM))
    else:
        s, e = t.GetStart(), t.GetEnd()
        segs.setdefault(n, []).append((b.GetLayerName(t.GetLayer()),
                                       s.x / MM, s.y / MM, e.x / MM, e.y / MM, t.GetWidth() / MM))

for n in want:
    S, V, P = segs.get(n, []), vias.get(n, []), pads.get(n, [])
    per = {}
    for lay, x0, y0, x1, y1, w in S:
        per.setdefault(lay, [0.0, set()])
        per[lay][0] += math.hypot(x1 - x0, y1 - y0); per[lay][1].add(round(w, 3))
    tot = sum(v[0] for v in per.values())
    far = (0, None, None)
    for i in range(len(P)):
        for j in range(i + 1, len(P)):
            d = math.hypot(P[i][1] - P[j][1], P[i][2] - P[j][2])
            if d > far[0]: far = (d, P[i], P[j])
    print('=== %s' % n)
    print('  copper %.1f mm, segments %d, vias %d, pads %d' % (tot, len(S), len(V), len(P)))
    for lay in sorted(per, key=lambda k: -per[k][0]):
        print('    %-6s %7.1f mm  widths %s' % (lay, per[lay][0], sorted(per[lay][1])))
    for r, x, y in P: print('    pad %-10s (%7.2f, %6.2f)' % (r, x, y))
    if far[1]:
        print('    straight %s->%s = %.1f mm, detour %.0f %%' %
              (far[1][0], far[2][0], far[0], 100 * (tot / far[0] - 1) if far[0] else 0))
    if V:
        print('    vias: ' + ', '.join('(%.2f,%.2f)%.2f/%.2f' % v for v in V))
