#!/usr/bin/env python3
"""Fill the LCSC field from the part number (MPN) using a local JLCPCB database.

For JLCPCB assembly the LCSC number matters more than the MPN: it is what selects the
stock item. Stock is checked at the same time, since a BOM with correct numbers is not
yet an orderable BOM.

Database: a SQLite copy of the JLCPCB parts catalogue with a `components` table
(MFR_Part, LCSC, Library_Type, Price, Stock). Default: ~/.konnect/jlcpcb.db, the database
downloaded by the Konnect MCP server (download_jlcpcb_database); override with --db or
the KAT_JLCPCB_DB environment variable.
Only an exact MFR_Part match is written; failing that, a prefix match, and such cases
are flagged in the output. A part without a match stays empty: substituting a similar
number would mean a different part.

Usage:  python3 tools/lcsc_lookup.py [--root DIR] [--db PATH]            plan
        python3 tools/lcsc_lookup.py [--root DIR] [--db PATH] --apply    write
"""
import re, sys, os, glob, shutil, sqlite3, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project

_ap = argparse.ArgumentParser(description='Fill LCSC fields from a local JLCPCB database.')
_ap.add_argument('--apply', action='store_true')
_ap.add_argument('--root', default=paths.KAT_ROOT, help='directory with the schematic sheets (default: project dir)')
_ap.add_argument('--db', default=os.environ.get('KAT_JLCPCB_DB') or project.get('jlcpcb_db', '~/.konnect/jlcpcb.db'),
                 help='JLCPCB SQLite database (default: $KAT_JLCPCB_DB or ~/.konnect/jlcpcb.db, '
                      'the database downloaded by the Konnect MCP server)')
_a = _ap.parse_args()
APPLY = _a.apply
ROOT = os.path.abspath(_a.root)
DB = os.path.expanduser(_a.db)
if not os.path.exists(DB):
    raise SystemExit('JLCPCB database not found: %s (use --db or KAT_JLCPCB_DB)' % DB)
MIN_STOCK = 1

def blocks(s, tag):
    res = []
    for mm in re.finditer(r'\n\t\(' + re.escape(tag) + r'(?=[\s(])', s):
        i = mm.start() + 1; j = i; d = 0; q = False
        while j < len(s):
            c = s[j]
            if q:
                if c == '\\': j += 2; continue
                if c == '"': q = False
            else:
                if c == '"': q = True
                elif c == '(': d += 1
                elif c == ')':
                    d -= 1
                    if d == 0: res.append((i, j + 1)); break
            j += 1
    return res

def nested(blk, tag):
    res = []
    for mm in re.finditer(r'\(' + re.escape(tag) + r'(?=[\s(])', blk):
        i = mm.start(); j = i; d = 0; q = False
        while j < len(blk):
            c = blk[j]
            if q:
                if c == '\\': j += 2; continue
                if c == '"': q = False
            else:
                if c == '"': q = True
                elif c == '(': d += 1
                elif c == ')':
                    d -= 1
                    if d == 0: res.append((i, j + 1)); break
            j += 1
    return res

con = sqlite3.connect(DB)
cache = {}
def lookup(mpn):
    if mpn in cache: return cache[mpn]
    row = con.execute(
        "SELECT LCSC,Library_Type,Price,Stock FROM components WHERE MFR_Part=? AND Stock>=? "
        "ORDER BY (Library_Type='Basic') DESC, Stock DESC LIMIT 1", (mpn, MIN_STOCK)).fetchone()
    exact = bool(row)
    if not row:
        row = con.execute(
            "SELECT LCSC,Library_Type,Price,Stock FROM components WHERE MFR_Part LIKE ? AND Stock>=? "
            "ORDER BY (Library_Type='Basic') DESC, Stock DESC LIMIT 1", (mpn + '%', MIN_STOCK)).fetchone()
    cache[mpn] = (row, exact)
    return cache[mpn]

done, approx, missing, skipped = [], [], [], 0
for f in sorted(glob.glob(os.path.join(ROOT, '*.kicad_sch'))):
    s = open(f).read()
    out = []; prev = 0; changed = 0
    for a, b in blocks(s, 'symbol'):
        blk = s[a:b]
        rm = re.search(r'\(property "Reference" "([^"]*)"', blk)
        mp = re.search(r'\(property "MPN"\s+"([^"]*)"', blk)
        if not (rm and mp): continue
        lc = re.search(r'\(property "LCSC"\s+"([^"]*)"', blk)
        if lc and lc.group(1).strip():
            skipped += 1; continue
        row, exact = lookup(mp.group(1))
        if not row:
            missing.append((rm.group(1), mp.group(1))); continue
        # insert right after MPN
        span = None
        for pa, pb in nested(blk, 'property'):
            if blk[pa:pa+22].startswith('(property "MPN"'):
                span = (pa, pb); break
        if not span:
            missing.append((rm.group(1), mp.group(1) + ' - MPN property not found')); continue
        at = re.search(r'\(at ([-\d.]+) ([-\d.]+)', blk[span[0]:span[1]])
        x, y = (at.group(1), at.group(2)) if at else ('0', '0')
        prop = (f'\n\t\t\t(property "LCSC" "{row[0]}"\n'
                f'\t\t\t\t(at {x} {y} 0)\n'
                f'\t\t\t\t(effects\n\t\t\t\t\t(font\n\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t)\n'
                f'\t\t\t\t\t(hide yes)\n\t\t\t\t)\n\t\t\t)')
        ins = a + span[1]
        out.append(s[prev:ins]); out.append(prop); prev = ins
        rec = (rm.group(1), mp.group(1), row[0], row[1], row[2], row[3])
        (done if exact else approx).append(rec)
        changed += 1
    if changed and APPLY:
        out.append(s[prev:])
        shutil.copy(f, f + '.bakLCSC')
        open(f, 'w').write(''.join(out))

print(f'filled by exact match: {len(done)} | by prefix: {len(approx)} | '
      f'already set: {skipped} | not found: {len(missing)}')
LOW = 100
low = [r for r in done + approx if r[5] < LOW]
if low:
    print(f'\nlow stock (under {LOW} pcs), order separately:')
    for ref, mpn, lcsc, lib, price, stock in sorted(low, key=lambda r: r[5]):
        print(f'   {ref:7s} {mpn:22s} {lcsc:11s} stock {stock:6d}  ${price}')
if approx:
    print('\nprefix match, check that it is the same part:')
    for ref, mpn, lcsc, lib, price, stock in approx:
        print(f'   {ref:7s} {mpn:22s} -> {lcsc}')
if missing:
    print('\nnot in the JLCPCB database, needs an alternative or external supply:')
    for ref, mpn in sorted(set(missing)):
        print(f'   {ref:7s} {mpn}')
if not APPLY:
    print('\nplan only. To write: python3 tools/lcsc_lookup.py --apply')
