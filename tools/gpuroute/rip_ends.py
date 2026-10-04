#!/usr/bin/env python3
"""A series of small rip-ups at the ends of ALL remaining signal breaks (not only the "locked"
ones, as in rip_campaign): centres are break ends grouped within 3 mm, IC pins first; DRC judges
each step (fewer breaks with zero violations), otherwise roll back to .bakRipRound.

    python3 tools/gpuroute/rip_ends.py --rounds 40 --radius 4 --window 6
"""
import argparse
import collections
import math
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import drcinfo as D
import paths

LOG = paths.work('rip_ends.log')


def say(msg):
    line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def ends_of(drc):
    """[(x, y, description, priority, net)] - signal break ends; priority 0 for IC pads (refs starting with U)."""
    out = []
    for x in drc['unconnected_items']:
        its = x.get('items', [])
        if len(its) < 2:
            continue
        net = D._net_of(its[0]) or '?'
        if net.split('/')[-1] in D.PLANE_NETS:
            continue
        for it in its:
            m = re.match(r'Pad (\S+) \[.*\] of (\S+)', it['description'])
            pri = 0 if (m and m.group(2).startswith('U')) else 1
            out.append((it['pos']['x'], it['pos']['y'], (m.group(2) + '.' + m.group(1)) if m else it['description'][:30], pri, net))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--rounds', type=int, default=40)
    ap.add_argument('--radius', type=float, default=4.0)
    ap.add_argument('--window', type=float, default=6.0)
    ap.add_argument('--iters', type=int, default=8)
    ap.add_argument('--rip-planes', default='tracks')
    ap.add_argument('--group-mm', type=float, default=3.0)
    ap.add_argument('--slack', type=int, default=0,
                    help='accept a round even if the total count grows by up to +N, as long as signal breaks decrease (plane pads are fixed by fix_plane_pads/--planes; use with care, a signal net can get lost this way)')
    ap.add_argument('--only-box', default=None, help='x0,y0,x1,y1 - try only ends inside this box')
    ap.add_argument('--skip-box', default=None,
                    help='x0,y0,x1,y1 - do not try ends inside this box (e.g. a via desert under a large MCU where r=4 rip-ups gave nothing for 12 rounds in a row)')
    a = ap.parse_args()
    cur_path = paths.work('ripe_cur.json')
    cur = D.run_drc(a.board, cur_path)
    U, E, _ = D.score(cur)
    say('=== rip at break ends: breaks %d, errors %d, up to %d rounds ===' % (U, E, a.rounds))
    if E:
        say('REFUSED: the board has violations'); return 1
    tried = []          # centres of groups already tried (x, y, breaks at the time of the attempt)
    rnd = 0
    while rnd < a.rounds and U > 0:
        ends = ends_of(cur)
        if a.only_box:
            x0, y0, x1, y1 = (float(v) for v in a.only_box.split(','))
            ends = [e for e in ends if (x0 <= e[0] <= x1 and y0 <= e[1] <= y1)]
        if a.skip_box:
            x0, y0, x1, y1 = (float(v) for v in a.skip_box.split(','))
            ends = [e for e in ends if not (x0 <= e[0] <= x1 and y0 <= e[1] <= y1)]
        # group by distance; skip groups already tried at the same break count
        groups = []
        for e in sorted(ends, key=lambda e: e[3]):
            for g in groups:
                if math.hypot(e[0] - g[0][0], e[1] - g[0][1]) <= a.group_mm:
                    g.append(e); break
            else:
                groups.append([e])
        cand = None
        for g in groups:
            cx = sum(e[0] for e in g) / len(g); cy = sum(e[1] for e in g) / len(g)
            if any(math.hypot(cx - tx, cy - ty) <= 1.0 and tu == U for tx, ty, tu in tried):
                continue
            cand = (cx, cy, g); break
        if cand is None:
            say('all end groups tried at %d breaks - stop' % U); break
        cx, cy, g = cand
        rnd += 1
        tried.append((cx, cy, U))
        say('--- round %d: %d ends at (%.2f, %.2f): %s' % (rnd, len(g), cx, cy, ', '.join(sorted({e[2] for e in g}))[:80]))
        bak = a.board + '.bakRipRound'
        shutil.copy2(a.board, bak)
        r = subprocess.run([sys.executable, os.path.join(HERE, 'ripup.py'), '--board', a.board, '--drc', cur_path,
                            '--at', '%.3f,%.3f' % (cx, cy), '--radius', str(a.radius), '--window', str(a.window),
                            '--iters', str(a.iters), '--rip-planes', a.rip_planes, '--apply'],
                           capture_output=True, text=True, timeout=1800)
        tail = [l for l in r.stdout.splitlines() if l.strip() and 'swig' not in l and 'property.h' not in l][-2:]
        say('  ripup rc=%d: %s' % (r.returncode, ' | '.join(l.strip()[:90] for l in tail)))
        if r.returncode != 0:
            shutil.copy2(bak, a.board); continue
        new_path = paths.work('ripe_new.json')
        new = D.run_drc(a.board, new_path)
        U2, E2, _ = D.score(new)
        if E2:
            # remove only the new copper that ended up in violations (prune_new) and judge again - do not lose the round
            subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), 'prune_new.py'), '--board', a.board,
                            '--since', bak, '--drc', new_path, '--apply'], capture_output=True, text=True, timeout=900)
            new = D.run_drc(a.board, new_path)
            U2, E2, _ = D.score(new)
            say('  after removing the rejected items: breaks %d, errors %d' % (U2, E2))
        sig, sig2 = len(D.signal_breaks(cur)), len(D.signal_breaks(new))
        if E2 == 0 and (U2 < U or (sig2 < sig and U2 <= U + a.slack)):
            say('  ACCEPTED: %d -> %d (%+d), signal %d -> %d, errors 0' % (U, U2, U2 - U, sig, sig2))
            shutil.copy2(new_path, cur_path); cur, U = new, U2
        else:
            say('  REJECTED: %d -> %d, errors %d - rolled back' % (U, U2, E2))
            shutil.copy2(new_path, paths.work('ripe_rej_%02d.json' % rnd))
            shutil.copy2(bak, a.board)
    say('=== finished: breaks %d ===' % U)
    shutil.copy2(cur_path, paths.work('cur_drc.json'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
