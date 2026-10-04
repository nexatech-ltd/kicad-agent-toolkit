#!/usr/bin/env python3
"""Fingerprint of the acceptance-gate inputs: which state a verdict belongs to, and
whether it is stale.

Why. A gate log can be quoted after the schematic changed: the gate ran honestly, but on
an input that no longer exists (e.g. two capacitors moved onto the connection grid after
the run turned a reported "ERC 12" into a real 10). Neither the exit code nor the report
content shows this; the only hint lies outside the report, in file timestamps.

So the verdict carries a fingerprint of what was checked: sha256 of every schematic sheet,
the board and the rules file. "Log is stale" is then one command away, not a manual mtime
comparison.

    python3 tools/gate_stamp.py --write     # write the fingerprint next to the verdict
    python3 tools/gate_stamp.py --check     # compare the log with the current files
    python3 tools/gate_stamp.py             # just print the fingerprint

--check exit code: 0 - the log matches the inputs, 1 - stale (or no fingerprint).
File times are NOT used: a rewrite without changes (e.g. kicad-cli --save-board after a
zone refill) moves mtime but keeps the content, and such a log is still valid.
"""
import argparse
import glob
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402

NAME = 'inputs.sha'


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def inputs(root, board):
    """Files the acceptance depends on: all schematic sheets, the board, the DRC rules."""
    items = sorted(glob.glob(os.path.join(root, '*.kicad_sch')))
    for extra in (board, paths.sibling(board, '.kicad_dru') if board else ''):
        if extra and os.path.exists(extra):
            items.append(extra)
    return [(os.path.relpath(p, root), sha(p)) for p in items]


def fmt(items):
    return ''.join('%s  %s\n' % (h, n) for n, h in items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=paths.KAT_ROOT)
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--out', default='', help='gate log directory (default: $KAT_WORK/gate)')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--write', action='store_true')
    g.add_argument('--check', action='store_true')
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    board = a.board or paths.BOARD
    out = a.out or os.path.join(paths.KAT_WORK, 'gate')
    items = inputs(root, board)
    stamp = os.path.join(out, NAME)

    if a.write:
        os.makedirs(out, exist_ok=True)
        open(stamp, 'w').write(fmt(items))
        print('input fingerprint: %d files -> %s' % (len(items), os.path.relpath(stamp, root)))
        return 0

    if a.check:
        if not os.path.exists(stamp):
            print('no fingerprint (%s): gate logs must not be quoted, run the gate'
                  % os.path.relpath(stamp, root))
            return 1
        was = {n.strip(): h for h, n in
               (l.split('  ', 1) for l in open(stamp).read().splitlines() if l.strip())}
        now = dict(items)
        diff = []
        for n in sorted(set(was) | set(now)):
            if was.get(n) != now.get(n):
                diff.append((n, 'added' if n not in was else
                             'removed' if n not in now else 'changed'))
        if not diff:
            print('gate log matches the inputs (%d files)' % len(items))
            return 0
        print('GATE LOG IS STALE: %d files changed since the gate run' % len(diff))
        for n, what in diff:
            print('  %-28s %s' % (n, what))
        print('numbers from %s must not be quoted: rerun the gate' % os.path.relpath(out, root))
        return 1

    print(fmt(items), end='')
    return 0


if __name__ == '__main__':
    sys.exit(main())
