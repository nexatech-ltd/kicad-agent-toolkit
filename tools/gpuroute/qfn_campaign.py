#!/usr/bin/env python3
"""The "open up a package" operator over every package with locked pins, under DRC acceptance.

One step for package R: (1) ripup.py centred on R with radius "half-diagonal + margin", also
removing copper of PLANE nets (tracks and vias at the pins: a power-plane via 0.2 mm from the end
of a QFN pin locked it for good); signal targets and ripped signal nets are re-routed by
negotiation; (2) fix_plane_pads.py gives plane pads their vias back - now around the new copper;
(3) prune_new.py removes items of this step that ended up in violations; (4) DRC: accepted only
with fewer breaks and zero violations, otherwise roll back to the copy.

    python3 tools/gpuroute/qfn_campaign.py [--rounds 12] [--margin 2.5]
"""
import argparse
import collections
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pcbnew

import drcinfo as D
import paths
import place_geom as P
from rip_campaign import locked_ends

MM = 1e6
LOG = paths.work('qfn.log')
VENV = sys.executable


def say(msg):
    line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def run(cmd, env=None, timeout=3600):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=timeout)


def hard_breaks(d):
    """Breaks that cannot be left for release: all signal breaks plus plane-net breaks where a PAD
    is cut off. Fill slivers (Zone+Zone of plane nets) are stitch_gnd's job at release and do not
    count in this step's acceptance: otherwise a round that opened two pins and added seven
    slivers was rejected (101 -> 106 while signal breaks went 68 -> 66)."""
    n = 0
    for b in D.breaks(d):
        plane = b[0].split('/')[-1] in D.PLANE_NETS
        if not plane or 'Pad' in b[1]:
            n += 1
    return n


def drc(board, out):
    d = D.run_drc(board, out)
    u, e, w = D.score(d)
    sig = hard_breaks(d)
    return d, u, e, sig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--rounds', type=int, default=12)
    ap.add_argument('--margin', type=float, default=2.5, help='radius margin beyond the package outline, mm')
    ap.add_argument('--iters', type=int, default=6)
    a = ap.parse_args()
    lck = os.path.join(os.path.dirname(os.path.abspath(a.board)), '~' + os.path.basename(a.board) + '.lck')
    if os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')
    work = os.path.dirname(paths.work('x'))
    cur_path = paths.work('qfn_cur.json')
    cur, U, E, S = drc(a.board, cur_path)
    say('=== qfn campaign: breaks %d (hard %d), errors %d ===' % (U, S, E))
    if E:
        say('REFUSED: the board has violations')
        return 1
    skipped = set()
    for rnd in range(1, a.rounds + 1):
        t = time.time()
        ends = locked_ends(a.board, cur)
        by_ref = collections.Counter(e[4].split()[-1] for e in ends)
        cands = [(r, n) for r, n in by_ref.most_common() if r not in skipped]
        say('locked ends %d, packages %d (skipped %d), search %.0f s'
            % (len(ends), len(cands), len(skipped), time.time() - t))
        if not cands:
            say('no packages with locked pins left - campaign finished')
            break
        ref, n = cands[0]
        board = pcbnew.LoadBoard(a.board)
        fp = next((f for f in board.GetFootprints() if f.GetReference() == ref), None)
        if fp is None:
            skipped.add(ref)
            continue
        bb = P.bbox_mm(fp)
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        radius = math.hypot(bb[2] - bb[0], bb[3] - bb[1]) / 2 + a.margin
        del board
        say('--- round %d: %s (%d locked), centre (%.2f, %.2f), radius %.1f' % (rnd, ref, n, cx, cy, radius))
        bak = a.board + '.bakQfn'
        shutil.copy2(a.board, bak)
        r = run([VENV, os.path.join(HERE, 'ripup.py'), '--board', a.board, '--drc', cur_path,
                 '--at', '%.3f,%.3f' % (cx, cy), '--radius', str(radius), '--window', str(radius),
                 '--iters', str(a.iters), '--rip-planes', 'all', '--apply'])
        tail = [l for l in r.stdout.splitlines() if l.strip() and 'swig' not in l and 'property.h' not in l][-2:]
        say('  ripup rc=%d: %s' % (r.returncode, ' | '.join(l.strip()[:90] for l in tail)))
        if r.returncode != 0:
            shutil.copy2(bak, a.board)
            skipped.add(ref)
            say('  round %d cancelled' % rnd)
            continue
        # shift the package's support parts (copper under the new place and stubs are removed inside nudge)
        r1 = run([VENV, os.path.join(HERE, 'nudge.py'), '--board', a.board, '--drc', cur_path,
                  '--ref', ref, '--apply'])
        t1 = [l for l in r1.stdout.splitlines() if 'SELECTED' in l or 'shifts:' in l]
        say('  nudge: %s' % ' | '.join(l.strip()[:80] for l in t1[-2:]))
        # targets of this package - one more router pass after the shift
        d_mid = D.run_drc(a.board, paths.work('qfn_mid.json'))
        nets = sorted({b[0] for b in D.signal_breaks(d_mid)
                       if ref in (b[3] or ()) and b[0].split('/')[-1] not in D.PLANE_NETS})
        if nets:
            only = paths.work('qfn_only.txt')
            with open(only, 'w') as f:
                f.write(''.join(n + '\n' for n in nets))
            r3 = run([VENV, os.path.join(HERE, 'route.py'), '--board', a.board, '--drc', paths.work('qfn_mid.json'),
                      '--limit', '60', '--only-net', only, '--iters', str(a.iters), '--apply'])
            t3 = [l for l in r3.stdout.splitlines() if 'to write:' in l]
            say('  route %s: %s' % (ref, t3[-1].strip()[:80] if t3 else 'rc=%d' % r3.returncode))
        # refill into the file - fix_plane_pads reads the fills from the file
        subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones', '--save-board', '--severity-all',
                        '--format', 'json', '-o', paths.work('qfn_tmp.json'), a.board],
                       capture_output=True, text=True)
        r2 = run([VENV, os.path.join(os.path.dirname(HERE), 'fix_plane_pads.py'), paths.work('qfn_tmp.json'), '--apply'],
                 env={'BRD': a.board, 'KAT_WORK': work})
        tail2 = [l for l in r2.stdout.splitlines() if l.strip()][-2:-1]   # summary line (last line is the backup note)
        say('  fix_plane_pads: %s' % (tail2[-1].strip()[:100] if tail2 else 'rc=%d' % r2.returncode))
        new, U2, E2, S2 = drc(a.board, paths.work('qfn_new.json'))
        if E2:
            run([VENV, os.path.join(os.path.dirname(HERE), 'prune_new.py'), '--board', a.board,
                 '--since', bak, '--drc', paths.work('qfn_new.json'), '--apply'])
            new, U2, E2, S2 = drc(a.board, paths.work('qfn_new.json'))
            say('  after removing DRC-rejected items: breaks %d, errors %d' % (U2, E2))
        if E2 == 0 and (S2 < S or (S2 == S and U2 < U)):
            say('  ACCEPTED: %d -> %d (hard %d -> %d), errors 0' % (U, U2, S, S2))
            U, S, cur = U2, S2, new
            shutil.copy2(paths.work('qfn_new.json'), cur_path)
            os.remove(bak)
        else:
            shutil.copy2(bak, a.board)
            skipped.add(ref)
            say('  REJECTED: %d -> %d (hard %d -> %d), errors %d - rolled back' % (U, U2, S, S2, E2))
    say('=== qfn campaign finished: breaks %d (hard %d) ===' % (U, S))
    return 0


if __name__ == '__main__':
    sys.exit(main())
