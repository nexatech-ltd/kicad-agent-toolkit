#!/usr/bin/env python3
"""Remove footprints that are no longer in the schematic, together with the copper of nets
that are left without a single pad. Without --apply only a plan is printed.

    python3 tools/remove_refs.py --refs U5,FB2,C60,C61,R50,TP19
    python3 tools/remove_refs.py --refs ... --apply
"""
import argparse
import os
import shutil
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project

ap = argparse.ArgumentParser()
ap.add_argument('--board', default=paths.BOARD)
ap.add_argument('--refs', required=True, help='comma-separated reference designators')
ap.add_argument('--keep-nets', default=','.join(sorted(project.plane_nets())),
                help='nets whose copper is kept even without pads (default: plane nets)')
ap.add_argument('--apply', action='store_true')
a = ap.parse_args()
refs = set(a.refs.split(','))
keep = set(a.keep_nets.split(','))

b = pcbnew.LoadBoard(a.board)
fps = {fp.GetReference(): fp for fp in b.GetFootprints()}
missing = refs - set(fps)
if missing:
    sys.exit('not on the board: %s' % ', '.join(sorted(missing)))

touched = {}          # net -> listed footprints that have it on a pad
for r in sorted(refs):
    fp = fps[r]
    p = fp.GetPosition()
    print('%-5s %-44s (%.2f, %.2f) %s' % (r, fp.GetFPIDAsString(), p.x / 1e6, p.y / 1e6,
                                          'B.Cu' if fp.IsFlipped() else 'F.Cu'))
    for pad in fp.Pads():
        n = pad.GetNetname()
        if n:
            touched.setdefault(n, set()).add(r)

remaining = {}        # net -> pad count on the remaining footprints
for fp in b.GetFootprints():
    if fp.GetReference() in refs:
        continue
    for pad in fp.Pads():
        n = pad.GetNetname()
        if n in touched:
            remaining[n] = remaining.get(n, 0) + 1
orphan = sorted(n for n in touched if remaining.get(n, 0) == 0 and n not in keep)
print('nets on the removed footprints: %d; left without pads: %s'
      % (len(touched), ', '.join(orphan) or '-'))

tracks = [t for t in b.GetTracks() if t.GetNetname() in orphan]
zones = [z for z in b.Zones() if z.GetNetname() in orphan]
nseg = sum(1 for t in tracks if t.Type() == pcbnew.PCB_TRACE_T)
nvia = sum(1 for t in tracks if t.Type() == pcbnew.PCB_VIA_T)
print('copper of orphaned nets: segments %d, vias %d, zones %d' % (nseg, nvia, len(zones)))
for n in sorted(touched):
    if n in keep or n in orphan:
        continue
    print('  keeps pads: %-30s pads %d (footprints %s)'
          % (n, remaining.get(n, 0), ','.join(sorted(touched[n]))))

if not a.apply:
    print('plan only, add --apply to write')
    sys.exit(0)

shutil.copy2(a.board, a.board + '.bakRemove')
for t in tracks:
    b.Remove(t)
for z in zones:
    b.Remove(z)
for r in refs:
    b.Remove(fps[r])
pcbnew.SaveBoard(a.board, b)
print('written: %s (removed footprints %d, segments %d, vias %d, zones %d)'
      % (a.board, len(refs), nseg, nvia, len(zones)))
