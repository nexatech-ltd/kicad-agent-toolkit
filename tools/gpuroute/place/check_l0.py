#!/usr/bin/env python3
"""Checkpoint B: L0 (locked ends) of a pocket model at the current placement.

    python3 tools/gpuroute/place/check_l0.py --refs U1,U2
"""
import argparse
import collections
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import pcbnew
import torch

import paths
import raster
import escape as E


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--geom', default=paths.work('place', 'geom.json'))
    ap.add_argument('--refs', required=True, help='comma-separated ICs of the pocket, e.g. U1,U2')
    ap.add_argument('--ref', default=None, help='locked.json reference from locked.py to compare against (ref, pad)')
    a = ap.parse_args()
    board = pcbnew.LoadBoard(a.board)
    g = raster.Grid(board, step=0.1)
    geom = json.load(open(a.geom))
    pk = E.Pocket(board, a.board, g, geom, a.refs.split(','))
    t = time.time()
    cm, pads = pk.model_for({})
    torch.cuda.synchronize()
    print('candidate model %.3f s, pads %d' % (time.time() - t, len(pads)))
    ends = pk.ends_of({}, refs=pk.ic_refs)
    t = time.time()
    locked = pk.l0(cm, ends)
    torch.cuda.synchronize()
    print('L0 ICs: ends %d, locked %d in %.3f s' % (len(ends), len(locked), time.time() - t))
    print('  by footprint:', dict(collections.Counter(l[3] for l in locked)))
    print('  list:', sorted((l[3], l[4], l[0].split('/')[-1][:12], l[5]) for l in locked))
    if a.ref and os.path.exists(a.ref):
        refset = set()
        for item in json.load(open(a.ref)):
            for (what, x, y, la, reach) in item['ends']:
                parts = what.split()
                if reach < E.LOCKED_CELLS and parts[0] == 'Pad' and parts[-1] in pk.ic_refs:
                    refset.add((parts[-1], parts[1]))
        l0set = {(l[3], l[4]) for l in locked}
        print('locked.py reference (%s): %d locked IC pads: %s' % (a.ref, len(refset), sorted(refset)))
        print('  L0 and reference agree: %d; L0 only: %s; reference only: %s'
              % (len(l0set & refset), sorted(l0set - refset), sorted(refset - l0set)))
    ends2 = pk.ends_of({}, refs=pk.movable)
    locked2 = pk.l0(cm, ends2)
    print('L0 support parts: ends %d, locked %d: %s' % (len(ends2), len(locked2), sorted((l[3], l[4]) for l in locked2)[:20]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
