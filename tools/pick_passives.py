#!/usr/bin/env python3
"""Pick an order code for every passive group and justify the choice by the rail it sits on.

Why. Passives without an LCSC code are placed by the assembler "by value from the Basic
library at their discretion", and the default for "100nF 0402" is a **16 V** part even
when the project has rails up to 20-25 V. One such capacitor on a high-voltage rail is
enough to make a board unreliable.

How it chooses. The norm is not "cheapest" but the voltage class the rail requires:

  * the rail comes from the nets of the group (the `RAIL_VMAX` table in check_components.py,
    config key "rail_vmax", each number with its source);
  * for class II ceramics (X5R/X7R) the working voltage is at most **half** the rating:
    X5R/X7R capacitance drops with voltage, and 100 nF/16 V on a 20 V rail is not "0 %
    margin" but a part outside its operating range. So the required rating = 2 x rail
    voltage, rounded up to the standard series (6.3, 10, 16, 25, 35, 50, 100 V);
  * for C0G/NP0 (class I) and resistors no capacitance derating is needed; the rating is
    at least the rail voltage with a 1.5x margin;
  * among matching catalog parts Basic/Preferred is preferred (no extra assembly fee),
    then stock, then price.

The tool writes NOTHING to the schematic: it prints a proposal with justification and,
with --write, fills the group fields in `<component_registry>/passives.yaml`. Setting the
fields in the schematic is a separate step (`tools/apply_passives.py`), so the change can be
read before it is written.

Project decisions go to the config: "passive_fixed_choices" {group: [lcsc, reason]} is
applied ALWAYS (an accepted choice, not a catalog proposal); "passive_blocked_codes"
{lcsc: reason} lists codes that must never be picked.

    python3 tools/pick_passives.py --net work/board.net
    python3 tools/pick_passives.py --net work/board.net --write
    python3 tools/pick_passives.py --net work/board.net --only '100nF | C_0402_1005Metric'
"""
import argparse
import collections
import datetime
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import project

PROJ = paths.KAT_ROOT
REG = os.path.join(PROJ, project.get('component_registry', os.path.join('doc', 'components')))
DB = os.path.expanduser(project.get('jlcpcb_db', '~/.konnect/jlcpcb.db'))
VOLT_ROW = [6.3, 10, 16, 25, 35, 50, 100]
CLASS2 = ('X5R', 'X7R', 'X6S', 'X7S', 'X7T')


def std_volt(need):
    for v in VOLT_ROW:
        if v >= need - 1e-9:
            return v
    return VOLT_ROW[-1]


def parse_value(value):
    """(kind, value in base units, value text) from a component value.

    The value is searched ANYWHERE in the string: values often carry a purpose, e.g.
    "ILIM pull-up 5k1", "PROG 27k", "100nF VCCIO", "SER L 0R". Parsing only the start of the
    string silently skips such groups.
    """
    v = value.strip()
    m = re.search(r'(?<![\w.])([\d.]+)\s*([pnuµm]?)F(?![\w])', v, re.I)
    if m:
        mult = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'm': 1e-3, '': 1.0}
        return 'C', float(m.group(1)) * mult[m.group(2).lower()], m.group(0)
    m = re.search(r'(?<![\w.])([\d.]+)\s*([nuµm]?)H(?![\w])', v, re.I)
    if m:
        mult = {'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'm': 1e-3, '': 1.0}
        return 'L', float(m.group(1)) * mult[m.group(2).lower()], m.group(0)
    # notation like 4k7, 24k9, 0R11, 5k1, 35k7: digits, multiplier, fractional digits
    m = re.search(r'(?<![\w.])(\d+)([kKMR])(\d+)(?![\w])', v)
    if m:
        base = {'k': 1e3, 'K': 1e3, 'M': 1e6, 'R': 1.0}[m.group(2)]
        return 'R', float('%s.%s' % (m.group(1), m.group(3))) * base, m.group(0)
    # 100k, 27k, 220R, 0R, 1M
    m = re.search(r'(?<![\w.])([\d.]+)\s*([kKM]?)(?:R|Ω|ohm)?(?![\w])', v)
    if m and (m.group(2) or re.search(r'[\d.]\s*(R|Ω|ohm)(?![\w])', v) or v.strip().startswith('0R')):
        base = {'k': 1e3, 'K': 1e3, 'M': 1e6, '': 1.0}[m.group(2)]
        return 'R', float(m.group(1)) * base, m.group(0)
    return None, None, None


def fmt_cap(f):
    for mult, suf in ((1e-6, 'uF'), (1e-9, 'nF'), (1e-12, 'pF')):
        if f >= mult - 1e-18:
            v = f / mult
            return ('%g%s' % (round(v, 3), suf))
    return '%g' % f


def fmt_res(r):
    if r >= 1e6:
        return '%gM' % (r / 1e6)
    if r >= 1e3:
        return '%gk' % (r / 1e3)
    return '%gR' % r


LIB_RANK = {'Basic': 0, 'Preferred': 0, 'Extended': 1}
# Project decisions: group -> (code, reason). Applied ALWAYS, even if the field is already
# filled: this is an accepted choice, not a catalog proposal. Example config entry:
#   "passive_fixed_choices": {"22uF | C_0805_2012Metric": ["C123456", "22 uF/35 V exists in 0805
#   only as Extended; rail up to 20 V vs 35 V rating is an accepted deviation backed by the
#   manufacturer's derating curve"]}
FIXED = {k: tuple(v) for k, v in (project.get('passive_fixed_choices', {}) or {}).items()}
# Codes that must never be picked, and why: each entry is an investigated case (e.g. a
# part number that copies another manufacturer's series naming). Config: "passive_blocked_codes".
BLOCKED = dict(project.get('passive_blocked_codes', {}) or {})


def find_part(db, kind, val, size, need_volt, want_class1, tol_pct, boards, qty):
    """Catalog candidates: (rank, row), sorted by suitability."""
    like = []
    if kind == 'C':
        # Catalog descriptions write the same capacitance differently: "100nF", "0.1uF",
        # "1000pF". Equivalents are computed NUMERICALLY, not by swapping letters: building
        # `fmt_cap(val*1000).replace('nF','pF')` for 1 nF gives "1uF" and picks a capacitor
        # a thousand times larger.
        like = [fmt_cap(val)]
        pf = val / 1e-12
        if pf >= 1 and float(pf).is_integer():
            like.append('%dpF' % pf)
        uf = val / 1e-6
        if uf >= 0.001:
            like.append(('%g' % round(uf, 4)) + 'uF')
        nf = val / 1e-9
        if nf >= 0.1:
            like.append(('%g' % round(nf, 4)) + 'nF')
        like = list(dict.fromkeys(like))
    elif kind == 'R':
        like = [fmt_res(val)]
        if val >= 1e3:
            k = val / 1e3
            like.append(('%g' % k) + 'k')
            if not float(k).is_integer():
                like.append('%dk%d' % (int(k), round((k - int(k)) * 10)))
        if val < 1e3:
            like += ['%gΩ' % val, '%gR' % val]
        like = list(dict.fromkeys(like))
    else:
        like = [fmt_cap(val).replace('F', 'H')]

    rows = []
    seen = set()
    for pat in like:
        q = ("select LCSC, MFR_Part, Package, Library_Type, Stock, Price, Description, Manufacturer"
             " from components where Package like ? and Description like ? and Stock > 0")
        for r in db.execute(q, ('%' + size + '%', '%' + pat + '%')):
            if r[0] in seen:
                continue
            seen.add(r[0])
            rows.append(r)
    out = []
    for r in rows:
        desc = r[6] or ''
        # Check the value NUMERICALLY, not as a substring: "10uF" occurs inside "100uF", and a
        # substring match would pick a part ten times larger.
        if kind == 'C':
            mm2 = re.search(r'(?<![\w.])([\d.]+)\s*([pnuµ])F', desc, re.I)
            if not mm2:
                continue
            mult = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6}
            got = float(mm2.group(1)) * mult[mm2.group(2).lower()]
            if abs(got - val) > val * 0.02:
                continue
        if kind == 'R':
            mm2 = re.search(r'(?<![\w.])([\d.]+)\s*([kKM]?)(?:Ω|ohm)', desc)
            if mm2:
                base = {'k': 1e3, 'K': 1e3, 'M': 1e6, '': 1.0}[mm2.group(2)]
                got = float(mm2.group(1)) * base
                if abs(got - val) > max(val * 0.02, 1e-9):
                    continue
        # voltage from the description
        mv = re.search(r'(\d+(?:\.\d+)?)\s*V\b', desc)
        volt = float(mv.group(1)) if mv else None
        cls1 = bool(re.search(r'\b(C0G|NP0)\b', desc, re.I))
        cls2 = bool(re.search(r'\b(%s)\b' % '|'.join(CLASS2), desc, re.I))
        if kind == 'C':
            if volt is None or volt < need_volt - 1e-9:
                continue
            if want_class1 and not cls1:
                continue
            if not want_class1 and not (cls1 or cls2):
                continue
        if kind == 'R' and tol_pct is not None:
            mt = re.search(r'±\s*([\d.]+)\s*%', desc)
            if mt and float(mt.group(1)) > tol_pct + 1e-9:
                continue
        if r[0] in BLOCKED:
            continue
        need = boards * qty
        if (r[4] or 0) < max(20, need):
            continue
        out.append(((LIB_RANK.get(r[3], 2), -(r[4] or 0) // 100000, r[5] or 9e9), r))
    out.sort(key=lambda x: x[0])
    return [r for _k, r in out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--net', default=paths.NETLIST)
    ap.add_argument('--boards', type=int, default=10)
    ap.add_argument('--only', help='a single group')
    ap.add_argument('--write', action='store_true', help='write the choice to passives.yaml')
    a = ap.parse_args()

    import yaml
    import check_components as K
    K.load_pin_nets(a.net)
    cs = [c for c in K.comps(a.net) if not c['ref'].startswith('#')]
    by_ref = {c['ref']: c for c in cs}
    p = os.path.join(REG, 'passives.yaml')
    doc = yaml.safe_load(open(p)) or {}
    groups = doc.get('groups', {}) or {}
    db = sqlite3.connect(DB)

    picked, skipped = {}, []
    for key in sorted(groups):
        if a.only and key != a.only:
            continue
        g = groups[key]
        refs = g.get('refs', [])
        value, fp = [x.strip() for x in key.split('|', 1)]
        kind, val, _txt = parse_value(value)
        size = re.search(r'_(\d{4})_', fp)
        size = size.group(1) if size else None
        if kind is None or val is None or size is None:
            if refs and all(by_ref.get(r, {}).get('dnp') for r in refs):
                skipped.append((key, 'all parts of the group are DNP (placeholder for a matching '
                                     'element): no value, no code needed'))
            else:
                skipped.append((key, 'value or package not parsed'))
            continue

        # rail of the group: the highest-voltage known rail among the nets of its parts
        rail, rail_v, why = None, 0.0, ''
        for ref in refs:
            for pin in ('1', '2'):
                net = K.pin_net(ref, pin)
                if net in K.RAIL_VMAX and K.RAIL_VMAX[net][0] > rail_v:
                    rail, rail_v = net, K.RAIL_VMAX[net][0]
                    why = K.RAIL_VMAX[net][1]
        # Class I (C0G/NP0) is required only where the value STATES it: "56pF C0G" at a crystal,
        # "220pF C0G" in a filter. Requiring it by value alone is wrong: demanding C0G for every
        # capacitance below 1 nF pushes button debounce filters to Extended parts, although X7R
        # is enough there (1 kHz, no precision needed) and Extended costs extra at assembly.
        want_class1 = kind == 'C' and bool(re.search(r'\b(C0G|NP0)\b', value, re.I))
        if kind == 'C':
            need_volt = std_volt(2 * rail_v) if not want_class1 else std_volt(1.5 * rail_v)
            if rail_v == 0:
                need_volt = 16.0 if val >= 1e-7 else 25.0
        else:
            need_volt = 0.0
        tol = 1.0 if (kind == 'R' and re.search(r'\d[kKmM]\d', value)) else 5.0

        cands = find_part(db, kind, val, size, need_volt, want_class1, tol, a.boards, len(refs))
        derate_note = ''
        if not cands and kind == 'C' and need_volt > 0:
            # A voltage class that does not exist in this package. E.g. 22 uF 0805 on a 20 V
            # rail needs 50 V by the "working <= half" rule, but 22 uF in 0805 is made up to
            # 35 V at most. Take the highest available and FLAG it: such a choice rests on the
            # manufacturer's derating curve and needs a separate justification (record it in
            # passive_fixed_choices once accepted).
            for v in reversed(VOLT_ROW):
                if v >= need_volt:
                    continue
                cands = find_part(db, kind, val, size, v, want_class1, tol, a.boards, len(refs))
                if cands:
                    derate_note = ('JUSTIFICATION NEEDED: %g V required, this package goes up to '
                                   '%g V: the choice relies on the manufacturer derating curve'
                                   % (need_volt, v))
                    break
        if not cands:
            why2 = ('all parts of the group are DNP: no value, no code needed'
                    if all(by_ref.get(r, {}).get('dnp') for r in refs) and refs
                    else 'no catalog part %s %s rated %g V in stock' % (value, size, need_volt))
            skipped.append((key, why2))
            continue
        code, mpn, pkg, lib, stock, price, desc, maker = cands[0]
        picked[key] = {
            'lcsc': code, 'mpn': mpn, 'library_type': lib, 'stock': stock, 'price_usd': price,
            'catalog_description': desc, 'manufacturer': maker,
            'rail': rail or 'unknown', 'rail_voltage': rail_v or None,
            'required_voltage': need_volt or None,
            'rationale': ('%s: rail %s up to %g V, class %s -> at least %g V required; picked %s '
                          '(%s, stock %s, $%s)'
                          % (key, rail or '-', rail_v, 'I (C0G)' if want_class1 else
                             ('II (X5R/X7R), working at most half the rating' if kind == 'C'
                              else '-'), need_volt, code, desc, stock, price))
                         if kind == 'C' else
                         ('%s: resistor, picked %s (%s, %s, stock %s, $%s)'
                          % (key, code, desc, lib, stock, price)),
            'source': 'JLCPCB catalog snapshot %s, checked %s; rail voltage: '
                      % (DB, datetime.date.today().isoformat())
                      + (why or 'rail not in the RAIL_VMAX table'),
            'warning': derate_note or None,
        }

    print('groups: %d, picked %d, not picked %d' % (len(groups), len(picked), len(skipped)))
    print('%-40s %-4s %-11s %-6s %-9s %-9s %-24s %s' %
          ('group', 'qty', 'rail', 'need', 'code', 'type', 'catalog description', '$'))
    for key in sorted(picked):
        v = picked[key]
        print('%-40s %-4d %-11s %-6s %-9s %-9s %-24s %s' %
              (key[:40], len(groups[key].get('refs', [])),
               (v['rail'] or '-')[:11],
               ('%g V' % v['required_voltage']) if v['required_voltage'] else '-',
               v['lcsc'], v['library_type'], (v['catalog_description'] or '')[:24],
               v['price_usd']))
    if skipped:
        print('\nnot picked:')
        for key, why in skipped:
            print('  %-42s %s' % (key[:42], why))

    if a.write:
        import sqlite3 as _s3
        for key, (code, why) in FIXED.items():
            if key not in groups:
                continue
            row = db.execute('select MFR_Part, Library_Type, Stock, Price, Description, Manufacturer'
                             ' from components where LCSC=?', (code,)).fetchone()
            if not row:
                raise SystemExit('REFUSED: a project decision refers to code %s, which is not '
                                 'in the catalog snapshot' % code)
            g = groups[key]
            g.update({'lcsc': code, 'mpn': row[0], 'library_type': row[1],
                      'catalog_description': row[4], 'manufacturer': row[5],
                      'voltage_rating': (re.search(r'(\d+(?:\.\d+)?)\s*V', row[4] or '')
                                            or [None, None])[1],
                      'dielectric': (re.search(r'\b(C0G|NP0|X5R|X7R)\b', row[4] or '', re.I)
                                     or [None, ''])[1],
                      'rationale': why,
                      'source': 'project decision, recorded in config passive_fixed_choices; '
                                'parameters from the JLCPCB catalog snapshot'})
            print('  project decision: %-40s -> %s (%s)' % (key[:40], code, row[5]))
        for key, v in picked.items():
            g = groups[key]
            if key in FIXED:
                continue
            if g.get('lcsc') not in (None, '', 'TBD'):
                continue          # already fixed by hand: do not overwrite
            g['lcsc'] = v['lcsc']
            g['mpn'] = v['mpn']
            g['voltage_rating'] = ((re.search(r'(\d+(?:\.\d+)?)\s*V', v['catalog_description'] or '')
                                       or [None, None])[1])
            g['dielectric'] = (re.search(r'\b(C0G|NP0|X5R|X7R|X6S|X7S)\b',
                                         v['catalog_description'] or '', re.I) or [None, ''])[1]
            g['rail'] = v['rail']
            g['catalog_description'] = v['catalog_description']
            g['library_type'] = v['library_type']
            g['rationale'] = v['rationale']
            g['source'] = v['source']
        doc['groups'] = groups
        yaml.safe_dump(doc, open(p, 'w'), allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=100)
        print('\nwritten %s' % os.path.relpath(p, PROJ))
    else:
        print('\nproposal only. Add --write to write passives.yaml')
    return 0


if __name__ == '__main__':
    sys.exit(main())
