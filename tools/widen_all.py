#!/usr/bin/env python3
"""Widen the copper of all nets to their class width - a wrapper around widen_net.py (one net per run, ~1 s).

The router lays 0.15 mm (ROUTE_WIDTH); the class width lives in the .kicad_pro net classes (e.g. Default 0.20,
a motor class 0.40, a power-rail class 1.20 ...) and DRC does not check it - verify.py does ("copper not narrower
than its class width"). Here: list the nets with segments narrower than their class (pcbnew, read only) ->
widen_net.py for each (text edit with .bakWidenNet) -> DRC at the end; on violations roll back to the copy taken
before the run.

    python3 tools/widen_all.py --board B [--apply] [--only Default,MOTOR]
"""
import argparse
import collections
import fnmatch
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import drcinfo as D

import pcbnew


def narrow_nets(board_path, pro_path, only=None):
    b = pcbnew.LoadBoard(board_path)
    ns = json.load(open(pro_path))['net_settings']
    cw = {c['name']: float(c.get('track_width', 0.2)) for c in ns['classes']}
    def cls(n):
        base = n.split('/')[-1]
        for pt in ns['netclass_patterns']:
            if fnmatch.fnmatch(base, pt['pattern']):
                return pt['netclass']
        return 'Default'
    acc = collections.defaultdict(lambda: [0, 0.0])
    for t in b.GetTracks():
        if t.GetClass() != 'PCB_TRACK':
            continue
        n = t.GetNetname()
        if not n:
            continue
        c = cls(n)
        if only and c not in only:
            continue
        if t.GetWidth() / 1e6 + 1e-6 < cw.get(c, 0.2):
            acc[n][0] += 1; acc[n][1] += t.GetLength() / 1e6
    return sorted(acc.items(), key=lambda kv: -kv[1][1]), cls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--only', default=None, help='comma-separated net classes')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--fix-rounds', type=int, default=5, help='how many times to restore the original width of segments in violations and re-run DRC')
    a = ap.parse_args()
    pro = paths.sibling(a.board, '.kicad_pro')
    only = set(a.only.split(',')) if a.only else None
    nets, cls = narrow_nets(a.board, pro, only)
    print('nets with narrow copper: %d, segments %d, length %.0f mm' % (len(nets), sum(v[0] for _, v in nets), sum(v[1] for _, v in nets)))
    if not a.apply:
        for n, (k, L) in nets[:20]:
            print('  %-40s %-8s %3d segs %6.1f mm' % (n[-40:], cls(n), k, L))
        return 0
    bak = a.board + '.bakWidenAll'
    shutil.copy2(a.board, bak)
    # original widths (uuid -> nm): read ONCE (a second LoadBoard in the process returns None); edits run as subprocesses
    b0 = pcbnew.LoadBoard(a.board)
    orig = {t.m_Uuid.AsString(): t.GetWidth() for t in b0.GetTracks() if t.GetClass() == 'PCB_TRACK'}
    drc0 = D.run_drc(a.board, paths.work('widen_all_drc0.json')); U0, E0, _ = D.score(drc0)
    del b0
    env = dict(os.environ, BRD=a.board)
    done = 0
    for n, (k, L) in nets:
        r = subprocess.run([sys.executable, os.path.join(HERE, 'widen_net.py'), n.split('/')[-1], '--apply'],
                           capture_output=True, text=True, env=env, timeout=600)
        last = [l for l in r.stdout.splitlines() if l.strip()][-1:]
        print('  %-40s %s' % (n[-40:], (last[0] if last else 'rc=%d' % r.returncode)[:90]), flush=True)
        done += 1
    for rnd in range(a.fix_rounds + 1):
        drc = D.run_drc(a.board, paths.work('widen_all_drc.json'))
        u, e, w = D.score(drc)
        print('after widening (check %d): unconnected %d (was %d), errors %d' % (rnd, u, U0, e), flush=True)
        if not e or rnd == a.fix_rounds:
            break
        # offending segments go back to their original width (widen_net's model does not know the class
        # clearance of the OTHER net, the board edge or NPTH); DRC has the last word
        bad = {}
        for v in drc.get('violations', []):
            if v.get('severity') != 'error':
                continue
            for it in v.get('items', []):
                uid = it.get('uuid')
                if uid in orig:
                    bad[uid] = orig[uid]
        if not bad:
            print('  violations without widened segments - not fixing further'); break
        js = paths.work('widen_all_restore.json')
        json.dump(bad, open(js, 'w'))
        r = subprocess.run([sys.executable, os.path.join(HERE, 'set_widths.py'), '--board', a.board, '--json', js],
                           capture_output=True, text=True, timeout=600)
        print('  restored width of %d segments: %s' % (len(bad), (r.stdout.strip().splitlines() or [''])[-1][:80]), flush=True)
    if e:
        print('REFUSED: violations remain - rolling back to %s' % bak)
        shutil.copy2(bak, a.board)
        return 1
    if u > U0:
        print('WARNING: unconnected count grew (%d -> %d) - check report %s' % (U0, u, paths.work('widen_all_drc.json')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
