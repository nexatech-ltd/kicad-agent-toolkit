"""Duplicate a footprint's keepout (rule-area) zones as board-level zones.

Usage: python3 tools/util/mirror_fp_keepouts.py BOARD.kicad_pcb U1 [J1 ...] [--apply]
       python3 tools/util/mirror_fp_keepouts.py BOARD.kicad_pcb --remove [--apply]

The GPU router rasterises only board-level zones, so keepouts defined inside a footprint
(e.g. under a card-slot contact area) are invisible to it and it happily routes through them
(DRC: items_not_allowed). Copies are named TMPKO_<REF>_<n>; remove them with --remove after
routing. Without --apply it is a dry run.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, pcbnew
MM = 1e6
bp = sys.argv[1]; apply = '--apply' in sys.argv
b = pcbnew.LoadBoard(bp)
if '--remove' in sys.argv:
    gone = [z for z in b.Zones() if z.GetZoneName().startswith('TMPKO_')]
    for z in gone: b.Remove(z)
    print('temporary keepouts removed: %d' % len(gone))
else:
    refs = [a for a in sys.argv[2:] if not a.startswith('--')]
    n = 0
    for fp in b.GetFootprints():
        if fp.GetReference() not in refs: continue
        for i, z in enumerate(fp.Zones()):
            if not z.GetIsRuleArea(): continue
            z2 = pcbnew.ZONE(b)
            z2.SetLayerSet(z.GetLayerSet())
            z2.SetIsRuleArea(True)
            z2.SetDoNotAllowTracks(z.GetDoNotAllowTracks())
            z2.SetDoNotAllowVias(z.GetDoNotAllowVias())
            z2.SetDoNotAllowPads(z.GetDoNotAllowPads())
            z2.SetDoNotAllowZoneFills(z.GetDoNotAllowZoneFills())
            src = z.Outline()
            pts = pcbnew.VECTOR_VECTOR2I()
            for k in range(src.OutlineCount()):
                ol = src.Outline(k)
                for j in range(ol.PointCount()):
                    pts.push_back(pcbnew.VECTOR2I(ol.CPoint(j).x, ol.CPoint(j).y))
                break                       # a keepout has a single outline
            z2.AddPolygon(pts)
            z2.SetZoneName('TMPKO_%s_%d' % (fp.GetReference(), i))
            b.Add(z2); n += 1
            bb = z2.GetBoundingBox()
            print('  copy %s: (%.2f,%.2f)-(%.2f,%.2f)' % (z2.GetZoneName(), bb.GetLeft()/MM, bb.GetTop()/MM, bb.GetRight()/MM, bb.GetBottom()/MM))
    print('temporary keepouts created: %d' % n)
if apply:
    pcbnew.SaveBoard(bp, b); print('written', bp)
else:
    print('dry run')
