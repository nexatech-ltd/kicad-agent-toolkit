#!/usr/bin/env python3
"""Unattended campaign of routing rounds: round after round, only upward.

A single round is not monotonic - it may close six breaks and introduce a violation.
Monotonicity comes from the pick_nets -> run_route -> merge_ses chain: only the nets that
improved are taken from a round, and a net that caused a violation is rolled back. So the
campaign can run unattended: the baseline is updated only on strict improvement.

Attempts are counted so the same stubborn nets are not hammered again and again: nets are
sorted by (attempt count, break length), i.e. untouched and short ones first.

    python3 tools/campaign.py --rounds 12 --batch 25 --seconds 240
"""
import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drcinfo as D
import paths
import pick_nets

STATE = paths.work('campaign.json')
LOG = paths.work('campaign.log')


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {'attempts': {}, 'rounds': []}


def save_state(st):
    with open(STATE, 'w') as f:
        json.dump(st, f, indent=1, ensure_ascii=False)


def say(msg):
    line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def run(cmd, env=None, timeout=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(cmd, shell=isinstance(cmd, str), env=e, timeout=timeout,
                          capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=8)
    ap.add_argument('--batch', type=int, default=25)
    ap.add_argument('--seconds', type=int, default=240)
    ap.add_argument('--max-len', type=float, default=None,
                    help='only take breaks shorter than this, mm')
    ap.add_argument('--full-every', type=int, default=0,
                    help='every N-th round is a full reroute instead of an incremental one')
    ap.add_argument('--full-seconds', type=int, default=None,
                    help='duration of a full round (default: twice the incremental one)')
    ap.add_argument('--prune-upto', type=int, default=25,
                    help='take a round whole if it adds at most this many violations, '
                         'and remove the copper DRC points at after acceptance')
    ap.add_argument('--keep-stitch', action='store_true',
                    help='do NOT hide ground stitching vias from the router. Hiding made the '
                         'pass faster but the result unusable: the router did not see hundreds '
                         'of through GND vias, ran tracks straight through them, and the import '
                         'produced ~150 shorting_items and dozens of clearance errors - the '
                         'round was always rejected.')
    ap.add_argument('--drop-plane-vias', action='store_true',
                    help='hide vias of all plane nets from the router, not just ground')
    ap.add_argument('--jar', default=os.environ.get('FR_JAR', 'fr.jar'),
                    help='Freerouting jar (relative to the work directory)')
    ap.add_argument('--java', default=os.environ.get('JAVA', 'java'),
                    help='Java binary that runs Freerouting')
    a = ap.parse_args()

    st = load_state()
    attempts = collections.Counter(st['attempts'])
    only = paths.work('campaign_nets.txt')
    stall = 0

    # The DRC report is reused between rounds. A full DRC with zone refill takes tens of
    # seconds and used to run 4-7 times per round - longer than the routing itself. Two of
    # those runs are redundant: after an accepted round the board state is already described
    # by the report acceptance produced, and after a rejected round the board did not change.
    cur = None
    drc_path = paths.work('cur_drc.json')
    for rnd in range(1, a.rounds + 1):
        if cur is None:
            cur = D.run_drc(paths.BOARD, drc_path)
        u0, e0, _ = D.score(cur)
        say('--- round %d/%d --- baseline: unconnected %d, errors %d' % (rnd, a.rounds, u0, e0))
        if u0 == 0:
            say('no breaks left - campaign finished')
            break

        # Full reroutes alternate with incremental ones on purpose. A full reroute yields an
        # entirely different solution, and per-net acceptance takes only the nets that
        # improved - so a full round can no longer spoil the board and serves as a source of
        # fresh alternatives where the incremental mode has stalled.
        full = a.full_every and (rnd % a.full_every == 0)
        if full:
            batch = []
            secs = a.full_seconds or a.seconds * 2
            say('FULL reroute round, %d s' % secs)
            env = {'ROUTE_SECONDS': str(secs), 'FR_JAR': a.jar, 'JAVA': a.java}
            if not a.keep_stitch:
                env['DROP_STITCH'] = '1'
            if a.drop_plane_vias:
                env['DROP_PLANE_VIAS'] = '1'
            env.pop('ONLY_NETS_FILE', None)
        else:
            rows = pick_nets.pick(cur, max_len=a.max_len)
            if not rows:
                say('no candidate nets (all filtered out) - campaign finished')
                break
            rows.sort(key=lambda r: (attempts[r[0]], r[1]))
            batch = rows[:a.batch]
            with open(only, 'w') as f:
                f.write(''.join(n + '\n' for n, _ in batch))
            say('batch of %d nets, breaks %.2f..%.2f mm, their attempts %s'
                % (len(batch), batch[0][1], batch[-1][1],
                   dict(collections.Counter(attempts[n] for n, _ in batch))))
            for n, _ in batch:
                attempts[n] += 1
            secs = a.seconds
            env = {'ONLY_NETS_FILE': only,
                   **({} if a.keep_stitch else {'DROP_STITCH': '1'}),
                   'ROUTE_SECONDS': str(secs), 'FR_JAR': a.jar, 'JAVA': a.java}
            if a.drop_plane_vias:
                env['DROP_PLANE_VIAS'] = '1'
        t = time.time()
        r = run(['bash', os.path.join(paths.PIPELINE, 'run_route.sh')], env=env,
                timeout=secs + 900)
        tail = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()][-3:]
        say('round ran %.0f s; tail: %s' % (time.time() - t, ' | '.join(tail)))

        # If the trial import AS A WHOLE is clean and better than the baseline, take it whole.
        # Per-net merging exists to extract the gain from a BAD round; on a good one it only
        # hurts: a round with verdict "ACCEPT, 247 -> 235, errors 0" was rejected by the merge
        # ("violations persisted after 4 merges"), because a mix of some nets from the new
        # solution with the old board is less consistent than the solution as a whole.
        # The strings matched here are printed by accept.py and merge_ses.py.
        whole_ok = any('VERDICT' in l and 'ACCEPT' in l for l in tail)
        # A round with few violations is also taken whole, and then the copper DRC points at
        # is removed - the same approach that converged on a fan-out ("lay by calculation,
        # DRC has the last word"). Measured: a full reroute of a fan-out gained hundreds of
        # breaks with JUST TWO violations, which per-net merging could not isolate, so the
        # whole round was lost.
        if not whole_ok:
            for l in tail:
                m = re.search(r'significant-severity violations appeared \((\d+) vs (\d+)\)', l)
                if m and int(m.group(1)) - int(m.group(2)) <= a.prune_upto:
                    whole_ok = True
                    say('round taken for pruning: violations %s vs %s, threshold %d'
                        % (m.group(1), m.group(2), a.prune_upto))
                    break
        trial_drc = paths.work('trial', 'drc.json')
        if whole_ok and os.path.exists(trial_drc):
            m = run([sys.executable, os.path.join(paths.PIPELINE, 'accept.py'),
                     trial_drc, '--apply', '--force'], timeout=1800)
            say('WHOLE-round acceptance: %s'
                % ((m.stdout.splitlines() or ['(empty)'])[-1])[:160])
            verdict = ['VERDICT: ACCEPT (whole)'] if m.returncode == 0 else []
            if not verdict:
                say('whole-round acceptance failed - trying per net')
        else:
            verdict = []
        if not verdict:
            m = run([sys.executable, os.path.join(paths.PIPELINE, 'merge_ses.py'),
                     '--src-drc', drc_path, '--apply'], timeout=1800)
            verdict = [l for l in m.stdout.splitlines() if l.startswith('VERDICT')]
            say('per-net acceptance: %s'
                % (verdict[0] if verdict else (m.stdout.splitlines() or ['(empty)'])[-1]))

        # Deduplicate vias after every accepted round. Otherwise they pile up: acceptance
        # judges by errors, KiCad classifies overlapping vias as warnings, and ten rounds
        # accumulated 31 holes_co_located and 4 hole_to_hole. A warning that keeps piling up
        # ends up as a hundred objects to sort out.
        if verdict and 'ACCEPT' in verdict[0]:
            dd = run([sys.executable, os.path.join(paths.KAT_TOOLS, 'dedup_vias.py'), '--apply'],
                     timeout=600)
            # matches the summary line printed by dedup_vias.py
            tail_dd = [l for l in dd.stdout.splitlines()
                       if 'removed' in l.lower() or 'deleted' in l.lower()]
            if tail_dd:
                say('dedup: %s' % tail_dd[-1])
                # The DRC report does not go stale: dedup_vias removes OVERLAPPING vias of the
                # same net, so connectivity and violation counts do not change. Otherwise DRC
                # would have to be rerun after every round.

        merged_drc = paths.work('merged', 'drc.json')
        accepted = bool(verdict) and 'ACCEPT' in verdict[0]
        if accepted and os.path.exists(merged_drc):
            # acceptance already ran DRC on the very board it copied into the project
            shutil.copy2(merged_drc, drc_path)
            after = D.load(drc_path)
        elif accepted:
            after = D.run_drc(paths.BOARD, drc_path)
        else:
            after = cur          # round rejected, the board did not change
        cur = after
        u1, e1, w1 = D.score(after)
        # Recheck after acceptance. The first DRC right after copying the board once showed
        # 19 violations where a rerun gave 0 - apparently the zone fill had not settled. So
        # with nonzero violations the report is taken again, and only if they are confirmed
        # does the campaign stop: the acceptance rule is monotonic, and a violation left in
        # place becomes the new ceiling.
        # Rollback if whole-round acceptance made the board worse. The run_route.sh verdict
        # measures the gain against ITS OWN trial baseline (copper of the routed nets
        # stripped), not against the project board. So "ACCEPT" on the trial import can mean
        # a regression in the project - measured: a round went 211 -> 216 (+5).
        # Copper rejected by DRC is removed right after whole-round acceptance.
        if accepted and e1 and os.path.exists(paths.BOARD + '.bakAccept'):
            pr = run([sys.executable, os.path.join(paths.PIPELINE, 'prune_new.py'),
                      '--since', paths.BOARD + '.bakAccept', '--drc', drc_path, '--apply'],
                     timeout=1800)
            # matches the summary lines printed by prune_new.py
            hit = [l for l in pr.stdout.splitlines() if 'remov' in l.lower()]
            after = D.run_drc(paths.BOARD, drc_path)
            cur = after
            u1, e1, w1 = D.score(after)
            say('removal of DRC-rejected copper: %s -> breaks %d, errors %d'
                % (hit[-1].strip() if hit else '?', u1, e1))
        if accepted and u1 > u0 and os.path.exists(paths.BOARD + '.bakAccept'):
            shutil.copy2(paths.BOARD + '.bakAccept', paths.BOARD)
            after = D.run_drc(paths.BOARD, drc_path)
            cur = after
            u1, e1, w1 = D.score(after)
            say('ROLLBACK of whole-round acceptance: the project got worse, back to %d' % u1)
            accepted = False
        if accepted and e1:
            after = D.run_drc(paths.BOARD, drc_path)
            cur = after
            u1, e1, w1 = D.score(after)
            say('recheck after acceptance: breaks %d, errors %d' % (u1, e1))
            if e1:
                say('STOP: %d significant-severity violations remain after acceptance' % e1)
                st['rounds'].append({'round': rnd, 'u0': u0, 'u1': u1, 'e1': e1,
                                     'mode': 'full' if full else 'step', 'stopped': True})
                save_state(st)
                break
        dt = time.time() - t
        say('round result: unconnected %d -> %d (%+d), errors %d | %.0f s, %.2f breaks/min'
            % (u0, u1, u1 - u0, e1, dt, (u0 - u1) / (dt / 60.0) if dt else 0))
        st['rounds'].append({'round': rnd, 'u0': u0, 'u1': u1, 'e1': e1,
                             'mode': 'full' if full else 'step',
                             'batch': [n for n, _ in batch], 'seconds': secs})
        st['attempts'] = dict(attempts)
        save_state(st)

        if u1 >= u0:
            stall += 1
            if stall >= (5 if a.full_every else 3):
                say('several rounds in a row without progress - stopping, change strategy')
                break
        else:
            stall = 0

    say('campaign finished')
    return 0


if __name__ == '__main__':
    sys.exit(main())
