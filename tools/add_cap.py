#!/usr/bin/env python3
"""Add a capacitor between a net and GND on a given schematic sheet.

Datasheet capacitance requirements are all closed the same way: add a part on the net,
place it on the board and route it. Reference, value, part codes and position are
arguments; the symbol template and the hierarchy (instances) block are taken from the
sheet ITSELF (from a model capacitor), so the field format matches the rest of the sheet
(KiCad silently drops a symbol whose field format is wrong).

Coordinates must be multiples of 1.27 mm, otherwise ERC reports endpoint_off_grid.

    python3 tools/add_cap.py --root R --sheet power.kicad_sch --ref C100 \\
        --value "100nF/50V" --footprint Capacitor_SMD:C_0402_1005Metric \\
        --mpn CL05B104KB54PNC --lcsc C307331 --net VIN --at 100.33,74.93 --model C10 --apply

Typical use: a datasheet asks for a small ceramic capacitor "as close as possible" to a
supply pin, while only bulk capacitors several millimetres away are present on that net.
The total capacitance looks fine, but the high-frequency current loop is not closed near
the pin, which shows up as switching overshoot on that pin. Check that the net is the one
that actually feeds the pin (e.g. the output of an upstream load switch, not the connector
net), and pick a voltage rating for the highest voltage the net can see.
"""
import argparse
import os
import re
import sys
import uuid as _uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import schedit as E

SYM_LIB = os.path.join(os.environ.get('KICAD_SYMBOL_DIR', '/usr/share/kicad/symbols'), 'Device.kicad_sym')
LIB_ID = 'Device:C'
DY = 3.81                     # pin end of Device:C from the symbol centre
REF = VALUE = FOOTPRINT = MPN = LCSC = NET = None
X = Y = None


def main():
    # global before the first use of the names
    global REF, VALUE, FOOTPRINT, MPN, LCSC, NET, X, Y
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--sheet', required=True)
    ap.add_argument('--ref', required=True)
    ap.add_argument('--value', required=True)
    ap.add_argument('--footprint', required=True)
    ap.add_argument('--mpn', default='')
    ap.add_argument('--lcsc', default='')
    ap.add_argument('--net', required=True)
    ap.add_argument('--at', required=True, help='x,y on the sheet, multiples of 1.27 mm')
    ap.add_argument('--model', required=True, help='reference of a model capacitor on this sheet')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    REF, VALUE, FOOTPRINT = a.ref, a.value, a.footprint
    MPN, LCSC, NET = a.mpn, a.lcsc, a.net
    if a.at:
        X, Y = (float(v) for v in a.at.split(','))
        for v, nm in ((X, 'x'), (Y, 'y')):
            if abs(round(v / 1.27) * 1.27 - v) > 1e-6:
                raise SystemExit('REFUSED: %s = %g is not a multiple of 1.27 mm; ERC would report endpoint_off_grid' % (nm, v))
    path = os.path.join(os.path.abspath(a.root), a.sheet)
    s = open(path).read()
    print('sheet: %s' % path)

    if '(property "Reference" "%s"' % REF in s:
        print('%s is already on the sheet, nothing to add' % REF)
        return 0

    a0, b0 = E.symbol_block_by_ref(s, a.model)    # model capacitor on this sheet
    im = re.search(r'\(instances.*?\n\t\t\)\n', s[a0:b0], re.S)
    if not im:
        raise SystemExit('REFUSED: %s has no (instances) block, hierarchy template unknown' % a.model)
    inst_tpl = im.group(0)
    gl = re.search(r'\t\(global_label "GND".*?\n\t\)\n', s, re.S)
    lab = re.search(r'\t\((?:global_)?label "%s".*?\n\t\)\n' % NET, s, re.S)
    if not (gl and lab):
        raise SystemExit('REFUSED: no model label for GND or %s on the sheet' % NET)

    if '(symbol "%s"' % LIB_ID not in s:
        s = E.add_cached_symbol(s, LIB_ID, E.extract_symbol(SYM_LIB, 'C'))
        print('symbol %s added to the sheet cache' % LIB_ID)

    props = [
        ('Reference', REF, X + 0.635, Y - 1.27, False, 0, 'left'),
        ('Value', VALUE, X + 0.635, Y + 1.27, False, 0, 'left'),
        ('Footprint', FOOTPRINT, X, Y, True, 0, 'left'),
        ('Datasheet', '', X, Y, True, 0, 'left'),
        ('MPN', MPN, X, Y, True, 0, 'left'),
        ('LCSC', LCSC, X, Y, True, 0, 'left'),
    ]
    blk = E.symbol_instance(LIB_ID, X, Y, 0, str(_uuid.uuid4()), props, ['1', '2'],
                            inst_tpl.replace('"%s"' % a.model, '"%s"' % REF))
    top = re.sub(r'\(at [-\d.]+ [-\d.]+ \d+\)', '(at %g %g 90)' % (X, Y - DY), lab.group(0))
    top = re.sub(r'\(uuid "[^"]*"\)', '(uuid "%s")' % _uuid.uuid4(), top, count=1)
    bot = re.sub(r'\(at [-\d.]+ [-\d.]+ \d+\)', '(at %g %g 270)' % (X, Y + DY), gl.group(0))
    bot = re.sub(r'\(uuid "[^"]*"\)', '(uuid "%s")' % _uuid.uuid4(), bot, count=1)
    s = E.insert_before_tail(s, blk + top + bot)
    print('  + %s "%s" at (%.2f, %.2f): label %s on top, GND below' % (REF, VALUE, X, Y, NET))

    try:
        E.check_balance(s)
    except AssertionError as e:
        raise SystemExit('REFUSED: %s - write cancelled' % e)
    if not a.apply:
        print('\nplan only. Add --apply to write')
        return 0
    open(path + '.bakAddCap', 'w').write(open(path).read())
    open(path, 'w').write(s)
    chk = open(path).read()
    ca, cb = E.symbol_block_by_ref(chk, REF)
    got = re.search(r'\(property "LCSC" "([^"]*)"', chk[ca:cb])
    if not got or got.group(1) != LCSC:
        raise SystemExit('ERROR after writing: LCSC = %r' % (got and got.group(1)))
    print('\nwrote %s' % path)
    print('next: netlist, sync_board.py --apply, place on the board (fit_cap.py) and route')
    return 0


if __name__ == '__main__':
    sys.exit(main())
