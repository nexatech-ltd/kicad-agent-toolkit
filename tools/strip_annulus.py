#!/usr/bin/env python3
"""Remove a package's copper in a ring around its pads - to make room for a systematic fan-out.

Motivation. On a large fine-pitch MCU, more than half of the signal pins can have no
room for a via, and the main cause is COPPER - yet foreign transit in the ring is
only a handful of segments. The rest are the package's own escapes, laid at an angle
by earlier routing campaigns: they occupy the ring and lock the pins that have no
escape at all.

So the ring is removed entirely and rebuilt by the rules. Long runs beyond the ring
stay: their ends will be 3..4 mm from the new vias, and a short gap is exactly what
the maze router closes reliably.
"""
import sys, os, math, argparse, shutil, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp, paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--ref', required=True, help='footprint reference, e.g. U1')
    ap.add_argument('--radius', type=float, default=2.6,
                    help='ring radius from the pad centres, mm')
    ap.add_argument('--own-only', action='store_true', default=True,
                    help='remove only copper of nets that connect to this footprint')
    ap.add_argument('--any', action='store_true',
                    help='remove copper with ANY point in the ring, not only copper entirely '
                         'inside: otherwise a run crossing the ring stays and locks pins '
                         '(observed: F.Cu still blocked dozens of pins after "entirely inside" removal)')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    import pcbnew
    b = pcbnew.LoadBoard(a.board)
    fp = {f.GetReference(): f for f in b.GetFootprints()}[a.ref]
    pxy = [(p.GetPosition().x / 1e6, p.GetPosition().y / 1e6) for p in fp.Pads()]
    own = {p.GetNetname() for p in fp.Pads() if p.GetNetname()}

    def d2ring(x, y):
        return min(math.hypot(x - px, y - py) for px, py in pxy)

    txt = open(a.board).read()
    kill = []
    stat = collections.Counter()
    for tag in ('segment', 'arc', 'via'):
        for i, j in sexp.blocks(txt, tag):
            blk = txt[i:j]
            nb = sexp.sub(blk, 'net')
            if not nb:
                continue
            net = sexp.parse(nb[0])[0][1]
            if a.own_only and net not in own:
                continue
            pts = []
            for key in ('at', 'start', 'end'):
                for sb in sexp.sub(blk, key):
                    v = sexp.parse(sb)[0]
                    if len(v) >= 3:
                        try:
                            pts.append((float(v[1]), float(v[2])))
                        except ValueError:
                            pass
            if not pts:
                continue
            # segment in the ring: entirely, or (with --any) at least partly.
            # Points along the segment are checked, not only its ends:
            # a long run can cross the ring without either end inside it.
            if a.any and len(pts) >= 2:
                inside = False
                steps = max(2, int(math.hypot(pts[-1][0] - pts[0][0],
                                              pts[-1][1] - pts[0][1]) / 0.2) + 1)
                for k in range(steps + 1):
                    X = pts[0][0] + (pts[-1][0] - pts[0][0]) * k / steps
                    Y = pts[0][1] + (pts[-1][1] - pts[0][1]) * k / steps
                    if d2ring(X, Y) <= a.radius:
                        inside = True
                        break
            else:
                inside = all(d2ring(x, y) <= a.radius for x, y in pts)
            if inside:
                kill.append((i, j))
                stat[tag] += 1
                stat['length'] += (math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])
                                  if tag != 'via' else 0)
    print('in the %.1f mm ring around %s: segments %d, arcs %d, vias %d, length %.1f mm'
          % (a.radius, a.ref, stat['segment'], stat['arc'], stat['via'], stat['length']))
    for i, j in sorted(kill, reverse=True):
        txt = txt[:i - 1] + txt[j:]
    print('objects removed: %d' % len(kill))
    if a.apply:
        shutil.copy(a.board, a.board + '.bakAnnulus')
        open(a.board, 'w').write(txt)
        print('written, backup .bakAnnulus')
    else:
        print('dry run, --apply to write')


if __name__ == '__main__':
    main()
