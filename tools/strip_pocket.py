#!/usr/bin/env python3
"""Remove the copper of the given nets inside a window - to free a spot that matters more.

Typical use: a switching-regulator loop. A pocket next to some MCU pins is free by
courtyard but taken by a via of a low-current analog supply 0.25 mm from the centre,
so placement rightly refuses. The low-current rail can be re-routed by the router;
a fast switching node has nowhere else to go.
"""
import sys, os, argparse, math, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp, paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--nets', required=True, help='comma-separated short net names')
    ap.add_argument('--box', required=True, help='x0,y0,x1,y1 in mm')
    ap.add_argument('--max-len', type=float, default=None,
                    help='remove only segments shorter than this, mm - targets package '
                         'escapes without touching long runs')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    x0, y0, x1, y1 = [float(v) for v in a.box.split(',')]
    want = {s.strip() for s in a.nets.split(',') if s.strip()}
    txt = open(a.board).read()
    n = 0
    for tag in ('via', 'segment', 'arc'):
        again = True
        while again:
            again = False
            for i, j in sexp.blocks(txt, tag):
                blk = txt[i:j]
                nb = sexp.sub(blk, 'net')
                if not nb:
                    continue
                nm = sexp.parse(nb[0])[0]
                if len(nm) < 2 or str(nm[1]).split('/')[-1] not in want:
                    continue
                if a.max_len is not None and tag != 'via':
                    sp = sexp.sub(blk, 'start')
                    ep = sexp.sub(blk, 'end')
                    if sp and ep:
                        v1 = sexp.parse(sp[0])[0]
                        v2 = sexp.parse(ep[0])[0]
                        try:
                            ln = math.hypot(float(v2[1]) - float(v1[1]),
                                            float(v2[2]) - float(v1[2]))
                        except (ValueError, IndexError):
                            ln = 0.0
                        if ln > a.max_len:
                            continue
                hit = False
                for key in ('at', 'start', 'end'):
                    for sb in sexp.sub(blk, key):
                        v = sexp.parse(sb)[0]
                        if len(v) < 3:
                            continue
                        try:
                            x, y = float(v[1]), float(v[2])
                        except ValueError:
                            continue
                        if x0 <= x <= x1 and y0 <= y <= y1:
                            hit = True
                if hit:
                    txt = txt[:i - 1] + txt[j:]
                    n += 1
                    again = True
                    break
    print('copper objects removed: %d (nets %s, window %s)' % (n, ','.join(sorted(want)), a.box))
    if a.apply:
        shutil.copy(a.board, a.board + '.bakPocket')
        open(a.board, 'w').write(txt)
        print('written, backup .bakPocket')
    else:
        print('dry run, --apply to write')


if __name__ == '__main__':
    main()
