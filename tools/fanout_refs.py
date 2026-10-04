#!/usr/bin/env python3
"""List the footprints involved in unconnected items - input for the fan-out.

A fan-out is better built not from a fixed list but from the footprints whose pins are
currently unconnected: the board changes, and after each routing wave free via spots
appear where there were none before. In practice a first pass around the main MCU
placed ~115 vias, a second pass over ~17 footprints another ~70, and a third pass over
the footprints involved in unconnected items another ~25.
"""
import sys, os, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths, drcinfo as D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--min-pads', type=int, default=1)
    a = ap.parse_args()
    d = D.load(a.drc)
    refs = {}
    for net, kinds, dist, rr, pos in D.breaks(d):
        if 'Zone' in kinds:
            continue
        for r in (rr or ()):
            refs[r] = refs.get(r, 0) + 1
    out = [r for r, n in sorted(refs.items(), key=lambda z: -z[1]) if n >= a.min_pads]
    print(','.join(out))


if __name__ == '__main__':
    main()
