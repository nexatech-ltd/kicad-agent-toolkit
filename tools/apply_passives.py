#!/usr/bin/env python3
"""Write the passive order codes recorded in passives.yaml into the schematic and the board.

Batch by necessity: with ~200 placements, `set_field.py` re-reads all sheets and the board
on every call (tens of MB), so ~200 runs would take hours and rewrite the board ~200
times. Here each file is read and written once.

The norm is `<component_registry>/passives.yaml`: each group has a code, voltage rating,
dielectric, rail and a `rationale` with its source (see `tools/pick_passives.py`). Fields
are written to BOTH:
  * the schematic symbol, otherwise the code is missing from the BOM;
  * the board footprint, otherwise `--schematic-parity` reports "Missing symbol field ...
    in footprint" (when "Update PCB from schematic" (F8) is not available, see
    `tools/sync_board.py`).

    python3 tools/apply_passives.py --root . --board board.kicad_pcb
    python3 tools/apply_passives.py --root . --board board.kicad_pcb --apply
"""
import argparse
import collections
import os
import re
import sys
import uuid as _uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import project
import schedit as E

PROJ = paths.KAT_ROOT
REG = os.path.join(PROJ, project.get('component_registry', os.path.join('doc', 'components')))
FIELDS = ('LCSC', 'MPN')


def wanted():
    """{ref: {'LCSC': code, 'MPN': part number}} from the passive group registry."""
    import yaml
    doc = yaml.safe_load(open(os.path.join(REG, 'passives.yaml'))) or {}
    out = {}
    for key, g in (doc.get('groups') or {}).items():
        code, mpn = g.get('lcsc'), g.get('mpn')
        if code in (None, '', 'TBD'):
            continue
        for ref in g.get('refs', []):
            out[ref] = {'LCSC': code, 'MPN': mpn or '', 'group': key}
    return out


# Format of a user field of a symbol: EXACTLY as KiCad writes it (copied from an existing LCSC
# field). Differences from a "similar" format matter: `(hide yes)` lives INSIDE `(effects)`,
# and a symbol field has no `(uuid ...)` at all. With hide outside and a uuid added, KiCad
# silently DROPS the symbol: the netlist kept a handful of components out of hundreds, and ERC
# reported hundreds of "label not connected" because the dropped symbols have no pins.
SCH_PROP = ('\n\t\t(property "%s" "%s"\n\t\t\t(at %s %s %s)\n\t\t\t(show_name no)\n'
            '\t\t\t(do_not_autoplace no)\n\t\t\t(effects\n\t\t\t\t(font\n'
            '\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(hide yes)\n'
            '\t\t\t)\n\t\t)')


def set_prop_sch(blk, name, val):
    """Field value in a schematic symbol block: replace it, or insert it next to the others."""
    pm = re.search(r'(\(property "%s" ")([^"]*)(")' % re.escape(name), blk)
    if pm:
        if pm.group(2) == val:
            return blk, 'unchanged'
        return blk[:pm.start(2)] + val + blk[pm.end(2):], 'changed'
    m = re.search(r'\(property "Reference" "[^"]*"\s*\n\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', blk)
    px, py, rot = (m.group(1), m.group(2), m.group(3)) if m else ('0', '0', '0')
    # insert after the LAST property but strictly BEFORE (instances): a field placed after the
    # hierarchy block also breaks parsing
    ip = blk.find('\n\t\t(instances')
    zone = blk[:ip] if ip > 0 else blk
    last = None
    for pm2 in re.finditer(r'\n\t\t\(property ', zone):
        last = pm2
    if last is None:
        raise SystemExit('symbol block has no property fields')
    _a, end = E.span(zone, last.start() + 3)
    return blk[:end] + (SCH_PROP % (name, val, px, py, rot)) + blk[end:], 'added'


def set_prop_fp(blk, name, val):
    """Same for a footprint block on the board: hidden field on F.Fab, as KiCad itself writes it."""
    pm = re.search(r'(\(property "%s" ")([^"]*)(")' % re.escape(name), blk)
    if pm:
        if pm.group(2) == val:
            return blk, 'unchanged'
        return blk[:pm.start(2)] + val + blk[pm.end(2):], 'changed'
    anchor = blk.find('\n\t\t(property ki_fp_filters')
    if anchor < 0:
        last = None
        for pm2 in re.finditer(r'\n\t\t\(property ', blk):
            last = pm2
        if last is None:
            raise SystemExit('footprint has no property fields')
        _a, anchor = E.span(blk, last.start() + 3)
    text = ('\n\t\t(property "%s" "%s"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n'
            '\t\t\t(hide yes)\n\t\t\t(uuid "%s")\n\t\t\t(effects\n\t\t\t\t(font\n'
            '\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n'
            '\t\t\t)\n\t\t)') % (name, val, _uuid.uuid4())
    return blk[:anchor] + text + blk[anchor:], 'added'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=paths.KAT_ROOT)
    ap.add_argument('--board', default='')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--only', help='edit only this sheet (for troubleshooting)')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    want = wanted()
    print('registry has codes for %d designators' % len(want))

    stat = collections.Counter()
    # --- schematic: one pass per sheet
    for fn in sorted(f for f in os.listdir(root) if f.endswith('.kicad_sch')):
        if a.only and fn != a.only:
            continue
        path = os.path.join(root, fn)
        s = open(path).read()
        refs = [r for r in want if re.search(r'\(property "Reference" "%s"' % re.escape(r), s)]
        if not refs:
            continue
        out = s
        # edit from the end of the file so block offsets do not shift
        spans = sorted(((E.symbol_block_by_ref(out, r), r) for r in refs), reverse=True)
        for (sa, sb), ref in spans:
            blk = out[sa:sb]
            for name in FIELDS:
                blk, what = set_prop_sch(blk, name, want[ref][name])
                stat['schematic:' + what] += 1
            out = out[:sa] + blk + out[sb:]
        try:
            E.check_balance(out)
        except AssertionError as e:
            raise SystemExit('REFUSED on %s: %s' % (fn, e))
        print('  %-24s designators %d' % (fn, len(refs)))
        if a.apply and out != s:
            open(path + '.bakPass', 'w').write(s)
            open(path, 'w').write(out)
            chk = open(path).read()
            for ref in refs:
                ca, cb = E.symbol_block_by_ref(chk, ref)
                got = re.search(r'\(property "LCSC" "([^"]*)"', chk[ca:cb])
                if not got or got.group(1) != want[ref]['LCSC']:
                    raise SystemExit('ERROR after writing %s: %s has LCSC = %r'
                                     % (fn, ref, got and got.group(1)))

    # --- board: one pass
    if a.board:
        bp = os.path.abspath(a.board)
        bs = open(bp).read()
        out = bs
        found = []
        for ref in want:
            fm = re.search(r'\(property "Reference" "%s"' % re.escape(ref), out)
            if fm:
                found.append(ref)
        spans = []
        for ref in found:
            fm = re.search(r'\(property "Reference" "%s"' % re.escape(ref), out)
            fa = out.rfind('(footprint ', 0, fm.start())
            _x, fb = E.span(out, fa)
            spans.append(((fa, fb), ref))
        for (fa, fb), ref in sorted(spans, reverse=True):
            blk = out[fa:fb]
            for name in FIELDS:
                blk, what = set_prop_fp(blk, name, want[ref][name])
                stat['board:' + what] += 1
            out = out[:fa] + blk + out[fb:]
        try:
            E.check_balance(out)
        except AssertionError as e:
            raise SystemExit('REFUSED on the board: %s' % e)
        print('  board: footprints %d' % len(found))
        if a.apply and out != bs:
            open(bp + '.bakPass', 'w').write(bs)
            open(bp, 'w').write(out)
            chk = open(bp).read()
            bad = [r for r in found
                   if not re.search(r'\(property "LCSC" "%s"' % re.escape(want[r]['LCSC']),
                                    chk[chk.rfind('(footprint ', 0,
                                        re.search(r'\(property "Reference" "%s"' % re.escape(r),
                                                  chk).start()):][:20000])]
            if bad:
                raise SystemExit('ERROR after writing the board: still without a code: %s' % bad[:5])

    print('\nresult: %s' % ', '.join('%s %d' % (k, v) for k, v in sorted(stat.items())))
    if not a.apply:
        print('plan only. Add --apply to write')
    return 0


if __name__ == '__main__':
    sys.exit(main())
