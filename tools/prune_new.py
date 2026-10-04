#!/usr/bin/env python3
"""Remove objects added since a given board copy that ended up in violations.

A generalisation of the technique that converged for the fan-out. Board rules live in
three places - net classes, `.kicad_dru` (often only track-track and track-pad pairs)
and the board setup - and reproducing all of them in a raster mask is costly and
fragile: e.g. `fanout.py` produced about one violation per seven placements.

So a tool lays copper by calculation and DRC has the last word: objects that appeared
in this run and ended up in error-severity violations are removed. The loop converges
by construction - only what DRC accepted stays on the board.

Own objects are recognised by uuid: whatever was not in the copy.
"""
import sys, os, re, math, argparse, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp, paths, drcinfo as D


def uuids(txt):
    out = {}
    for tag in ('segment', 'arc', 'via'):
        for i, j in sexp.blocks(txt, tag):
            blk = txt[i:j]
            u = re.search(r'\(uuid "([^"]+)"\)', blk)
            if u:
                out[u.group(1)] = (tag, i, j, blk)
    return out


def near_seg(hx, hy, pts, r):
    """Violation point near an object: for a segment, the distance to the segment ITSELF.

    Checking only the endpoints let a long track that shorts a pad with its middle escape
    removal: DRC reports a point on the track while its ends are far away, and the
    violations survive the prune.
    """
    if len(pts) >= 2:
        (ax, ay), (bx, by) = pts[0], pts[-1]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((hx - ax) * dx + (hy - ay) * dy) / L2))
        if math.hypot(hx - (ax + t * dx), hy - (ay + t * dy)) < r:
            return True
    return any(math.hypot(hx - x, hy - y) < r for x, y in pts)


def coords(blk):
    pts = []
    for key in ('at', 'start', 'end'):
        for sb in sexp.sub(blk, key):
            v = sexp.parse(sb)[0]
            if len(v) >= 3:
                try:
                    pts.append((float(v[1]), float(v[2])))
                except ValueError:
                    pass
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--since', required=True, help='board copy taken before the run')
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--radius', type=float, default=0.9)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    old = uuids(open(a.since).read())
    txt = open(a.board).read()
    new = uuids(txt)
    added = {u: v for u, v in new.items() if u not in old}
    print('objects added since the copy: %d' % len(added))

    drc = D.load(a.drc)
    hot = []
    nerr = 0
    for v in drc['violations']:
        if v.get('severity') != 'error':
            continue
        nerr += 1
        for i in v.get('items', []):
            m = re.search(r'\[([^\]]+)\]', i.get('description', ''))
            p = i.get('pos') or {}
            if m and 'x' in p:
                hot.append((m.group(1), p['x'], p['y']))
    print('error-severity violations: %d' % nerr)

    victims = []
    for u, (tag, i, j, blk) in added.items():
        nb = sexp.sub(blk, 'net')
        if not nb:
            continue
        net = sexp.parse(nb[0])[0][1]
        pts = coords(blk)
        for hnet, hx, hy in hot:
            if hnet != net:
                continue
            if near_seg(hx, hy, pts, a.radius):
                victims.append((u, tag, net, pts[0]))
                break
    print('to remove: %d objects' % len(victims))
    for u, tag, net, p in victims:
        print('   %-8s %-28s (%.2f, %.2f)' % (tag, net.split('/')[-1], p[0], p[1]))
    if not victims:
        return 0
    # delete from the end so the offsets stay valid
    spans = sorted(((new[u][1], new[u][2]) for u, _, _, _ in victims), reverse=True)
    for i, j in spans:
        txt = txt[:i - 1] + txt[j:]
    if a.apply:
        shutil.copy(a.board, a.board + '.bakPruneNew')
        open(a.board, 'w').write(txt)
        print('written, backup .bakPruneNew')
    else:
        print('dry run, --apply to write')


if __name__ == '__main__':
    main()
