#!/usr/bin/env python3
"""Refill all zones and save the board (pcbnew ZONE_FILLER + SaveBoard) in a separate process.

Why: kicad-cli drc --refill-zones fills in memory and never writes the file, while verify.py reads
the fill polygons from the file. After text edits (stitch_gnd, widen_net, close_gaps) the stored
fills are stale and the "nothing lies inside a foreign fill" check reports hundreds of false hits
(they all disappear after a refill). Do not mix with text edits in one process; one LoadBoard here.

    python3 tools/refill_zones.py --board B
"""
import argparse, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths
import pcbnew
ap = argparse.ArgumentParser(); ap.add_argument('--board', default=paths.BOARD); a = ap.parse_args()
t = time.time(); b = pcbnew.LoadBoard(a.board)
f = pcbnew.ZONE_FILLER(b); f.Fill(b.Zones()); pcbnew.SaveBoard(a.board, b)
print('%d zones refilled and saved in %.0f s: %s' % (len(b.Zones()), time.time() - t, a.board))
