#!/usr/bin/env python3
"""Make the net names of copper in the board file match what KiCad sees.

On load KiCad propagates a pad's net onto the copper touching it. So after a pad's
net is changed (e.g. a few pads switched from a plane net to a signal), the file
still says `(net "+3V3")` while KiCad assigns that copper to the new signal. DRC
stays silent, since everything is consistent after propagation, but text-based
tools read the file and work with the stale name: `strip_pocket.py --nets +3V3`
would remove the new signal's copper.

Typically only a handful of objects out of thousands differ, but exactly such
mismatches give plausible, wrong results without a single error message.
"""
import sys, os, re, argparse, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp, paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    import pcbnew
    b = pcbnew.LoadBoard(a.board)
    live = {t.m_Uuid.AsString(): t.GetNetname() for t in b.GetTracks()}
    s = open(a.board).read()
    fixes = []
    for tag in ('segment', 'via', 'arc'):
        for i, j in sexp.blocks(s, tag):
            blk = s[i:j]
            u = re.search(r'\(uuid "([^"]+)"\)', blk)
            n = re.search(r'\(net "([^"]*)"\)', blk)
            if not (u and n):
                continue
            want = live.get(u.group(1))
            if want is not None and want != n.group(1):
                fixes.append((i, j, n.start(1), n.end(1), n.group(1), want))
    print('net name mismatches: %d' % len(fixes))
    for _, _, _, _, old, new in fixes:
        print('   %-24s -> %s' % (old.split('/')[-1], new.split('/')[-1]))
    if not fixes:
        return 0
    # edit from the end so offsets stay valid
    for i, j, ns, ne, old, new in sorted(fixes, reverse=True):
        s = s[:i + ns] + new + s[i + ne:]
    if a.apply:
        shutil.copy(a.board, a.board + '.bakNorm')
        open(a.board, 'w').write(s)
        print('written, backup .bakNorm')
    else:
        print('dry run, use --apply to write')


if __name__ == '__main__':
    main()
