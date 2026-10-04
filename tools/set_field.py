#!/usr/bin/env python3
"""Set or change a component field in the schematic (LCSC, MPN, or any custom field).

  python3 set_field.py REF FIELD=VALUE [FIELD=VALUE ...] [--root DIR] [--board PCB] [--apply]

Why. Procurement fields (`LCSC`, `MPN`) are read by the stock check and the BOM export, so an
empty field goes straight into the fabrication kit: a single power inductor without an LCSC
code would be fitted "at the assembler's discretion". Hand edits across many sheets are a
source of unbalanced brackets, so this tool finds the sheet by reference, inserts next to the
existing fields, checks bracket balance and RE-MEASURES by reading the written file back (a
replacement counter is not trusted: a substitution that matched nothing still "succeeds").

The field value is hidden on the schematic (`hide yes`): it is procurement data, not a label.

**`--board` is required once the board is laid out.** Fields live both in the schematic and in
the footprint on the board, and `--schematic-parity` compares them: editing only the schematic
gives `footprint_symbol_field_mismatch: Missing symbol field 'LCSC' in footprint ...`. Updating
the board from the schematic (F8) cannot be scripted, so the field is also written into the
footprint here, next to the others: hidden, on F.Fab, with its own uuid, as KiCad itself does.
"""
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedit as E

# --root consumes ITS value: otherwise the path ends up in the field list and fails on split('=')
argv = sys.argv[1:]
ROOT = os.getcwd()
if '--root' in argv:
    i = argv.index('--root')
    ROOT = os.path.abspath(argv[i + 1])
    argv = argv[:i] + argv[i + 2:]
BOARD = None
if '--board' in argv:
    i = argv.index('--board')
    BOARD = os.path.abspath(argv[i + 1])
    argv = argv[:i] + argv[i + 2:]
APPLY = '--apply' in argv
args = [a for a in argv if not a.startswith('--')]
if len(args) < 2 or '=' not in ''.join(args[1:]):
    raise SystemExit(__doc__)
REF = args[0]
FIELDS = [a.split('=', 1) for a in args[1:]]

sheets = sorted(f for f in os.listdir(ROOT) if f.endswith('.kicad_sch'))
hits = []
for fn in sheets:
    s = open(os.path.join(ROOT, fn)).read()
    if re.search(r'\(property "Reference" "%s"' % re.escape(REF), s):
        hits.append(fn)
if not hits:
    raise SystemExit('ERROR: component %s is not in any sheet of %s' % (REF, ROOT))
if len(hits) > 1:
    raise SystemExit('ERROR: %s appears in several sheets: %s' % (REF, hits))
path = os.path.join(ROOT, hits[0])
s = open(path).read()
a, b = E.symbol_block_by_ref(s, REF)
blk = s[a:b]
print('%s: sheet %s, symbol block %d bytes' % (REF, hits[0], len(blk)))

# take the new field's position from Reference so KiCad does not put it at (0,0)
m = re.search(r'\(property "Reference" "[^"]*"\s*\n\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', blk)
px, py, rot = (m.group(1), m.group(2), m.group(3)) if m else ('0', '0', '0')

new = blk
for name, val in FIELDS:
    pm = re.search(r'(\(property "%s" ")([^"]*)(")' % re.escape(name), new)
    if pm:
        was = pm.group(2)
        if was == val:
            print('  %-6s already %r, unchanged' % (name, val))
            continue
        new = new[:pm.start(2)] + val + new[pm.end(2):]
        print('  %-6s %r -> %r' % (name, was, val))
    else:
        # insert after the last existing field: KiCad does not care about field order,
        # what matters is staying inside the symbol block
        last = None
        for pm2 in re.finditer(r'\n\t\t\(property ', new):
            last = pm2
        if last is None:
            raise SystemExit('ERROR: block %s has no property fields' % REF)
        _pa, pb = E.span(new, last.start() + 3)
        # E.prop already returns an indented "\t\t(property ..." line with a trailing newline
        text = '\n' + E.prop(name, val, float(px), float(py), hide=True, rot=int(float(rot))).rstrip('\n')
        new = new[:pb] + text + new[pb:]
        print('  %-6s + %r (new field)' % (name, val))

# The schematic may already be edited (e.g. a re-run just for --board): do not exit here,
# or the board sync is skipped and the parity finding stays open.
SCH_CHANGED = new != blk
if not SCH_CHANGED:
    print('  schematic: no changes')
out = s[:a] + new + s[b:]
# check_balance returns nothing and raises AssertionError: catch it explicitly
try:
    E.check_balance(out)
except AssertionError as e:
    raise SystemExit('ERROR: %s - write cancelled' % e)
if not APPLY:
    print('\nplan only. Add --apply to write'); sys.exit(0)
if SCH_CHANGED:
    open(path + '.bakField', 'w').write(s)
    open(path, 'w').write(out)
    # re-measure: read the written file back
    chk = open(path).read()
    ca, cb = E.symbol_block_by_ref(chk, REF)
    cblk = chk[ca:cb]
    for name, val in FIELDS:
        got = re.search(r'\(property "%s" "([^"]*)"' % re.escape(name), cblk)
        if not got or got.group(1) != val:
            raise SystemExit('ERROR after writing: %s = %r, expected %r'
                             % (name, got.group(1) if got else None, val))
    print('wrote %s, re-measured from file: fields in place' % path)

if not BOARD:
    print('WARNING: --board not given. If the board is laid out, --schematic-parity will report '
          '"Missing symbol field": the field is in the schematic but not in the footprint.')
    sys.exit(0)

# --- the same field into the footprint on the board
bs = open(BOARD).read()
fm = re.search(r'\(property "Reference" "%s"' % re.escape(REF), bs)
if not fm:
    raise SystemExit('ERROR: footprint %s not found in %s' % (REF, BOARD))
fa = bs.rfind('(footprint ', 0, fm.start())
_x, fb = E.span(bs, fa)
fblk = bs[fa:fb]
nb = fblk
for name, val in FIELDS:
    pm = re.search(r'(\(property "%s" ")([^"]*)(")' % re.escape(name), nb)
    if pm:
        if pm.group(2) == val:
            print('  board: %-6s already %r' % (name, val)); continue
        print('  board: %-6s %r -> %r' % (name, pm.group(2), val))
        nb = nb[:pm.start(2)] + val + nb[pm.end(2):]
    else:
        # insert before ki_fp_filters / the first non-property node, as KiCad writes it
        anchor = nb.find('\n\t\t(property ki_fp_filters')
        if anchor < 0:
            last = None
            for pm2 in re.finditer(r'\n\t\t\(property ', nb):
                last = pm2
            if last is None:
                raise SystemExit('ERROR: footprint %s has no property fields' % REF)
            _pa, anchor = E.span(nb, last.start() + 3)
        text = ('\n\t\t(property "%s" "%s"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n'
                '\t\t\t(hide yes)\n\t\t\t(uuid "%s")\n\t\t\t(effects\n\t\t\t\t(font\n'
                '\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n'
                '\t\t\t)\n\t\t)') % (name, val, uuid.uuid4())
        nb = nb[:anchor] + text + nb[anchor:]
        print('  board: %-6s + %r (new footprint field)' % (name, val))
if nb != fblk:
    out_b = bs[:fa] + nb + bs[fb:]
    try:
        E.check_balance(out_b)
    except AssertionError as e:
        raise SystemExit('ERROR: board bracket balance broken (%s) - write cancelled' % e)
    open(BOARD + '.bakField', 'w').write(bs)
    open(BOARD, 'w').write(out_b)
    chk_b = open(BOARD).read()
    bm = re.search(r'\(property "Reference" "%s"' % re.escape(REF), chk_b)
    cfa = chk_b.rfind('(footprint ', 0, bm.start())
    _y, cfb = E.span(chk_b, cfa)
    cfblk = chk_b[cfa:cfb]
    for name, val in FIELDS:
        got = re.search(r'\(property "%s" "([^"]*)"' % re.escape(name), cfblk)
        if not got or got.group(1) != val:
            raise SystemExit('ERROR after writing the board: %s = %r' % (name, got.group(1) if got else None))
    print('wrote %s, re-measured: footprint fields in place' % BOARD)
