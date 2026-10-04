#!/usr/bin/env python3
"""Set LCSC / MPN (and optionally Value, Footprint, lib_id) on schematic components from a
table. Works on all sheets as text (schedit), without KiCad.

The table is JSON: {"R1": {"LCSC": "C25744", "MPN": "0402WGF1002TCE"}, ...}
Any key other than lib_id is written as a symbol property; lib_id swaps the symbol and
caches the new one from the KiCad library ($KICAD_SYMBOL_DIR, default /usr/share/kicad/symbols).

    python3 tools/set_lcsc.py --table work/lcsc.json            # plan
    python3 tools/set_lcsc.py --table work/lcsc.json --apply
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedit as E

SYM_DIR = os.environ.get('KICAD_SYMBOL_DIR', '/usr/share/kicad/symbols')


def set_prop_in_block(blk, name, value):
    m = re.search(r'\(property "%s" "[^"]*"' % re.escape(name), blk)
    if m:
        return blk[:m.start()] + '(property "%s" "%s"' % (name, value) + blk[m.end():], 'updated'
    # add a hidden property after Footprint
    m = re.search(r'\n\t\t\(property "Footprint" "[^"]*"', blk)
    assert m, 'block has no Footprint property'
    a, b = E.span(blk, m.start() + 3)
    at = re.search(r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', blk[a:b])
    new = ('\n\t\t(property "%s" "%s"\n\t\t\t(at %s %s 0)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n'
           '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)'
           % (name, value, at.group(1), at.group(2)))
    return blk[:b] + new + blk[b:], 'added'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--table', help='JSON table {REF: {FIELD: VALUE, ...}} (required)')
    ap.add_argument('--apply', action='store_true')
    # --root says WHICH sheets are edited. Without it the result would depend on the
    # directory the script is started from, which silently edits the wrong copy when
    # several board variants sit side by side.
    ap.add_argument('--root', default='.', help='directory with the schematic sheets (.kicad_sch)')
    a = ap.parse_args()
    if not a.table:
        ap.error('--table is required: a JSON file {"REF": {"LCSC": "...", "MPN": "..."}, ...}')
    table = json.load(open(a.table))
    root = os.path.abspath(a.root)
    print('sheets: %s' % root)
    sheets = {f: open(f).read() for f in sorted(glob.glob(os.path.join(root, '*.kicad_sch')))}
    if not sheets:
        raise SystemExit('REFUSED: no .kicad_sch in %s' % root)
    done, missing = 0, []
    for ref, props in table.items():
        hit = None
        for f, s in sheets.items():
            if '(property "Reference" "%s"' % ref in s:
                hit = f; break
        if not hit:
            missing.append(ref); continue
        s = sheets[hit]
        a0, b0 = E.symbol_block_by_ref(s, ref)
        blk = s[a0:b0]
        notes = []
        for name, value in props.items():
            if name == 'lib_id':
                old = re.search(r'\(lib_id "([^"]*)"\)', blk).group(1)
                if old != value:
                    blk = blk.replace('(lib_id "%s")' % old, '(lib_id "%s")' % value, 1)
                    lib, bare = value.split(':', 1)
                    sym = E.extract_symbol(os.path.join(SYM_DIR, '%s.kicad_sym' % lib), bare)
                    s_tmp = E.add_cached_symbol(s[:a0] + blk + s[b0:], value, sym)
                    # add_cached_symbol changes the whole sheet: recompute the block bounds
                    s = s_tmp
                    a0, b0 = E.symbol_block_by_ref(s, ref); blk = s[a0:b0]
                    notes.append('lib_id %s->%s' % (old, value))
                continue
            blk, how = set_prop_in_block(blk, name, value)
            notes.append('%s %s' % (name, how))
        s = s[:a0] + blk + s[b0:]
        sheets[hit] = s
        done += 1
        print('  %-6s %-22s %s' % (ref, hit, ', '.join(notes)))
    if missing:
        print('not found: %s' % ', '.join(missing))
    if not a.apply:
        print('plan only (%d components), use --apply to write' % done); return
    for f, s in sheets.items():
        if s != open(f).read():
            E.check_balance(s)
            shutil.copy2(f, f + '.bakLcsc')
            open(f, 'w').write(s)
            print('wrote %s' % f)


if __name__ == '__main__':
    main()
