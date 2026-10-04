#!/usr/bin/env python3
"""Restore the newline before symbols inserted by scripts: `)\\t(symbol` -> `)\\n\\t(symbol`.

Why. Scripts add symbols with `schedit.insert_before_tail`, and some can end up in a sheet
without a newline before `\\t(symbol`. KiCad reads such a file fine (whitespace is not
significant in s-expressions), but text tools do not: `symbol_block_by_ref` used to find the
block start by the pattern `'\\n\\t(symbol\\n'` and, for such symbols, returned the block of
the PREVIOUS component. Reading a resistor's fields then returned the part code of the
neighbouring ferrite bead, and writing a field would have gone into the neighbour and
corrupted the BOM.

The function now verifies its result and fails with a clear message, but the formatting still
has to be fixed, or every edit of those symbols will trip over it.

    python3 tools/fix_sch_indent.py --root .            # plan
    python3 tools/fix_sch_indent.py --root . --apply
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import schedit as E


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    total = 0

    for fn in sorted(f for f in os.listdir(root) if f.endswith('.kicad_sch')):
        path = os.path.join(root, fn)
        s = open(path).read()
        spots = []
        for m in re.finditer(r'\t\(symbol\n\t\t\(lib_id', s):
            if m.start() > 0 and s[m.start() - 1] != '\n':
                r = re.search(r'\(property "Reference" "([^"]*)"', s[m.start():m.start() + 700])
                spots.append((m.start(), r.group(1) if r else '?'))
        if not spots:
            continue
        print('%-24s symbols without a newline: %d (%s)'
              % (fn, len(spots), ', '.join(ref for _p, ref in spots)))
        total += len(spots)
        if not a.apply:
            continue
        out = s
        for pos, _ref in sorted(spots, reverse=True):
            out = out[:pos] + '\n' + out[pos:]
        try:
            E.check_balance(out)
        except AssertionError as e:
            raise SystemExit('REFUSED on %s: %s' % (fn, e))
        open(path + '.bakIndent', 'w').write(s)
        open(path, 'w').write(out)
        # re-measure: every symbol now reads as its own block
        chk = open(path).read()
        for _pos, ref in spots:
            if ref.startswith('#'):
                continue
            aa, bb = E.symbol_block_by_ref(chk, ref)   # raises AssertionError if it is still the neighbour's
            got = re.search(r'\(property "Reference" "([^"]*)"', chk[aa:bb]).group(1)
            if got != ref:
                raise SystemExit('ERROR after writing %s: block %s has reference %s' % (fn, ref, got))
        print('  wrote %s, re-measured: every block reads as its own' % path)

    print('\ntotal spots: %d%s' % (total, '' if a.apply else ' (plan only, use --apply to write)'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
