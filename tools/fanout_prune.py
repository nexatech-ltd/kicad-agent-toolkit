#!/usr/bin/env python3
"""Remove the fan-out escapes that DRC pointed at.

The fan-out mask is computed from the rules, but board rules live in three places -
net classes, `.kicad_dru` (often only track-track and track-pad pairs) and the board
setup - and reproducing them fully in a raster is costly and fragile. So the fan-out
is laid by calculation and DRC has the last word: fan-out objects involved in
error-severity violations are removed together with their escape+via pair.

The loop thus converges by construction: however many rules the mask misses, only
what DRC accepted stays on the board.
"""
import sys, os, re, json, math, argparse, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp, paths, drcinfo as D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--fanout', default=paths.work('fanout.json'))
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    made = json.load(open(a.fanout))
    # key: (net, via position) - identifies the objects this tool placed
    mine = {}
    for pn, net, px, py, X, Y, d in made:
        mine[(net, round(X, 3), round(Y, 3))] = (pn, px, py)

    drc = D.load(a.drc)
    # positions of objects in error-severity violations
    hot = []
    for v in drc['violations']:
        if v.get('severity') != 'error':
            continue
        for i in v.get('items', []):
            m = re.search(r'\[([^\]]+)\]', i.get('description', ''))
            p = i.get('pos') or {}
            if m and 'x' in p:
                hot.append((m.group(1), p['x'], p['y']))

    victims = []
    for (net, X, Y), (pn, px, py) in mine.items():
        for hnet, hx, hy in hot:
            if hnet != net:
                continue
            # violation object near our via or our escape
            if (math.hypot(hx - X, hy - Y) < 1.2
                    or math.hypot(hx - px, hy - py) < 1.2):
                victims.append((net, X, Y, px, py, pn))
                break
    print('error-severity violations: %d, fan-out objects to remove: %d'
          % (sum(1 for v in drc['violations'] if v.get('severity') == 'error'),
             len(victims)))
    for net, X, Y, px, py, pn in victims:
        print('   %-28s pin %-4s via (%.2f, %.2f)' % (net.split('/')[-1], pn, X, Y))

    if not victims:
        return 0
    txt = open(a.board).read()
    n = 0
    for net, X, Y, px, py, pn in victims:
        for tag in ('via', 'segment'):
            again = True
            while again:
                again = False
                for i, j in sexp.blocks(txt, tag):
                    blk = txt[i:j]
                    nb = sexp.sub(blk, 'net')
                    if not nb or sexp.parse(nb[0])[0][1] != net:
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
                    hit = any(math.hypot(x - X, y - Y) < 0.02 for x, y in pts)
                    if tag == 'segment':
                        hit = hit and any(math.hypot(x - px, y - py) < 0.02 for x, y in pts)
                    if hit:
                        txt = txt[:i - 1] + txt[j:]
                        n += 1
                        again = True
                        break
    print('objects removed: %d' % n)
    if a.apply:
        shutil.copy(a.board, a.board + '.bakPrune')
        open(a.board, 'w').write(txt)
        print('written, backup .bakPrune')
    else:
        print('dry run, --apply to write')


if __name__ == '__main__':
    main()
