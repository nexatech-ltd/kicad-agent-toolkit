#!/usr/bin/env python3
"""Nudge the locking parts package by package under DRC acceptance.

Locked ends are computed once per pass (a few minutes), then nudge.py --ref runs for each
package as a separate process (a second LoadBoard in one process returns None).
A shift is accepted with zero violations; the break count may grow (segments removed at the
moved pads) - the next GPU router round closes them, and route_gpu_all.sh judges the whole cycle.

    python3 tools/gpuroute/nudge_campaign.py
"""
import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import drcinfo as D
import paths
from rip_campaign import locked_ends

LOG = paths.work('nudge.log')


def say(msg):
    line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--max-refs', type=int, default=12)
    a = ap.parse_args()
    cur_path = paths.work('nudge_cur.json')
    cur = D.run_drc(a.board, cur_path)
    U, E, _ = D.score(cur)
    say('=== nudge campaign: breaks %d, errors %d ===' % (U, E))
    if E:
        say('REFUSED: the board has violations')
        return 1
    t = time.time()
    ends = locked_ends(a.board, cur)
    by_ref = collections.Counter(e[4].split()[-1] for e in ends)
    say('locked ends %d: %s (%.0f s)' % (
        len(ends), ', '.join('%sx%d' % kv for kv in by_ref.most_common()), time.time() - t))
    ends_path = paths.work('nudge_ends.json')
    with open(ends_path, 'w') as f:
        json.dump(ends, f)
    done = 0
    for ref, n in by_ref.most_common()[:a.max_refs]:
        bak = a.board + '.bakNudgeRound'
        shutil.copy2(a.board, bak)
        r = subprocess.run([sys.executable, os.path.join(HERE, 'nudge.py'), '--board', a.board,
                            '--ends', ends_path, '--ref', ref, '--apply'],
                           capture_output=True, text=True, timeout=1800)
        tail = [l for l in r.stdout.splitlines() if 'SELECTED' in l or 'nothing to move' in l or 'shifts:' in l]
        say('--- %s (%d locked): %s' % (ref, n, ' | '.join(l.strip()[:100] for l in tail[-3:])))
        if 'board written' not in r.stdout:
            os.remove(bak)
            continue
        new = D.run_drc(a.board, paths.work('nudge_new.json'))
        U2, E2, _ = D.score(new)
        if E2 == 0:
            say('  ACCEPTED: breaks %d -> %d, errors 0' % (U, U2))
            U = U2
            done += 1
            os.remove(bak)
        else:
            shutil.copy2(bak, a.board)
            say('  REJECTED: errors %d - rolled back' % E2)
    say('=== nudge campaign finished: shifts accepted %d, breaks %d ===' % (done, U))
    return 0


if __name__ == '__main__':
    sys.exit(main())
