#!/usr/bin/env python3
"""Board views (renders, SVGs) were taken from the board that is on disk now.

Exists because conclusions are drawn from the views (parts overhanging the outline,
external connectors only on the edges, fit in the enclosure), and views have been found
rendered from outdated copper, or a few seconds older than the last board write (the gate
refilled zones after the render).

CONTENT is compared, not time: a zone refill touches the file without changing the result,
so mtime would raise a false alarm; conversely, a copper edit within the same second would
not be distinguished by mtime.

  python3 tools/check_views_fresh.py --board board.kicad_pcb --views diagrams/views
"""
import argparse
import hashlib
import os
import sys


def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--views', required=True,
                    help='views directory; an empty string means no views are kept for this board')
    a = ap.parse_args()
    if not a.views:
        # Empty path: no views are maintained for this board. Say so explicitly; silently
        # returning "passed" would misreport the scope of the check.
        print('no views are maintained for this board: nothing to check (directory not set)')
        return 0
    if not os.path.isdir(a.views):
        print('WARNING: views directory %s does not exist: views were never rendered.' % a.views)
        return 1
    stamp = os.path.join(a.views, '.board.sha256')
    if not os.path.exists(stamp):
        print('WARNING: %s has no .board.sha256: views predate the fingerprint.'
              % a.views)
        print('Re-render: OUT=%s bash tools/make_views.sh' % a.views)
        return 1
    want = open(stamp, encoding='utf-8').read().split()[0]
    got = sha(a.board)
    if want != got:
        print('WARNING: views were rendered from a DIFFERENT board.')
        print('  in views: %s' % want)
        print('  on disk:  %s' % got)
        print('Conclusions about part overhang and enclosure fit drawn from an outdated')
        print('picture can hide a mechanical collision.')
        print('Re-render: OUT=%s bash tools/make_views.sh' % a.views)
        return 1
    n = len([f for f in os.listdir(a.views) if not f.startswith('.')])
    print('views are fresh: fingerprint matches, %d files in the directory' % n)
    return 0


if __name__ == '__main__':
    sys.exit(main())
