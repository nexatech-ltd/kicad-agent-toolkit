#!/usr/bin/env python3
"""JLCPCB stock for every LCSC code in the schematic: availability, library type, price,
shortage.

Catches: a code missing from the catalog; stock below the build quantity; Extended parts
(charged extra at assembly); active parts with no code at all (left to the assembler's
discretion). Database: a local JLCPCB catalog snapshot (config key "jlcpcb_db", default
~/.konnect/jlcpcb.db); netlist in kicadsexpr format.

    python3 check_stock.py work/board.net [--min 20] [--boards 10]
"""
import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project
import schedit as E

DB = os.path.expanduser(project.get('jlcpcb_db', '~/.konnect/jlcpcb.db'))


def comps(netlist):
    s = open(netlist).read()
    out = []
    for m in re.finditer(r'\n\t\t\(comp\n', s):
        a, b = E.span(s, m.start() + 3)
        blk = s[a:b]
        ref = re.search(r'\(ref "([^"]*)"\)', blk).group(1)
        val = re.search(r'\(value "([^"]*)"\)', blk)
        fp = re.search(r'\(footprint "([^"]*)"\)', blk)
        dnp = '(dnp yes)' in blk or re.search(r'\(property\s*\(name "dnp"\)', blk) is not None
        fields = dict(re.findall(r'\(field\s*\(name "([^"]*)"\) "([^"]*)"\)', blk))
        out.append({'ref': ref, 'value': val.group(1) if val else '', 'fp': fp.group(1) if fp else '',
                    'dnp': dnp, 'lcsc': fields.get('LCSC', '').strip(), 'mpn': fields.get('MPN', '').strip()})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('netlist', nargs='?', default=paths.NETLIST)
    ap.add_argument('--min', type=int, default=20, help='stock alarm threshold, pcs')
    ap.add_argument('--boards', type=int, default=10, help='build quantity for the demand estimate')
    a = ap.parse_args()
    cs = [c for c in comps(a.netlist) if not c['ref'].startswith('#')]
    db = sqlite3.connect(DB)
    q = 'select MFR_Part, Library_Type, Stock, Price, Package from components where LCSC=?'
    by_code = {}
    for c in cs:
        if c['lcsc']:
            by_code.setdefault(c['lcsc'], []).append(c)
    bad, extended, total = [], 0, 0.0
    print('components %d, with LCSC code %d (unique %d), without code %d, DNP %d'
          % (len(cs), sum(1 for c in cs if c['lcsc']), len(by_code), sum(1 for c in cs if not c['lcsc']), sum(1 for c in cs if c['dnp'])))
    print('%-11s %-5s %-9s %-8s %-8s %s' % ('LCSC', 'qty', 'stock', 'type', 'price', 'refs'))
    for code, items in sorted(by_code.items(), key=lambda kv: kv[0]):
        need = sum(1 for c in items if not c['dnp']) * a.boards
        row = db.execute(q, (code,)).fetchone()
        refs = ','.join(c['ref'] for c in items)
        if row is None:
            bad.append('%s (%s): code not in the catalog' % (code, refs)); print('%-11s %-5d %-9s %-8s %-8s %s' % (code, len(items), 'NONE', '-', '-', refs)); continue
        mpn, lib, stock, price, pkg = row
        if lib == 'Extended':
            extended += 1
        total += price * sum(1 for c in items if not c['dnp'])
        flag = ''
        if stock < a.min:
            flag = ' <-- SHORTAGE'; bad.append('%s %s (%s): stock %d' % (code, mpn, refs, stock))
        elif stock < need:
            flag = ' <-- below build quantity %d' % need; bad.append('%s %s (%s): stock %d < %d' % (code, mpn, refs, stock, need))
        print('%-11s %-5d %-9d %-8s %-8.3f %s%s' % (code, len(items), stock, lib, price, refs[:40], flag))
    nolcsc = [c for c in cs if not c['lcsc'] and not c['dnp']]
    active = [c['ref'] for c in nolcsc if c['ref'][0] in 'UQDJLYFBS' and not c['ref'].startswith('TP')]
    passive = [c['ref'] for c in nolcsc if c['ref'][0] in 'RC']
    print('\nwithout LCSC code: active %d %s' % (len(active), sorted(active)))
    print('without LCSC code: passive %d (assembler picks a Basic part by value)' % len(passive))
    print('Extended codes: %d of %d; price of coded parts per board: $%.2f' % (extended, len(by_code), total))
    if bad:
        print('\nFAILED  stock/catalog: %d:' % len(bad))
        for b in bad:
            print('   ' + b)
        return 1
    print('\nPASSED  all codes in the catalog, stock at least %d pcs and at least the build of %d' % (a.min, a.boards))
    return 0


if __name__ == '__main__':
    sys.exit(main())
