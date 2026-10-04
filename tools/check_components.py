#!/usr/bin/env python3
"""Check schematic components against a component registry (`doc/components/`).

Facts come from the netlist, the JLCPCB catalog snapshot and (for packages) the board; the norm
comes from the registry, where every number carries a `source` naming a datasheet section or
table. The check does NOT parse PDFs: that happens when the registry is filled in, here only
YAML, the netlist and the board are read - so it is cheap enough to live in the gate.

Axes and what they catch:
  COV  coverage    every schematic reference is covered by a part file, a passive group or a
                   "not purchased" entry; otherwise the component is not checked at all
  ORD  ordering    order code present, present in the catalog, the catalog returns THE SAME part,
                   stock covers the build, the registry does not mark the part as discontinued
  DOC  document    the datasheet link points to the installed part; the instance field and the
                   symbol cache `docs` do not contradict each other (the two records can drift
                   apart: the instance pointing at the right vendor, the cache at a vendor
                   selection guide)
  VOLT voltage     rail working voltage vs the part voltage rating, with derating for class II
                   ceramics (X5R/X7R lose capacitance: working voltage at most half the rating -
                   this is how a "100uF/35V 0805" that does not exist gets caught)
  THERM thermal    Tj = Ta + Pd x RthJA vs the junction limit
  PKG  package     signal pad count of the footprint vs the datasheet (catches e.g. a 40-pin
                   connector ordered for an 18-pin land pattern)
  PIN  pins        symbol (number, name) vs the datasheet pinout
  REQ  required    capacitance on a pin within the window the datasheet requires (catches e.g.
                   missing 200-480 pF on the CC1/CC2 pins of a USB-PD controller)
  POL  polarity    a pin with a given role is not on a forbidden net (catches unidirectional
                   TVS diodes fitted cathode-to-ground)

    python3 check_components.py                      # netlist from paths.NETLIST, no board
    python3 check_components.py --board board.kicad_pcb
    python3 check_components.py --coverage           # coverage map only
    python3 check_components.py --allow-incomplete   # registry gaps do not fail the exit code

Exit code: 0 - no defects (and the registry is complete unless --allow-incomplete), else 1.

Accepted deviations are waivers of check "check_components" in kicad-agent.json, keyed
"<axis>:<ref or group>", e.g. "DOC-4:D4", "VOLT:22uF/35V | C_0805_2012Metric", "REQ-4:U2",
"REQ:U2:CC1".
"""
import argparse
import collections
import math
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import project
import schedit as E
from check_stock import comps

REG = os.path.abspath(os.path.join(paths.KAT_ROOT,
                                   project.get('component_registry', os.path.join('doc', 'components'))))
DB = os.path.expanduser(project.get('jlcpcb_db', '~/.konnect/jlcpcb.db'))
TA = float(project.get('ambient_temp_c', 40.0))   # ambient for the thermal estimate, deg C
CLASS2_DERATE = 0.5   # X5R/X7R: working voltage at most half the rating (JEITA derating)

# Deviations accepted by decision, with a numeric justification. Key - (axis, ref or group).
ACCEPTED = {}
for _k, _why in project.waivers('check_components').items():
    _axis, _, _what = _k.partition(':')
    ACCEPTED[(_axis, _what)] = _why


def part_for(mpn, parts):
    """Registry file for a part: exact slug first, then a compatible prefix.

    Required because the schematic MPN and the catalog order number legitimately differ by a
    packaging suffix (schematic `XYZ123`, file `xyz123rqmr.yaml`). With exact-slug matching only,
    a whole registry file can go unfound, and EVERY check against it silently returns "nothing to
    check" - including a missing capacitance on a supply pin.
    """
    if not mpn:
        return None
    key = re.sub(r'[^a-z0-9.+-]+', '_', mpn.lower()).strip('_')
    if key in parts:
        return parts[key]
    for k, v in parts.items():
        if same_prefix(k, key) or same_prefix(str(v.get('mpn', '')), mpn):
            return v
    return None


def load_registry():
    """Registry: index, passive groups, non-purchased items and part files by slug."""
    import yaml
    def y(name):
        p = os.path.join(REG, name)
        return (yaml.safe_load(open(p, encoding='utf-8')) if os.path.exists(p) else None) or {}
    idx = y('index.yaml')
    parts = {}
    for fn in sorted(os.listdir(REG)) if os.path.isdir(REG) else []:
        if fn.endswith('.yaml') and fn not in ('index.yaml', 'passives.yaml', 'not-purchased.yaml'):
            d = y(fn)
            if d.get('mpn'):
                parts[re.sub(r'\.yaml$', '', fn)] = d
    return idx, y('passives.yaml').get('groups', {}) or {}, \
        y('not-purchased.yaml').get('categories', {}) or {}, parts


def netlist_facts(net):
    """References, fields and symbol pinouts from the netlist."""
    cs = [c for c in comps(net) if not c['ref'].startswith('#')]
    s = open(net).read()
    # instance Datasheet field and the libpart it is bound to
    for c in cs:
        m = re.search(r'\(comp\s*\(ref "%s"\)' % re.escape(c['ref']), s)
        a, b = E.span(s, m.start())
        blk = s[a:b]
        f = dict(re.findall(r'\(field\s*\(name "([^"]*)"\) "([^"]*)"\)', blk))
        c['datasheet'] = f.get('Datasheet', '').strip()
        lp = re.search(r'\(libsource\s*\(lib "([^"]*)"\)\s*\(part "([^"]*)"\)', blk)
        c['libpart'] = '%s:%s' % lp.groups() if lp else ''
    # pinout and docs of each libpart
    lib = {}
    for m in re.finditer(r'\(libpart\n\t+\(lib "([^"]*)"\)\n\t+\(part "([^"]*)"\)', s):
        a, b = E.span(s, m.start())
        blk = s[a:b]
        docs = re.search(r'\(docs "([^"]*)"\)', blk)
        pins = [(pm.group(1), pm.group(2), pm.group(3)) for pm in re.finditer(
            r'\(pin\n\t+\(num "([^"]*)"\)\n\t+\(name "([^"]*)"\)\n\t+\(type "([^"]*)"\)', blk)]
        lib['%s:%s' % (m.group(1), m.group(2))] = {'docs': docs.group(1) if docs else '', 'pins': pins}
    return cs, lib


def catalog():
    if not os.path.exists(DB):
        return None
    db = sqlite3.connect(DB)
    return lambda code: db.execute(
        'select MFR_Part, Library_Type, Stock, Price, Package, Manufacturer, Datasheet '
        'from components where LCSC=?', (code,)).fetchone()


def norm(s):
    return re.sub(r'[^A-Za-z0-9]', '', s or '').upper()


def board_geometry(board, refs=None):
    """Pad and body positions from the board: {(ref, number): (x, y)} and {ref: (x, y)}.

    Needed for requirements like "place ceramic as close as possible to the pin": the sum of
    capacitance on a net does not check that - 20 uF six millimetres away formally satisfies it
    while the high-frequency loop stays open.
    """
    import pcbnew
    b = pcbnew.LoadBoard(board)
    pads, bodies = {}, {}
    for fp in b.GetFootprints():
        r = fp.GetReference()
        bodies[r] = (fp.GetPosition().x / 1e6, fp.GetPosition().y / 1e6)
        for p in fp.Pads():
            num = p.GetNumber()
            if num:
                pads[(r, num)] = (p.GetPosition().x / 1e6, p.GetPosition().y / 1e6)
    return pads, bodies


def pad_count(board, refs):
    """Signal pads of board footprints: {ref: count}. Mechanical pads (no number) do not count -
    otherwise a connector with mounting tabs (e.g. 16 contacts + 4 shell pads) is a false hit."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    out = {}
    for fp in b.GetFootprints():
        r = fp.GetReference()
        if refs and r not in refs:
            continue
        # Count UNIQUE numbers, not objects: a WQFN thermal pad may be split into several pieces
        # sharing one number, and counting objects would overstate the pin count. Pads that are
        # only on F.Paste (paste windows from the vendor drawing) have no copper and are not pins.
        nums = set()
        for p in fp.Pads():
            num = p.GetNumber()
            if not num or num.startswith('MP'):
                continue
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                continue
            if not p.IsOnCopperLayer():
                continue
            nums.add(num)
        out[r] = len(nums)
    return out


def _rail_table():
    """Maximum working voltage of rails from config `rail_vmax`: {net: [volts, source]} or
    {net: volts}. Each number should cite where it comes from (architecture doc, PD contract,
    battery chemistry, regulator setpoint)."""
    out = {}
    for k, v in (project.get('rail_vmax', {}) or {}).items():
        if isinstance(v, (list, tuple)):
            out[k] = (float(v[0]), str(v[1]) if len(v) > 1 else '')
        else:
            out[k] = (float(v), '')
    return out


# Maximum working voltage of rails: {net: (volts, source)}.
RAIL_VMAX = _rail_table()
# Imperial case size from a KiCad footprint name: C_0805_2012Metric -> 0805
IMPERIAL = re.compile(r'_(\d{4})_\d{4}Metric')


def nets_by_ref(net):
    """{ref: {net name, ...}} from the (nets) section - used to find the rail a part sits on."""
    s = open(net).read()
    out = collections.defaultdict(set)
    for m in re.finditer(r'\(net\n\t+\(code "\d+"\)\n\t+\(name "([^"]*)"\)', s):
        a, b = E.span(s, m.start())
        name = m.group(1).split('/')[-1]
        for nm in re.finditer(r'\(node\s*\(ref "([^"]*)"\)', s[a:b]):
            out[nm.group(1)].add(name)
    return out


def rail_of(names):
    """The highest-voltage known rail among the part's nets."""
    best = None
    for n in names:
        if n in RAIL_VMAX and (best is None or RAIL_VMAX[n][0] > RAIL_VMAX[best][0]):
            best = n
    return best


class Report:
    def __init__(self):
        self.defects, self.gaps, self.notes = [], [], []

    def defect(self, code, ref, msg):
        self.defects.append((code, ref, msg))

    def gap(self, code, ref, msg):
        """Registry incomplete: nothing to check against. Not a schematic defect, not a pass."""
        self.gaps.append((code, ref, msg))

    def note(self, code, ref, msg):
        """A reviewed project decision: checked and found intended, but must stay visible.

        Separate from `gap` because the meaning differs: there "nothing to check against", here
        "checked, and this is by design". Example: datasheet pins of a module that are not drawn
        in its symbol (unused NC, second SIM, SD/MMC, audio...). Printing them as defects trains
        the reader to skim past defects.
        """
        self.notes.append('%s %s - %s' % (code, ref, msg))


PASSIVE_FP = re.compile(r'(R|C|L|FB)_\d{4}_')


def nominal_forms(kind, val):
    """How this value may be written in a catalog description.

    Comparing values with a "general parser" over the description is NOT possible: a catalog line
    starts with "-55C~+155C", and the first attempt to parse it as a number read 55 ohm instead of
    0 ohm - dozens of false defects. So we check for the PRESENCE of one of the spellings.
    """
    out = []
    if kind == 'C':
        for mult, suf in ((1e-12, 'pF'), (1e-9, 'nF'), (1e-6, 'uF')):
            v = val / mult
            if 0.999 <= v < 100000:
                out.append(('%g' % round(v, 3)) + suf)
    elif kind == 'R':
        # the milli spelling is required: a "0R11" shunt is described as "110mΩ" in the catalog
        for base, suf in ((1e-3, 'm'), (1.0, ''), (1e3, 'k'), (1e6, 'M')):
            v = val / base
            if 0.0999 <= v < 1000 or (val == 0 and base == 1.0):
                t = '%g' % round(v, 3)
                out += [t + suf + 'Ω', t + suf + 'R', t + suf + ' Ohm', t + suf + 'Ohm']
                if suf:
                    out.append(t + suf)
    else:
        for mult, suf in ((1e-9, 'nH'), (1e-6, 'uH')):
            v = val / mult
            if 0.999 <= v < 100000:
                out.append(('%g' % round(v, 3)) + suf)
    return list(dict.fromkeys(out))


def check_passive_code(rep, cs, cat):
    """A passive's order code must match its value and case size.

    For a passive the code and MPN are all that goes to assembly. `check_stock.py` only checks
    that the code exists and is in stock, so a valid code of an existing but WRONG part would
    pass silently: the resistor value and a buzzer's code are each "valid" on their own.
    """
    if not cat:
        return
    sys.path.insert(0, HERE)
    from pick_passives import parse_value
    import sqlite3
    db2 = sqlite3.connect(DB) if os.path.exists(DB) else None
    for c in cs:
        fp = c['fp'].split(':')[-1]
        if not PASSIVE_FP.search(fp) or not c['lcsc']:
            continue
        kind, val, _txt = parse_value(c['value'])
        if kind is None:
            continue
        row = cat(c['lcsc'])
        if row is None:
            continue                    # caught by ORD-2
        mfr, _lib, _stock, _price, pkg = row[0], row[1], row[2], row[3], row[4]
        desc = ''
        if db2:
            r2 = db2.execute('select Description from components where LCSC=?',
                             (c['lcsc'],)).fetchone()
            desc = (r2[0] if r2 else '') or ''
        size = re.search(r'_(\d{4})_', fp)
        size = size.group(1) if size else None
        if size and size not in re.sub(r'[^0-9]', '', str(pkg)):
            rep.defect('ORD-8', c['ref'],
                       'code %s (%s) - catalog package "%s", footprint %s'
                       % (c['lcsc'], mfr, pkg, fp))
            continue
        forms = nominal_forms(kind, val)
        hay = (desc + ' ' + str(mfr)).lower().replace(' ', '')
        if forms and not any(f.lower().replace(' ', '') in hay for f in forms):
            rep.defect('ORD-8', c['ref'],
                       'value "%s" (%s) vs code %s (%s, "%s") - value not in the description; '
                       'expected one of %s'
                       % (c['value'], c['ref'], c['lcsc'], mfr, desc[:40], ', '.join(forms[:4])))


def check_orderability(rep, cs, cat, parts, boards, min_stock):
    for c in cs:
        ref, mpn, code = c['ref'], c['mpn'], c['lcsc']
        if not mpn:
            continue          # passives and non-purchased items are checked by groups, below
        if not code:
            rep.defect('ORD-1', ref, 'purchased part %s has no LCSC code' % mpn)
            continue
        row = cat(code) if cat else None
        if row is None:
            rep.defect('ORD-2', ref, 'code %s not in the catalog snapshot' % code)
            continue
        mfr, libtype, stock, price, pkg, maker, ds = row
        if norm(mfr) != norm(mpn) and norm(mpn) not in norm(mfr) and norm(mfr) not in norm(mpn):
            rep.defect('ORD-3', ref, 'catalog returns %s for %s, schematic has %s' % (mfr, code, mpn))
        need = boards * sum(1 for x in cs if x['lcsc'] == code and not x['dnp'])
        if not c['dnp'] and (stock or 0) < max(min_stock, need):
            rep.defect('ORD-4', ref, 'stock of %s: %s pcs for a need of %d on %d boards'
                       % (code, stock, need, boards))
        life = ((part_for(mpn, parts) or {}).get('availability', {}) or {}).get('lifecycle', '')
        if life and life.lower() in ('eol', 'nrnd', 'obsolete', 'discontinued'):
            rep.defect('ORD-6', ref, 'registry marks the part as %s' % life)


def words(url):
    """Candidate part numbers from a document address.

    Words between separators are not enough: a name like AB-1234-C7.pdf splits into AB, 1234 and
    C7 - each shorter than any sane threshold, and part AB-1234-C7 cannot be recognised from them.
    So concatenations of consecutive pieces are candidates too: "mod-x5-wroom-1_..." yields
    MODX5WROOM1, the start of part number MOD-X5-WROOM-1-N16R8.
    """
    parts = [norm(w) for w in re.split(r'[^A-Za-z0-9]+', url or '') if norm(w)]
    out = set()
    for i in range(len(parts)):
        glued = ''
        for j in range(i, min(i + 6, len(parts))):
            glued += parts[j]
            # Six-character threshold, as in tools/fetch_datasheet.py. Five is already risky:
            # in the USB spec address (usb_type-c.zip) the piece TYPEC would confirm a connector
            # TYPE-C-31-M-12, though the document is about the interface, not the part.
            if len(glued) >= 6:
                out.add(glued)
    return out


def series_match(cand, mpn):
    """A document for a masked series: SP05XXBA covers SP0504BAHTG.

    A mask is recognised only for two or more consecutive X - that is how series numbers are
    written in datasheets (SP05xxBA, TPS6xxxx). A single X is too common as a real letter of a
    part number to treat as a wildcard.
    """
    if 'XX' not in cand or len(cand) > len(mpn):
        return False
    return all(c == 'X' or c == mpn[i] for i, c in enumerate(cand))


def same_prefix(a, b):
    """Strict comparison: one part number is a prefix of the other, no other allowance.

    Used where the part number is written without surrounding noise - the library symbol name.
    A soft rule is not acceptable here: PESD5V0S1UL and PESD5V0S1UB share 10 of 11 characters,
    so by percentage they are "one part", yet they are different devices from different makers.
    """
    a, b = norm(a), norm(b)
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def same_part(a, b):
    """Whether two part numbers agree - soft, for a number extracted from a document address.

    Three kinds of agreement, all seen in practice:
      * one is a prefix of the other: 74LVC1G14 vs 74LVC1G14GW,125 (packaging suffix), or a
        module series document vs a full ordering code;
      * series mask: SP05xxBA vs SP0504BAHTG;
      * a common prefix of at least 70 % of the part number (and at least five characters): a
        vendor document number like ABC123DS1 for part ABC123P is only linked this way.
    The percentage rule also rejects "another variant of the same family": an MCU datasheet named
    for one package variant shares too short a prefix with a different variant and is honestly
    reported as foreign.
    """
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a.startswith(b) or b.startswith(a):
        return True
    if series_match(a, b) or series_match(b, a):
        return True
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    return k >= max(5, int(round(0.7 * len(b))))


def doc_about(url, mpn):
    """Whether the address points to a document about THIS part."""
    return any(same_part(w, mpn) for w in words(url))


def clean(url):
    """KiCad stores "no value" as ~ - for the check that is a missing link, not an address."""
    u = (url or '').strip()
    return '' if u in ('', '~') else u


def doc_in_registry(inst, part):
    """Whether the Datasheet field points to the document recorded in this part's registry file.

    Stronger than any name heuristic: the registry stores the document with its number, revision
    and sha256, i.e. it was verified by content when fetched (tools/fetch_datasheet.py searches
    the part number in the text). Also needed because a document may cover a family (named e.g.
    "XYZ573xI") and cannot be linked to a specific ordering code by file name alone.
    """
    d = (part or {}).get('document') or {}
    ref = [str(d.get('url') or ''), str(d.get('file') or '')]
    tail = os.path.basename(str(d.get('file') or ''))
    u = inst.strip()
    return any(r and (u == r or u.endswith(r)) for r in ref) or bool(tail and u.endswith(tail))


def check_docs(rep, cs, lib, parts=None):
    """The document must be about the installed part - in both records of it.

    There are two datasheet records: the instance field in the schematic and the symbol cache
    `docs` (the latter is what the netlist prints, i.e. what the BOM generator and a human in the
    editor see). They can drift apart in either direction: the instance correct and the cache
    pointing at a vendor selection guide (useless for verifying the device), or the cache correct
    and the instance pointing at a different package variant. So the defect is not a mismatch of
    addresses as such, but an address leading to another part.
    """
    seen_sym = set()
    for c in cs:
        mpn = c['mpn']
        if not mpn:
            continue
        inst = clean(c['datasheet'])
        lp = c['libpart']
        docs = clean((lib.get(lp) or {}).get('docs', ''))
        part = lp.split(':', 1)[1] if ':' in lp else lp

        reg = part_for(mpn, parts or {})
        if not inst:
            # A passive needs no document: it is covered by the order code, the catalog
            # description and check ORD-8 (value and case). Requiring a spec for each of hundreds
            # of resistors and capacitors is noise that hides real gaps on active parts.
            # The condition MUST be nested: filtering passives in the outer `if` let an empty link
            # fall through to the "points to another part" check and produced hundreds of false
            # DOC-1 defects.
            if not PASSIVE_FP.search(c['fp'].split(':')[-1]):
                rep.gap('DOC-2', c['ref'], 'purchased part %s has no document link' % mpn)
        elif doc_in_registry(inst, reg):
            pass          # the link points to the registry document - verified by content
        elif not doc_about(inst, mpn):
            rep.defect('DOC-1', c['ref'], 'Datasheet field points to "%s" - that is not %s'
                       % (inst.rsplit('/', 1)[-1][:52], mpn))

        if lp in seen_sym:
            continue
        seen_sym.add(lp)
        # A symbol is "specific" if its own document is named after it: generic symbols
        # (Device:R, Connector_Generic:Conn_01x04) have empty docs or a general document, and
        # requiring them to match the part number makes no sense.
        specific = docs and doc_about(docs, part)
        if specific and not same_prefix(part, mpn) and ('DOC-4', c['ref']) in ACCEPTED:
            rep.notes.append('accepted: %s - %s' % (c['ref'], ACCEPTED[('DOC-4', c['ref'])]))
        elif specific and not same_prefix(part, mpn):
            rep.defect('DOC-4', c['ref'],
                       'symbol %s is a different device than %s: pinout and package cannot be '
                       'considered verified until checked against the installed part\'s document'
                       % (lp, mpn))
        elif docs and not doc_about(docs, mpn) and not specific:
            rep.gap('DOC-5', lp, 'symbol cache document "%s" is named neither after the symbol '
                    'nor after %s' % (docs.rsplit('/', 1)[-1][:44], mpn))
        elif docs and inst and docs != inst and doc_about(docs, mpn) and doc_about(inst, mpn):
            rep.gap('DOC-3', lp, 'two records of one document differ: instance "%s", '
                    'symbol cache "%s"' % (inst.rsplit('/', 1)[-1][:34], docs.rsplit('/', 1)[-1][:34]))


def num(v):
    """Number from a registry field; 'TBD' and empty give None, not an exception."""
    if isinstance(v, (int, float)):
        return float(v)
    if not v:
        return None
    m = re.match(r'\s*([\d.]+)', str(v))
    return float(m.group(1)) if m else None


def check_voltage(rep, cs, groups, parts, nets, cat):
    """Part voltage rating vs the rail it sits on."""
    by_group = collections.defaultdict(list)
    for c in cs:
        if not c['mpn']:
            key = '%s | %s' % (c['value'], c['fp'].split(':')[-1])
            if key in groups:
                by_group[key].append(c)

    for key, members in sorted(by_group.items()):
        g = groups[key]
        rail = None
        for c in members:
            r = rail_of(nets.get(c['ref'], set()))
            if r and (rail is None or RAIL_VMAX[r][0] > RAIL_VMAX[rail][0]):
                rail = r
        vpart = num(g.get('voltage_rating'))
        if vpart is None:
            rep.gap('VOLT-1', key, 'part voltage rating not recorded (%d parts%s)'
                    % (len(members), ', rail %s' % rail if rail else ''))
            continue
        if rail is None:
            continue          # not on a rail with a known maximum - nothing to judge by
        vrail = RAIL_VMAX[rail][0]
        diel = str(g.get('dielectric', '') or '')
        derate = CLASS2_DERATE if diel.upper() in ('X5R', 'X7R', 'X6S', 'X7S') else 1.0
        limit = vpart * derate
        if vrail > limit + 1e-9:
            msg = ('%s: part %g V%s, rail %s up to %g V - limit %g V (%s)'
                   % (key, vpart, ' %s (working at most half)' % diel if derate < 1 else '',
                      rail, vrail, limit, RAIL_VMAX[rail][1]))
            if ('VOLT', key) in ACCEPTED:
                rep.notes.append('accepted: %s - %s' % (key, ACCEPTED[('VOLT', key)]))
            else:
                rep.defect('VOLT-1', members[0]['ref'], msg)

    # purchased parts with a registry file: absolute maximum per pin
    for c in cs:
        if not c['mpn']:
            continue
        p = part_for(c['mpn'], parts)
        if not p:
            continue
        for v in p.get('voltages', []) or []:
            pin, amax = v.get('pin'), num(v.get('abs_max'))
            if not pin or amax is None:
                continue
            # net of this pin
            nm = pin_net(c['ref'], str(pin))
            if not nm or nm not in RAIL_VMAX:
                continue
            if RAIL_VMAX[nm][0] > amax + 1e-9:
                rep.defect('VOLT-3', c['ref'],
                           'pin %s (%s) on rail %s up to %g V with absolute maximum %g V (%s)'
                           % (pin, v.get('name', ''), nm, RAIL_VMAX[nm][0], amax,
                              v.get('source', 'source not given')))


PIN_NET = {}


def load_pin_nets(net):
    """{(ref, pin): net} - needed to compare absolute maxima on specific pins."""
    s = open(net).read()
    for m in re.finditer(r'\(net\n\t+\(code "\d+"\)\n\t+\(name "([^"]*)"\)', s):
        a, b = E.span(s, m.start())
        name = m.group(1).split('/')[-1]
        for nm in re.finditer(r'\(node\s*\(ref "([^"]*)"\)\s*\(pin "([^"]*)"\)', s[a:b]):
            PIN_NET[(nm.group(1), nm.group(2))] = name


def pin_net(ref, pin):
    return PIN_NET.get((ref, pin))


def check_thermal(rep, cs, parts):
    """Tj = Ta + Pd x RthJA vs the junction limit - under the conditions recorded in the registry."""
    done = set()
    for c in cs:
        if not c['mpn'] or c['dnp']:
            continue
        key = re.sub(r'[^a-z0-9.+-]+', '_', c['mpn'].lower()).strip('_')
        p = part_for(c['mpn'], parts)
        if not p or key in done:
            if not p:
                continue
        done.add(key)
        t = p.get('thermal') or {}
        pd, rth, tjmax = num(t.get('pd_w')), num(t.get('rth_ja')), num(t.get('tj_max'))
        if pd is None or rth is None or tjmax is None:
            rep.gap('THERM-1', c['mpn'], 'thermal estimate not recorded')
            continue
        tj = TA + pd * rth
        if tj > tjmax + 1e-9:
            rep.defect('THERM-1', c['ref'],
                       '%s: Tj = %.0f + %.3f x %.1f = %.0f with limit %.0f (%s; %s)'
                       % (c['mpn'], TA, pd, rth, tj, tjmax,
                          t.get('conditions', 'conditions not given'), t.get('source', 'no reference')))


def check_package(rep, cs, parts, pads):
    """Footprint signal pad count vs the datasheet; catalog package vs the footprint."""
    for c in cs:
        if not c['mpn']:
            continue
        p = part_for(c['mpn'], parts)
        if not p:
            continue
        want = num((p.get('package') or {}).get('signal_pads'))
        if want is None:
            rep.gap('PKG-1', c['mpn'], 'package pin count not recorded')
            continue
        # merged pins give one pad per group, plus the package thermal pads
        merged = p.get('merged_pins', []) or []
        want = want - sum(max(0, len(g.get('pins', [])) - 1) for g in merged)
        want += len(p.get('thermal_pads', []) or [])
        got = pads.get(c['ref'])
        if got is not None and int(want) != got:
            rep.defect('PKG-1', c['ref'],
                       '%s: datasheet %d pins, footprint %d (%s; %s)'
                       % (c['mpn'], int(want), got, (p.get('package') or {}).get('name', ''),
                          (p.get('package') or {}).get('source', 'no reference')))


def check_catalog_package(rep, cs, cat, groups):
    """Order-code package vs the board package - for passives, where the size reads unambiguously.

    Without this comparison a "100uF/35V" placed in an 0805 footprint passes, although 100 uF
    in 0805 exists only at 4 and 6.3 V. With an order code it is caught mechanically.
    """
    if not cat:
        return
    for key, g in sorted(groups.items()):
        code = g.get('lcsc')
        if not code or code == 'TBD' or g.get('not_ordered'):
            continue     # DNP place: no code expected in the BOM, nothing to compare
        row = cat(code)
        if row is None:
            rep.defect('ORD-2', key, 'code %s not in the catalog snapshot' % code)
            continue
        pkg = row[4] or ''
        m = IMPERIAL.search(key)
        if m and m.group(1) not in norm(pkg):
            rep.defect('ORD-7', key, 'package of code %s is "%s", footprint is %s'
                       % (code, pkg, m.group(1)))


CAP_UNIT = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'm': 1e-3}
CAP_RE = re.compile(r'([\d.]+)\s*([pnuµm])?F', re.I)


def ref_net(comp, net):
    """Whether the part sits on net `net` (by either of its pins)."""
    for pin in ('1', '2'):
        if pin_net(comp['ref'], pin) == net:
            return True
    return False


def parse_cap(value):
    """Capacitance from a component value: "10uF/35V" -> 1e-5, "330pF CC" -> 3.3e-10.

    Only the start of the value is parsed: the rest is a purpose ("100nF VCCIO") or a voltage
    rating ("10uF/35V"), neither of which is capacitance.
    """
    m = CAP_RE.search(value or '')
    if not m:
        return None
    return float(m.group(1)) * CAP_UNIT.get((m.group(2) or '').lower(), 1.0)


def check_required(rep, cs, parts, nets, geom=None):
    """Datasheet-required support parts: capacitance on a pin within a given window.

    Example of what it catches: a USB-PD controller requires 200-480 pF external capacitance on
    CC1/CC2, and the datasheet states explicitly that the internal capacitance is not enough for
    the USB-PD cReceiver minimum. With nothing but TVS diodes on those nets, neither DRC, ERC nor
    parity notices: the topology is right, and they know nothing about values.
    """
    byref = {c['ref']: c for c in cs}
    scale = {'pF': 1e-12, 'nF': 1e-9, 'uF': 1e-6}
    for c in cs:
        if not c['mpn']:
            continue
        p = part_for(c['mpn'], parts)
        if not p:
            continue
        for req in p.get('required', []) or []:
            if req.get('tie_to'):
                check_tie(rep, c, p, req)
                continue
            net = req.get('net')
            unit = scale.get(req.get('unit'), 1.0)
            near = num(req.get('max_distance_mm'))
            if near and geom:
                pads, bodies = geom
                # pin numbers with this name - from the registry pinout
                names = {str(x.get('n')) for x in (p.get('pins') or [])
                         if str(x.get('name', '')).upper() == str(req.get('pin', '')).upper()}
                spots = [pads[(c['ref'], n)] for n in names if (c['ref'], n) in pads]
                want_val = num(req.get('nom')) or num(req.get('min'))
                found = None
                for cc in cs:
                    if not cc['ref'].startswith('C') or ref_net(cc, net) is False:
                        continue
                    v = parse_cap(cc['value'])
                    if v is None or want_val is None:
                        continue
                    if abs(v - want_val * unit) > want_val * unit * 0.3:
                        continue
                    xy = bodies.get(cc['ref'])
                    if not xy or not spots:
                        continue
                    d = min(math.hypot(xy[0] - sx, xy[1] - sy) for sx, sy in spots)
                    if d <= near:
                        found = (cc['ref'], d)
                        break
                if not found and ('REQ-4', c['ref']) in ACCEPTED:
                    rep.notes.append('accepted: %s - %s' % (c['ref'], ACCEPTED[('REQ-4', c['ref'])]))
                elif not found:
                    rep.defect('REQ-4', c['ref'],
                               '%s: no %g %s within %g mm of pin %s - %s'
                               % (c['mpn'], want_val, req.get('unit'), near, req.get('pin'),
                                  req.get('source', 'no reference')))
                continue
            lo, hi = num(req.get('min')), num(req.get('max'))
            if not net or (lo is None and hi is None):
                continue
            total, members = 0.0, []
            for (ref, pin), nn in PIN_NET.items():
                if nn != net or not ref.startswith('C'):
                    continue
                v = parse_cap(byref.get(ref, {}).get('value', ''))
                if v is not None and (ref, v) not in members:
                    members.append((ref, v))
            for ref, v in sorted(set(members)):
                total += v
            shown = ', '.join('%s' % r for r, _v in sorted(set(members))) or 'nothing'
            key = ('REQ', '%s:%s' % (c['ref'], req.get('pin')))
            if lo is not None and total < lo * unit * (1 - 1e-9):
                msg = ('%s: pin %s (net %s) requires %s%g %s, fitted %g %s (%s) - %s'
                       % (c['mpn'], req.get('pin'), net, 'at least ', lo, req.get('unit'),
                          total / unit, req.get('unit'), shown, req.get('source', 'no reference')))
                if key in ACCEPTED:
                    rep.notes.append('accepted: %s' % ACCEPTED[key])
                else:
                    rep.defect('REQ-1', c['ref'], msg)
            elif hi is not None and total > hi * unit * (1 + 1e-9):
                rep.defect('REQ-2', c['ref'],
                           '%s: pin %s (net %s) allows at most %g %s, fitted %g %s (%s) - %s'
                           % (c['mpn'], req.get('pin'), net, hi, req.get('unit'),
                              total / unit, req.get('unit'), shown,
                              req.get('source', 'no reference')))


def merged_map(part):
    """{datasheet pin number: pad number} for pins merged into one pad.

    Needed because vendor WQFN footprints route parallel pins to ONE pad numbered with the first
    pin of the group: some pad numbers simply do not exist, while the merged pad is several pitches
    long. Without this map the check reports those pins as "missing from the symbol" although they
    are connected, and a conclusion like "the high-current path uses a single pin" would be false.
    """
    out = {}
    for g in part.get('merged_pins', []) or []:
        for n in g.get('pins', []):
            out[str(n)] = str(g.get('pad_number'))
    return out


def check_tie(rep, c, part, req):
    """A pin the datasheet requires to be tied to a specific net must not float.

    Typical case: RESERVED pins documented as "Tie to ground or LDO_3V3". Neither DRC nor ERC
    sees this - KiCad gives an unconnected symbol pin a stub net `unconnected-(...)`, and
    formally everything is connected.
    """
    names = {str(x) for x in re.findall(r'\d+', str(req.get('pin', '')))}
    merged = merged_map(part)
    ok = {n.strip() for n in req.get('tie_to', [])}
    hang = []
    for n in sorted(names, key=lambda x: int(x)):
        net = pin_net(c['ref'], merged.get(n, n))
        if net is None or net.startswith('unconnected-') or net not in ok:
            hang.append('%s -> %s' % (n, net or 'no net'))
    if hang:
        rep.defect('REQ-3', c['ref'],
                   '%s: pins %s must be tied to %s, but float: %s - %s'
                   % (c['mpn'], req.get('pin'), ' or '.join(sorted(ok)),
                      '; '.join(hang), req.get('source', 'no reference')))


def check_polarity(rep, cs, parts):
    """Polarity: a pin with a given role must not end up on a forbidden net.

    The most dangerous class of error: a unidirectional TVS fitted cathode to GND, anode to the
    protected rail. A forward-biased diode on a 20 V rail shorts the USB-PD input; on CC lines it
    is a 0.7 V shunt to ground, and the PD controller sees neither Rd nor BMC.

    Why a separate check. ERC does not know polarity; DRC sees a connected board; parity matches.
    If the schematic uses a BIDIRECTIONAL TVS symbol (Device:D_TVS, pins A1/A2), polarity is not
    in the schematic at all and cannot be read by eye - only the footprint defines it (in KiCad,
    pad 1 of a diode is always the cathode).
    """
    for c in cs:
        if not c['mpn']:
            continue
        p = part_for(c['mpn'], parts)
        if not p:
            continue
        rules = p.get('connection_rules') or []
        if not rules and p.get('polarity'):
            rep.gap('POL-1', c['ref'], '%s: registry has polarity but no connection rules'
                    % c['mpn'])
        for r in rules:
            pin = str(r.get('pin', ''))
            bad = {x.strip() for x in (r.get('forbidden_nets') or [])}
            net = pin_net(c['ref'], pin)
            if net is None:
                rep.gap('POL-1', c['ref'], 'pin %s not found in the netlist' % pin)
                continue
            if net in bad:
                rep.defect('POL-1', c['ref'],
                           '%s: pin %s (%s) is on net %s, which is forbidden for this role - %s'
                           % (c['mpn'], pin, r.get('role', ''), net,
                              r.get('source', 'no reference')))


def check_pinout(rep, cs, lib, parts, limit=6):
    """Symbol (number, name) vs the datasheet pinout recorded in the registry."""
    done = set()
    for c in cs:
        if not c['mpn']:
            continue
        key = re.sub(r'[^a-z0-9.+-]+', '_', c['mpn'].lower()).strip('_')
        p = part_for(c['mpn'], parts)
        if not p or key in done:
            continue
        pins = p.get('pins') or []
        if not pins:
            rep.gap('PIN-1', c['mpn'], 'pinout not recorded')
            done.add(key)
            continue
        done.add(key)
        sym = {n: nm for n, nm, _t in (lib.get(c['libpart']) or {}).get('pins', [])}
        if not sym:
            rep.gap('PIN-1', c['mpn'], 'symbol %s not found in the netlist' % c['libpart'])
            continue
        want = {str(d.get('n')): str(d.get('name', '')) for d in pins}
        # The symbol may use another vendor's pin nomenclature - then the registry stores both
        # spellings with a justification that the function is the same (e.g. a flash symbol says
        # "DO/IO1", the installed part's datasheet "SO/SIO1"). Either spelling matches.
        alias = {str(d.get('n')): str(d.get('symbol_name')) for d in pins if d.get('symbol_name')}
        for ep in p.get('thermal_pads', []) or []:
            want[str(ep.get('number'))] = str(ep.get('name', ep.get('net', '')))
        merged = merged_map(p)
        # a pin merged with its neighbour into one pad is looked up under that pad's number
        want = {merged.get(n, n): nm for n, nm in want.items()}
        # Two cases with DIFFERENT meaning that must not be mixed:
        #   * the pin name differs, or the datasheet has no such pin - a defect: the symbol
        #     describes the part wrongly, and a net may go to the wrong place;
        #   * a datasheet pin is missing from the symbol - deliberately unused peripheral. For a
        #     module or connector this is almost always the case (a modem module may have 85 pins
        #     of which 51 are used; a USB-C receptacle may leave DP/DN and SBU unused). Requiring
        #     completeness means drawing pins that go nowhere.
        # Reporting both as PIN-1 defects buried real mismatches under dozens of non-defects.
        # Unused pins are a separate PIN-2 note with a list - visible but not failing. An
        # unconnected pin becomes a defect where the datasheet requires it: such pins are listed
        # under "required" and checked by the REQ axis.
        diff, unused = [], []
        for n in sorted(set(want) | set(sym), key=lambda x: (len(x), x)):
            a, b = want.get(n), sym.get(n)
            if a is None:
                diff.append('%s: symbol "%s", not in the datasheet' % (n, b))
            elif b is None:
                unused.append('%s %s' % (n, a))
            elif norm(a) != norm(b) and norm(a) not in norm(b) and norm(b) not in norm(a) \
                    and norm(alias.get(n, '')) != norm(b):
                diff.append('%s: datasheet "%s", symbol "%s"' % (n, a, b))
        if diff:
            rep.defect('PIN-1', c['ref'], '%s: %d mismatches - %s%s'
                       % (c['mpn'], len(diff), '; '.join(diff[:limit]),
                          ' ...' if len(diff) > limit else ''))
        if unused:
            rep.note('PIN-2', c['ref'], '%s: %d datasheet pins not drawn in the symbol (unused '
                     'in the project) - %s%s'
                     % (c['mpn'], len(unused), ', '.join(unused[:limit]),
                        ' ...' if len(unused) > limit else ''))


def check_coverage(rep, cs, groups, nobuy, parts):
    """Coverage: every schematic reference is covered by something whose norm comes from a document."""
    nobuy_fp = tuple(project.get('not_purchased_footprints', ['TestPoint_', 'MountingHole_']))
    covered, missing = 0, collections.defaultdict(list)
    for c in cs:
        ref, p = c['ref'], re.match(r'[A-Za-z]+', c['ref']).group(0)
        gkey = '%s | %s' % (c['value'], c['fp'].split(':')[-1])
        grp = groups.get(gkey)
        if c['mpn']:
            if part_for(c['mpn'], parts):
                covered += 1
            elif PASSIVE_FP.search(c['fp'].split(':')[-1]) and c['lcsc']:
                # A passive with a code is covered by two checks: ORD-8 compares the code's value
                # and case with the schematic, check_stock.py checks existence and stock. A
                # separate registry file for each of hundreds of passives adds nothing.
                covered += 1
            elif grp and grp.get('lcsc') not in (None, '', 'TBD'):
                # A passive with a code is covered by its GROUP: in passives.yaml the group has a
                # code, voltage rating, rail and a sourced justification. Requiring a file per
                # passive on top of that would collapse coverage to zero on a formality.
                covered += 1
            else:
                missing['part without a registry file'].append('%s (%s)' % (ref, c['mpn']))
        elif any(x in c['fp'] for x in nobuy_fp):
            key = c['fp'].split(':')[-1]
            if key in nobuy:
                covered += 1
            else:
                missing['not purchased, but not described'].append('%s (%s)' % (ref, key))
        else:
            key = '%s | %s' % (c['value'], c['fp'].split(':')[-1])
            g = groups.get(key)
            if g and g.get('not_ordered'):
                covered += 1     # placeholder fitted by measurement - no code expected
            elif g and g.get('lcsc') not in (None, '', 'TBD'):
                covered += 1
            elif g:
                missing['passive without an order code'].append(ref)
            else:
                missing['passive outside registry groups'].append(ref)
    for what, refs in sorted(missing.items()):
        rep.gap('COV-1', what, '%d references: %s%s'
                % (len(refs), ', '.join(sorted(set(refs))[:12]),
                   ' ...' if len(refs) > 12 else ''))
    return covered, sum(len(v) for v in missing.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--net', default=paths.NETLIST)
    ap.add_argument('--board', default='', help='board for the package check (needs pcbnew)')
    ap.add_argument('--boards', type=int, default=10, help='build quantity for the stock estimate')
    ap.add_argument('--min', dest='min_stock', type=int, default=20)
    ap.add_argument('--registry', default='', help='registry directory (default: config key '
                    '"component_registry" or doc/components in the project) - lets the mutation '
                    'test run on a copy')
    ap.add_argument('--coverage', action='store_true', help='coverage map only')
    ap.add_argument('--allow-incomplete', action='store_true',
                    help='registry gaps do not affect the exit code')
    a = ap.parse_args()

    global REG
    if a.registry:
        REG = os.path.abspath(a.registry)
    idx, groups, nobuy, parts = load_registry()
    cs, lib = netlist_facts(a.net)
    load_pin_nets(a.net)
    nets = nets_by_ref(a.net)
    cat = catalog()
    rep = Report()

    covered, uncovered = check_coverage(rep, cs, groups, nobuy, parts)
    print('registry %s: part files %d, passive groups %d, non-purchased categories %d'
          % (REG, len(parts), len(groups), len(nobuy)))
    print('coverage: %d of %d references (%.0f %%)'
          % (covered, len(cs), 100.0 * covered / max(1, len(cs))))

    if not a.coverage:
        check_orderability(rep, cs, cat, parts, a.boards, a.min_stock)
        check_passive_code(rep, cs, cat)
        check_docs(rep, cs, lib, parts)
        check_voltage(rep, cs, groups, parts, nets, cat)
        check_catalog_package(rep, cs, cat, groups)
        geom = board_geometry(a.board) if a.board else None
        check_required(rep, cs, parts, nets, geom)
        check_polarity(rep, cs, parts)
        check_thermal(rep, cs, parts)
        check_pinout(rep, cs, lib, parts)
        if a.board:
            check_package(rep, cs, parts, pad_count(a.board, {c['ref'] for c in cs}))
        else:
            rep.gap('PKG-1', '-', 'no board given (--board) - packages not checked')

    if rep.notes:
        print('\naccepted deviations:')
        for n in rep.notes:
            print('  %s' % n)
    if rep.defects:
        print('\nDEFECTS (%d):' % len(rep.defects))
        for code, ref, msg in rep.defects:
            print('  [%-7s] %-6s %s' % (code, ref, msg))
    else:
        print('\nno defects found')
    if rep.gaps:
        print('\nnot checked - registry incomplete (%d entries):' % len(rep.gaps))
        for code, ref, msg in rep.gaps:
            print('  [%-7s] %-28s %s' % (code, str(ref)[:28], msg))

    bad = len(rep.defects) + (0 if a.allow_incomplete else uncovered)
    print('\nRESULT: defects %d, uncovered references %d' % (len(rep.defects), uncovered))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
