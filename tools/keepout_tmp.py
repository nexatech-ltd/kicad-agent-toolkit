#!/usr/bin/env python3
"""Temporary keepout for the router: add a rectangle, route, remove it.

Needed when a project policy is wider than the board rules. Example: a policy forbids
signal copper in the corridor of an antenna feed, but the corridor is a band along the RF
tracks, not a board object. The router does not know the policy and can keep placing a via
0.2 mm from the feed; check_rf_reference.py only catches it afterwards.

    python3 tools/keepout_tmp.py --board B --box x0,y0,x1,y1 --name RF_TMP --add
    python3 tools/keepout_tmp.py --board B --name RF_TMP --remove
"""
import argparse
import sys

import pcbnew

MM = 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--name', default='TMP_KEEPOUT')
    ap.add_argument('--box', default=None, help='x0,y0,x1,y1')
    ap.add_argument('--add', action='store_true')
    ap.add_argument('--remove', action='store_true')
    a = ap.parse_args()

    b = pcbnew.LoadBoard(a.board)
    if a.remove:
        gone = 0
        for z in list(b.Zones()):
            if z.GetZoneName() == a.name:
                b.Remove(z)
                gone += 1
        print('removed zones "%s": %d' % (a.name, gone))
    if a.add:
        x0, y0, x1, y1 = (float(v) for v in a.box.split(','))
        z = pcbnew.ZONE(b)
        z.SetIsRuleArea(True)
        z.SetDoNotAllowTracks(True)
        z.SetDoNotAllowVias(True)
        z.SetDoNotAllowZoneFills(False)
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(False)
        z.SetZoneName(a.name)
        ls = pcbnew.LSET()
        for lid in pcbnew.LSET.AllCuMask().CuStack():   # every enabled copper layer
            if b.IsLayerEnabled(lid):
                ls.addLayer(lid)
        z.SetLayerSet(ls)
        o = z.Outline()
        o.NewOutline()
        for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            o.Append(int(px * MM), int(py * MM))
        b.Add(z)
        print('added keepout "%s" %s on all copper layers' % (a.name, a.box))
    pcbnew.SaveBoard(a.board, b)
    print('board saved')
    return 0


if __name__ == '__main__':
    sys.exit(main())
