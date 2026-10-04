#!/usr/bin/env python3
"""Per-pocket placement acceptance: search -> apply -> re-route -> DRC -> accept/roll back.

Skeleton follows qfn_campaign.py. One pocket = one run; every external tool runs as a subprocess
(LoadBoard limitation). Accepted only with 0 violations and fewer "hard" breaks (signal breaks +
cut-off plane pads; Zone+Zone fill slivers are left to stitch_gnd at release).

The pocket is given on the command line: --refs (ICs of the pocket) and optionally --window
(x0,y0,x1,y1 in mm, e.g. one side of a large IC). To process several pockets, call this script
once per pocket from a shell loop.

    python3 tools/gpuroute/place/place_campaign.py --refs U1,U2 --seconds 2700
    python3 tools/gpuroute/place/place_campaign.py --refs U1 --best work/place/U1/best.json   # no search
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import drcinfo as D
import paths
from qfn_campaign import hard_breaks

VENV = sys.executable
ROOT = os.path.dirname(os.path.dirname(HERE))          # tools/
LOG = paths.work('place', 'campaign.log')
# Markers printed by gpu_campaign.sh for an accepted round and for the end of the campaign;
# only used to summarise its output in the log.
CAMPAIGN_MARKERS = ('ACCEPTED', 'campaign finished')


def say(msg):
    line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def run(cmd, env=None, timeout=7200):
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=timeout)
    tail = [l for l in (r.stdout + r.stderr).splitlines() if l.strip() and 'swig' not in l and 'property.h' not in l and 'Debug' not in l]
    return r.returncode, tail


def drc(board, out):
    d = D.run_drc(board, out)
    u, e, w = D.score(d)
    return d, u, e, hard_breaks(d)


def signal_breaks_n(d):
    return sum(1 for b in D.breaks(d) if b[0].split('/')[-1] not in D.PLANE_NETS)


HARD_SLACK = 2   # hard breaks may grow by at most this much (plane pads are fixed by --planes/fix_plane_pads)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--refs', required=True, help='comma-separated ICs of the pocket, e.g. U1,U2')
    ap.add_argument('--seconds', type=float, default=2700)
    ap.add_argument('--best', default=None, help='ready best.json (skip the search)')
    ap.add_argument('--no-search', action='store_true',
                    help='no search: apply the CURRENT placement (re-route the window with L1 via jobs)')
    ap.add_argument('--movable-ics', action='store_true')
    ap.add_argument('--iters', type=int, default=8)
    ap.add_argument('--rounds', type=int, default=8, help='router campaign rounds after applying')
    ap.add_argument('--window', default=None, help='manual window x0,y0,x1,y1 (e.g. one side of a large IC)')
    a = ap.parse_args()
    refs = a.refs.split(',')
    tag = '_'.join(refs)
    win_args = ['--window', a.window] if a.window else []
    if a.window:
        tag += '_w' + a.window.replace(',', '_').replace('.', 'p')
    out_dir = paths.work('place', tag)
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.dirname(paths.work('x'))
    cur_path = os.path.join(out_dir, 'drc_cur.json')
    cur, U, E, S = drc(a.board, cur_path)
    say('=== pocket %s: breaks %d (hard %d), errors %d ===' % (tag, U, S, E))
    if E:
        say('REFUSED: the board has violations')
        return 1
    # 1. search
    best_path = a.best or os.path.join(out_dir, 'best.json')
    if a.no_search and not a.best:
        best_path = os.path.join(out_dir, 'base.json')
        rc, tail = run([VENV, os.path.join(HERE, 'check_l1.py'), '--board', a.board, '--refs', a.refs, '--best', best_path] + win_args)
        say('current placement as candidate rc=%d: %s' % (rc, ' | '.join(l.strip()[:110] for l in tail if l.startswith('L1:'))))
        if rc != 0 or not os.path.exists(best_path):
            say('base.json not created — pocket skipped')
            return 2
    elif not a.best:
        cmd = [VENV, os.path.join(HERE, 'search.py'), '--board', a.board, '--refs', a.refs, '--seconds', str(a.seconds)]
        if a.movable_ics:
            cmd.append('--movable-ics')
        rc, tail = run(cmd)
        say('search rc=%d: %s' % (rc, ' | '.join(l.strip()[:100] for l in tail[-2:])))
        if not os.path.exists(best_path):
            say('best.json not created — pocket skipped')
            return 2
    best = json.load(open(best_path))
    say('placement: F=%s %s' % (best.get('F'), best.get('info')))
    # 2. L1 paths of this placement (escape copper of the pocket and plane vias) — check_l1 --placement --write
    paths_txt = os.path.join(out_dir, 'l1_paths.txt')
    rc, tail = run([VENV, os.path.join(HERE, 'check_l1.py'), '--board', a.board, '--refs', a.refs,
                    '--placement', best_path, '--write', paths_txt] + win_args)
    # 'L1:' and 'L1 path clashes' are line prefixes printed by check_l1.py
    say('L1 rc=%d: %s' % (rc, ' | '.join(l.strip()[:110] for l in tail if l.startswith(('L1:', 'L1 path clashes')))))
    if rc != 0 or not os.path.exists(paths_txt):
        say('no L1 paths — pocket skipped')
        return 2
    # 3. apply: move footprints, rip the window copper, write the L1 paths
    bak = os.path.join(out_dir, 'pre.kicad_pcb')
    shutil.copy2(a.board, bak)
    rc, tail = run([VENV, os.path.join(HERE, 'apply_place.py'), '--board', a.board, '--best', best_path,
                    '--paths', paths_txt, '--apply'])
    say('apply rc=%d: %s' % (rc, ' | '.join(l.strip()[:100] for l in tail[-2:])))
    if rc != 0:
        shutil.copy2(bak, a.board)
        return 2
    # 4. join the stubs on the ring: GPU router campaign on this board (per-round DRC acceptance:
    #    fewer breaks, no more errors). On a dense row of drivers this took e.g. 154 -> 114 -> 108 breaks in two rounds.
    env = {'BRD': os.path.abspath(a.board), 'KAT_WORK': os.path.abspath(out_dir), 'BATCH': '60',
           'ROUNDS': str(a.rounds), 'RETRY': '2', 'ITERS': str(a.iters), 'STALL': '2', 'PLANES': '1'}
    for f in ('hard_nets.txt', 'offender_hits.txt'):
        fp = os.path.join(out_dir, f)
        if os.path.exists(fp):
            os.remove(fp)
    rc, tail = run(['bash', os.path.join(ROOT, 'gpu_campaign.sh')], env=env, timeout=4 * 3600)
    acc = [l for l in tail if any(m in l for m in CAMPAIGN_MARKERS)]
    say('window campaign rc=%d: %s' % (rc, ' | '.join(l.strip()[10:90] for l in acc[-4:])))
    # 4. plane pads, removal of rejected copper
    subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones', '--save-board', '--severity-all', '--format', 'json',
                    '-o', os.path.join(out_dir, 'drc_mid2.json'), a.board], capture_output=True, text=True)
    rc, tail = run([VENV, os.path.join(ROOT, 'fix_plane_pads.py'), os.path.join(out_dir, 'drc_mid2.json'), '--apply'],
                   env={'BRD': a.board, 'KAT_WORK': work})
    say('fix_plane_pads: %s' % ' | '.join(l.strip()[:100] for l in tail[-1:]))
    new, U2, E2, S2 = drc(a.board, os.path.join(out_dir, 'drc_new.json'))
    if E2:
        run([VENV, os.path.join(ROOT, 'prune_new.py'), '--board', a.board, '--since', bak,
             '--drc', os.path.join(out_dir, 'drc_new.json'), '--apply'])
        new, U2, E2, S2 = drc(a.board, os.path.join(out_dir, 'drc_new.json'))
        say('after removing rejected copper DRC: breaks %d, errors %d' % (U2, E2))
    # 5. one more round of the regular router over the window — stubs on the ring
    if E2 == 0:
        rc, tail = run([VENV, os.path.join(os.path.dirname(HERE), 'route.py'), '--board', a.board,
                        '--drc', os.path.join(out_dir, 'drc_new.json'), '--limit', '80', '--iters', str(a.iters), '--apply'])
        new, U2, E2, S2 = drc(a.board, os.path.join(out_dir, 'drc_new.json'))
        if E2:
            run([VENV, os.path.join(ROOT, 'prune_new.py'), '--board', a.board, '--since', bak,
                 '--drc', os.path.join(out_dir, 'drc_new.json'), '--apply'])
            new, U2, E2, S2 = drc(a.board, os.path.join(out_dir, 'drc_new.json'))
    # Acceptance: 0 errors, fewer breaks in total, no more signal breaks, hard ones at most +HARD_SLACK.
    # A strict "fewer hard breaks" rule rejected a pocket that went from 76 to 65 breaks because of a
    # single plane pad (hard 32 -> 33), which fix_plane_pads/route --planes repairs afterwards.
    sig, sig2 = signal_breaks_n(cur), signal_breaks_n(new)
    if E2 == 0 and (S2 < S or (U2 < U and sig2 <= sig and S2 <= S + HARD_SLACK)):
        say('ACCEPTED: breaks %d -> %d (hard %d -> %d, signal %d -> %d), errors 0' % (U, U2, S, S2, sig, sig2))
        shutil.copy2(os.path.join(out_dir, 'drc_new.json'), paths.work('cur_drc.json'))
        # footprint geometry for the next pockets — at the NEW positions (geom.json is stale after moves)
        rc, tail = run([VENV, os.path.join(HERE, 'geom.py'), '--board', a.board])
        say('geom.json updated rc=%d' % rc)
        return 0
    shutil.copy2(bak, a.board)
    say('REJECTED: breaks %d -> %d (hard %d -> %d, signal %d -> %d), errors %d — rolled back' % (U, U2, S, S2, sig, sig2, E2))
    return 3


if __name__ == '__main__':
    sys.exit(main())
