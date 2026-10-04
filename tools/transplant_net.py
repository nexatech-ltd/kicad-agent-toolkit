#!/usr/bin/env python3
"""Transplant a net's copper from another board - same schematic, routed differently.

Why. Automatic routing on the project board can plateau, while a copy routed from
scratch (e.g. on a full fan-out) ends up worse overall - yet FOR SOME NETS its
solution is better. Taking those nets over gains the difference.

This is the same per-net acceptance as merge_ses.py, except the source is another
board instead of a router SES file. It works because the schematic is the same: net
names and pad positions match exactly.

A net's copper is replaced as a whole: a mix of two solutions within one net is not
consistent (each solution is consistent only within itself).
"""
import sys, os, re, argparse, shutil
import uuid as _uuid
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp, paths


def net_of(blk):
    nb = sexp.sub(blk, 'net')
    if not nb:
        return None
    v = sexp.parse(nb[0])[0]
    return v[1] if len(v) > 1 else None


def extract(txt, nets):
    """copper blocks of the given nets, with fresh uuids"""
    out = []
    for tag in ('segment', 'arc', 'via'):
        for i, j in sexp.blocks(txt, tag):
            blk = txt[i:j]
            if net_of(blk) in nets:
                blk = re.sub(r'\(uuid "[^"]*"\)', '(uuid "%s")' % _uuid.uuid4(), blk, count=1)
                out.append(blk)
    return out


def strip(txt, nets):
    n = 0
    for tag in ('segment', 'arc', 'via'):
        again = True
        while again:
            again = False
            for i, j in sexp.blocks(txt, tag):
                if net_of(txt[i:j]) in nets:
                    txt = txt[:i - 1] + txt[j:]
                    n += 1
                    again = True
                    break
    return txt, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--from-board', required=True, help='source board')
    ap.add_argument('--nets-file', required=True, help='list of nets, one per line')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    nets = {l.rstrip('\n') for l in open(a.nets_file) if l.strip()}
    src = open(a.from_board).read()
    dst = open(a.board).read()

    add = extract(src, nets)
    have = {net_of(b) for b in add}
    missing = nets - have
    print('nets to transplant: %d, copper found for %d' % (len(nets), len(have)))
    if missing:
        print('   no copper in the source: %s' % ', '.join(sorted(m.split('/')[-1] for m in missing)[:10]))
    dst, removed = strip(dst, nets)
    print('own copper removed: %d objects, transplanted: %d' % (removed, len(add)))
    k = dst.rstrip().rfind(')')
    dst = dst.rstrip()[:k] + '\n' + '\n'.join(add) + '\n)\n'
    sexp.parse(dst)
    if a.apply:
        shutil.copy(a.board, a.board + '.bakTransplant')
        open(a.board, 'w').write(dst)
        print('written, backup .bakTransplant')
    else:
        print('dry run, --apply to write')


if __name__ == '__main__':
    main()
