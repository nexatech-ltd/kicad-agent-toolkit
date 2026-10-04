#!/usr/bin/env python3
"""Per-net acceptance of a routing round: take from the round only the nets that improved.

Why. "Whole round or nothing" acceptance throws away the good together with the bad.
Measured: a round that the rule had to reject (429 breaks against a baseline of 256)
contained 11 nets whose breaks went down by 26 in total. An incremental round on 25 nets
gave 247 against 256 - an improvement, but with one clearance violation, so the
"zero new violations" rule rejected it entirely.

How. The board is assembled from the source board with the copper of the selected nets
replaced by copper from the round. KiCad is the judge: after assembly a full DRC runs, and
if significant-severity violations appeared, the offending nets (DRC names them directly)
are rolled back to their source copper and DRC is repeated. There is no custom violation
classification here - that lesson has been paid for: a home-made list of "significant
types" once let through three 0.45 mm vias on a board with a 0.6 mm minimum.

    python3 tools/merge_ses.py                       # assemble and report
    python3 tools/merge_ses.py --apply               # + write to the project and baseline
"""
import argparse
import collections
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drcinfo as D
import paths
from sexp import blocks

COPPER_TAGS = ('segment', 'arc', 'via')
MAX_ROUNDS = 4


def read(path):
    with open(path) as f:
        return f.read()


def copper_index(txt):
    """{net: [copper blocks]} and a list of cuts (a, b) for assembling the rest of the board."""
    by_net = collections.defaultdict(list)
    cuts = []
    for tag in COPPER_TAGS:
        for a, b in blocks(txt, tag):
            blk = txt[a:b]
            m = re.search(r'\(net "([^"]*)"\)', blk)
            by_net[m.group(1) if m else ''].append(blk.strip())
            cuts.append((a, b))
    cuts.sort()
    return by_net, cuts


def strip_copper(txt, cuts):
    """Board without any copper block; returns the file head without the closing bracket."""
    out, prev = [], 0
    for a, b in cuts:
        out.append(txt[prev:a])
        prev = b
        while prev < len(txt) and txt[prev] == '\n':
            prev += 1
    base = ''.join(out) + txt[prev:]
    head = base.rstrip()
    assert head.endswith(')'), 'board file does not end with a closing bracket'
    return head[:-1].rstrip('\n')


def assemble(head, by_net_src, by_net_new, take_from_new):
    """Board = head + copper: new copper for nets in take_from_new, source copper otherwise."""
    add = []
    for net in sorted(set(by_net_src) | set(by_net_new)):
        src = by_net_new if net in take_from_new else by_net_src
        for blk in src.get(net, []):
            add.append('\t' + blk)
    return head + '\n' + '\n'.join(add) + '\n)\n'


def offenders(drc):
    """Nets involved in significant-severity violations, by frequency."""
    c = collections.Counter()
    for v in drc['violations']:
        if v.get('severity') != 'error':
            continue
        for it in v.get('items', []):
            m = re.search(r'\[([^\]]*)\]', it.get('description', ''))
            if m and m.group(1):
                c[m.group(1)] += 1
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=paths.BOARD)
    ap.add_argument('--trial', default=paths.work('trial', paths.NAME + '.kicad_pcb'))
    ap.add_argument('--src-drc', default=None, help='DRC of the source board (computed if omitted)')
    ap.add_argument('--trial-drc', default=paths.work('trial', 'drc.json'))
    ap.add_argument('--out', default=paths.work('merged', paths.NAME + '.kicad_pcb'))
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    lck = os.path.join(os.path.dirname(os.path.abspath(a.src)),
                       '~' + os.path.basename(a.src) + '.lck')
    if os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor (%s)' % lck)

    outdir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(outdir, exist_ok=True)
    for ext in ('.kicad_pro', '.kicad_dru'):
        p = paths.sibling(a.src, ext)
        if os.path.exists(p):
            shutil.copy2(p, outdir)
    # verify.py and the schematic checks read the schematic from the board's directory
    for p in os.listdir(os.path.dirname(os.path.abspath(a.src))):
        if p.endswith('.kicad_sch'):
            shutil.copy2(os.path.join(os.path.dirname(os.path.abspath(a.src)), p), outdir)

    src_drc = D.load(a.src_drc) if a.src_drc else D.run_drc(a.src, os.path.join(outdir, 'src_drc.json'))
    trial_drc = D.load(a.trial_drc)
    bs, bt = D.by_net(src_drc), D.by_net(trial_drc)
    print('source: %s' % D.fmt(src_drc))
    print('round:  %s' % D.fmt(trial_drc))

    gain = {n: bs.get(n, 0) - bt.get(n, 0) for n in set(bs) | set(bt)}
    cand = sorted([n for n, g in gain.items() if g > 0 and n and n != '?'],
                  key=lambda n: (-gain[n], n))
    if not cand:
        print('no improved nets - nothing to take')
        return 1
    print('improved nets: %d, %d breaks in total'
          % (len(cand), sum(gain[n] for n in cand)))
    for n in cand[:15]:
        print('    %3d -> %-3d  %s' % (bs.get(n, 0), bt.get(n, 0), n))
    worse = sum(-gain[n] for n in gain if gain[n] < 0)
    if worse:
        print('(the round is worse by %d breaks on %d nets - those nets are not taken)'
              % (worse, sum(1 for n in gain if gain[n] < 0)))

    src_txt, trial_txt = read(a.src), read(a.trial)
    by_src, cuts_src = copper_index(src_txt)
    by_new, _ = copper_index(trial_txt)
    head = strip_copper(src_txt, cuts_src)

    take = set(cand)
    for it in range(1, MAX_ROUNDS + 1):
        with open(a.out, 'w') as f:
            f.write(assemble(head, by_src, by_new, take))
        d = D.run_drc(a.out, os.path.join(outdir, 'drc.json'), save_board=True)
        u, e, w = D.score(d)
        print('\nmerge %d: nets taken %d -> %s' % (it, len(take), D.fmt(d)))
        if e <= D.score(src_drc)[1]:
            print('VERDICT: ACCEPT - breaks %d -> %d (%+d), errors %d'
                  % (D.score(src_drc)[0], u, u - D.score(src_drc)[0], e))
            break
        bad = offenders(d)
        drop = [n for n, _ in bad.most_common() if n in take]
        if not drop:
            print('VERDICT: REJECT - the %d violations are not tied to any taken net:'
                  % e)
            for (sev, typ), n in D.types(d).most_common(6):
                if sev == 'error':
                    print('    %d %s' % (n, typ))
            return 1
        print('  significant-severity violations %d; rolling back nets: %s'
              % (e, ', '.join(drop[:4])))
        take -= set(drop[:max(1, len(drop) // 2 or 1)])
        if not take:
            print('VERDICT: REJECT - nothing left to take after rollbacks')
            return 1
    else:
        print('VERDICT: REJECT - violations persisted after %d merges' % MAX_ROUNDS)
        return 1

    if a.apply:
        shutil.copy2(paths.BOARD, paths.BOARD + '.bakMerge')
        shutil.copy2(a.out, paths.BOARD)
        with open(paths.work('baseline.json'), 'w') as f:
            json.dump({'u': u, 'v': e}, f)
        print('  board updated (backup .bakMerge), baseline -> {"u": %d, "v": %d}' % (u, e))
    else:
        print('  write to the project: python3 tools/merge_ses.py --apply')
    return 0


if __name__ == '__main__':
    sys.exit(main())
