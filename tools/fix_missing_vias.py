#!/usr/bin/env python3
"""Break "same point, different layers": an orphaned stub left after a layer change.

The router changed layer but no via appeared (or dedup_vias later removed it as a
duplicate). DRC reports such a break as two items of one net at the SAME coordinate
on DIFFERENT layers, and the maze router cannot handle it: on a 0.1 mm grid both
ends fall into the same cell, there is no zero-length path, and it reports
"ends coincide".

Placing a via there usually does not work: such points tend to lie under fine-pitch
packages or in keepouts, and a single 0.6/0.3 via can create many violations.
So the SHORTER of the two stubs is removed: the break becomes an ordinary one with
separated ends and is closed by the router under the clearance rules.

Zones are skipped: DRC reports a fill fragment at the start of the zone outline, so
dozens of ground fragments look like "one point" and have nothing to do with vias;
stitch_gnd.py handles them.
"""
import sys, os, re, json, argparse, shutil, uuid as _uuid
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drcinfo as D, paths

VIA_SIZE, VIA_DRILL = 0.6, 0.3


def candidates(drc):
    out = []
    for v in drc.get('unconnected_items', []):
        it = v.get('items', [])
        if len(it) != 2:
            continue
        if any('Zone' in i.get('description', '') for i in it):
            continue
        p = [i.get('pos', {}) for i in it]
        if abs(p[0].get("x", 0) - p[1].get("x", 1)) > 0.15:
            continue
        if abs(p[0].get("y", 0) - p[1].get("y", 1)) > 0.15:
            continue
        lay = [re.search(r' on ([A-Za-z0-9_.]+)', i.get('description', '')) for i in it]
        net = re.search(r'\[([^\]]+)\]', it[0].get('description', ''))
        if not (net and lay[0] and lay[1]):
            continue
        if lay[0].group(1) == lay[1].group(1):
            continue
        ln = []
        for i in it:
            m = re.search(r'length ([0-9.]+)', i.get('description', ''))
            ln.append(float(m.group(1)) if m else 0.0)
        out.append((net.group(1), p[0]['x'], p[0]['y'],
                    lay[0].group(1), lay[1].group(1), ln))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=paths.work('maze_cur.json'))
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    drc = D.load(a.drc)
    cs = candidates(drc)
    print('missing vias: %d' % len(cs))
    import sexp, math
    txt = open(a.board).read()
    removed = 0
    for net, x, y, l1, l2, lens in cs:
        # the shorter of the two is the stub
        short_layer = l1 if lens[0] <= lens[1] else l2
        best = None
        for i, j in sexp.blocks(txt, 'segment'):
            blk = txt[i:j]
            nb = sexp.sub(blk, 'net')
            lb = sexp.sub(blk, 'layer')
            if not nb or not lb:
                continue
            if sexp.parse(nb[0])[0][1] != net:
                continue
            if sexp.parse(lb[0])[0][1] != short_layer:
                continue
            sp = sexp.parse(sexp.sub(blk, 'start')[0])[0]
            ep = sexp.parse(sexp.sub(blk, 'end')[0])[0]
            d = min(math.hypot(float(sp[1]) - x, float(sp[2]) - y),
                    math.hypot(float(ep[1]) - x, float(ep[2]) - y))
            ln = math.hypot(float(ep[1]) - float(sp[1]), float(ep[2]) - float(sp[2]))
            if d < 0.05 and (best is None or ln < best[2]):
                best = (i, j, ln)
        if best:
            txt = txt[:best[0] - 1] + txt[best[1]:]
            removed += 1
            print('   %-32s (%.3f, %.3f)  removed stub %s, %.3f mm'
                  % (net.split('/')[-1], x, y, short_layer, best[2]))
        else:
            print('   %-32s (%.3f, %.3f)  stub not found, left to the router'
                  % (net.split('/')[-1], x, y))
    print('stubs removed: %d' % removed)
    blocks = removed
    if a.apply and blocks:
        shutil.copy(a.board, a.board + '.bakVias')
        open(a.board, 'w').write(txt)
        print('written, backup .bakVias')
    elif not a.apply:
        print('dry run, use --apply to write')


if __name__ == '__main__':
    main()
