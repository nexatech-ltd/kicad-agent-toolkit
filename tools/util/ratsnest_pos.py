"""Locate the exact break in a net.

Usage: python3 tools/util/ratsnest_pos.py BOARD.kicad_pcb NET [NET ...]

Refills zones, builds KiCad connectivity, splits the net's copper into clusters with
CONNECTIVITY_DATA.GetConnectedItems (zone fills included) and prints every cluster except
the main one. DRC "Zone <-> Zone" unconnected items carry the zone's anchor point, not the
location of the break; this tool lists the real items of the cut-off pieces.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, pcbnew
bp = sys.argv[1]; want = set(sys.argv[2:])
b = pcbnew.LoadBoard(bp); MM = 1e6
filler = pcbnew.ZONE_FILLER(b); filler.Fill(b.Zones())
b.BuildConnectivity(); conn = b.GetConnectivity()
print('unconnected (KiCad):', conn.GetUnconnectedCount(True))
def desc(it):
    c = it.GetClass(); p = it.GetPosition()
    if c == 'PCB_VIA': return 'via (%.2f,%.2f)' % (p.x / MM, p.y / MM)
    if c == 'PCB_TRACK':
        s, e = it.GetStart(), it.GetEnd(); return 'trk %s (%.2f,%.2f)-(%.2f,%.2f)' % (b.GetLayerName(it.GetLayer()), s.x / MM, s.y / MM, e.x / MM, e.y / MM)
    if c == 'PAD': return 'pad %s.%s (%.2f,%.2f)' % (it.GetParentFootprint().GetReference(), it.GetNumber(), p.x / MM, p.y / MM)
    return '%s (%.2f,%.2f)' % (c, p.x / MM, p.y / MM)
for net in sorted(want):
    items = [t for t in b.GetTracks() if t.GetNetname().split('/')[-1] == net]
    items += [pd for fp in b.GetFootprints() for pd in fp.Pads() if pd.GetNetname().split('/')[-1] == net]
    U = lambda x: x.m_Uuid.AsString()   # SWIG returns fresh wrappers: compare by uuid, not id()
    byu = {U(it): it for it in items}
    seen = set(); clusters = []
    for it in items:
        if U(it) in seen: continue
        cl = [x for x in conn.GetConnectedItems(it) if x.GetClass() != 'ZONE']
        members = {U(x) for x in cl} | {U(it)}
        seen |= members
        clusters.append([byu[u] for u in members if u in byu])
    clusters.sort(key=len, reverse=True)
    print('%s: items %d, clusters %d, main cluster %d items' % (net, len(items), len(clusters), len(clusters[0]) if clusters else 0))
    for cl in clusters[1:]:
        print('  piece of %d:' % len(cl))
        for x in cl[:12]: print('     ', desc(x))
        if len(cl) > 12: print('      ...')
