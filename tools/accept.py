"""Acceptance of a routing round: a verdict with an exit code, not just numbers.

after_ses.sh used to print numbers while the acceptance rule lived in documentation and
was applied by a human; there was no exit code and no copying of an accepted result into
the project. The rule is written down once, here:

  accept if unconnected went DOWN from the baseline and no new violations of significant
  severity appeared (severity "error" as KiCad classifies it, not a custom type list)

  python3 tools/accept.py work/trial/drc.json            # verdict only, exit code 0/1
  python3 tools/accept.py work/trial/drc.json --apply    # + copy into the project and baseline
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drcinfo as D
import paths


def baseline_path():
    return paths.work('baseline.json')


def read_baseline():
    p = baseline_path()
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('drc', nargs='?', default=paths.work('trial', 'drc.json'))
    ap.add_argument('--force', action='store_true',
                    help='copy the round into the project despite a reject verdict. The '
                         'campaign decides to take a round "for pruning": a full reroute of a '
                         'fan-out can gain hundreds of breaks at the cost of a few violations, '
                         'and it is cheaper to take it whole and remove the copper DRC points '
                         'at (tools/prune_new.py) than to lose the round.')
    ap.add_argument('--apply', action='store_true',
                    help='copy an accepted result into the project and update the baseline')
    a = ap.parse_args()

    d = D.load(a.drc)
    u, e, w = D.score(d)
    base = read_baseline()
    print('TRIAL IMPORT: ' + D.fmt(d))
    for (sev, typ), n in D.types(d).most_common():
        print('    %5d  %-8s %s' % (n, sev, typ))
    cmin = D.clearance_min(d)
    if cmin is not None:
        print('    smallest actual clearance in violation texts: %.4f mm'
              ' (typical fab minimum 0.0889)' % cmin)
    if base is None:
        print('BASELINE not set - no verdict')
        return 1
    print('BASELINE: unconnected %d | errors %d' % (base['u'], base['v']))

    better = u < base['u']
    clean = e <= base['v']
    if better and clean:
        print('VERDICT: ACCEPT - breaks %d -> %d (%+d), errors %d -> %d'
              % (base['u'], u, u - base['u'], base['v'], e))
    else:
        why = []
        if not better:
            why.append('breaks not reduced (%d vs %d)' % (u, base['u']))
        if not clean:
            why.append('significant-severity violations appeared (%d vs %d)' % (e, base['v']))
        print('VERDICT: REJECT - ' + '; '.join(why))
        if not (a.force and a.apply):
            print('  the gain can be extracted per net: python3 tools/merge_ses.py')
            return 1
        print('  --force: the round is copied anyway; prune_new.py will remove the violations')

    if a.apply:
        trial = os.path.join(os.path.dirname(os.path.abspath(a.drc)),
                             os.path.basename(paths.BOARD))
        assert os.path.exists(trial), trial
        shutil.copy2(paths.BOARD, paths.BOARD + '.bakAccept')
        shutil.copy2(trial, paths.BOARD)
        with open(baseline_path(), 'w') as f:
            json.dump({'u': u, 'v': e}, f)
        print('  board updated (backup .bakAccept), baseline -> {"u": %d, "v": %d}' % (u, e))
    else:
        print('  copy into the project: python3 tools/accept.py %s --apply' % a.drc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
