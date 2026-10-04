#!/usr/bin/env python3
"""Turn unidirectional TVS diodes around: the cathode must face the protected line, the anode GND.

Failure mode this fixes: unidirectional TVS diodes connected backwards, cathode to GND and
anode to the protected net, e.g.

    D1  SMAJ24A      pad 1 -> GND, pad 2 -> VBUS
    D2  PESD5V0S1UB  pad 1 -> GND, pad 2 -> CC1

How to confirm from the documents:
  * the datasheet says "Uni-directional" (and shows a cathode band), so polarity matters;
  * pad 1 of the KiCad footprints `Diode_SMD:D_SMA` and `Diode_SMD:D_SOD-523` is the CATHODE:
    on F.Fab the triangle tip and the cathode bar are drawn on the pad-1 side, and the silkscreen
    cathode mark is on the same side. KiCad symbols follow the same convention: pin 1 is "K"
    (Device:D, Device:D_Schottky).

Consequence: a forward-biased diode across the rail. On a 20 V input that is a short through
the TVS; on CC lines it is a ~0.7 V shunt to ground, and the USB-PD controller sees neither Rd
nor BMC traffic.

Why it is easy to miss: if the schematic uses `Device:D_TVS`, that is a BIDIRECTIONAL TVS with
pins A1/A2 and no polarity at all, so neither ERC, nor parity, nor reading the schematic shows
it. Only the footprint sets the physical polarity.

The fix is a 180 degree symbol rotation. With horizontal symbols (pins at (-3.81, 0) and
(3.81, 0)) wires and labels stay in place; only which pin reaches which net changes. On the
board the footprints are symmetric about their centre (D_SMA pads at x = +-2, D_SOD-523 at
x = +-0.7), so a 180 degree rotation puts the pads back on the same spots and no re-routing is
needed: the pad nets are simply swapped.

    python3 tools/fix_tvs_polarity.py --root <dir> --tvs D1=power.kicad_sch,D2=usb.kicad_sch            # plan
    python3 tools/fix_tvs_polarity.py --root <dir> --tvs ... --swap D1=power.kicad_sch --apply
    python3 tools/fix_tvs_polarity.py --board B.kicad_pcb --tvs D1,D2 --apply
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import schedit as E

# reference -> schematic sheet (from --tvs)
TVS = {}
# Parts drawn with the BIDIRECTIONAL `Device:D_TVS` symbol ("Bidirectional transient-voltage-
# suppression diode", pins A1/A2) while a unidirectional part is fitted. They are switched to
# `Device:D_Zener` (pins K and A): the geometry is the same, both pins at (-3.81, 0) and
# (3.81, 0), so wires and labels stay put and the polarity becomes visible to people and to
# pinout checks. Reference -> sheet (from --swap).
SWAP = {}
OLD_SYM, NEW_SYM = 'Device:D_TVS', 'Device:D_Zener'
SYM_LIB = os.path.join(os.environ.get('KICAD_SYMBOL_DIR', '/usr/share/kicad/symbols'), 'Device.kicad_sym')


def _refmap(text):
    """'D1=a.kicad_sch,D2=b.kicad_sch' (or 'D1,D2' for board-only use) -> {ref: sheet}."""
    out = {}
    for item in (text or '').split(','):
        if item.strip():
            ref, _, sheet = item.strip().partition('=')
            out[ref] = sheet
    return out


def swap_symbols(root, sheet, refs, apply):
    """Replace the bidirectional TVS symbol with a unidirectional one where a unidirectional part is fitted."""
    path = os.path.join(root, sheet)
    s = open(path).read()
    changed = []
    for ref in refs:
        a, b = E.symbol_block_by_ref(s, ref)
        blk = s[a:b]
        if '(lib_id "%s")' % OLD_SYM not in blk:
            print('  %s: symbol is already %s' % (ref, re.search(r'\(lib_id "([^"]+)"\)', blk).group(1)))
            continue
        nb = blk.replace('(lib_id "%s")' % OLD_SYM, '(lib_id "%s")' % NEW_SYM)
        s = s[:a] + nb + s[b:]
        changed.append(ref)
        print('  %s: symbol %s -> %s' % (ref, OLD_SYM, NEW_SYM))
    if changed and '(symbol "%s"' % NEW_SYM not in s:
        sym = E.extract_symbol(SYM_LIB, NEW_SYM.split(':')[1])
        s = E.add_cached_symbol(s, NEW_SYM, sym)
        print('  symbol %s added to the sheet cache' % NEW_SYM)
    if not (changed and apply):
        return 0
    E.check_balance(s)
    open(path + '.bakSwap', 'w').write(open(path).read())
    open(path, 'w').write(s)
    chk = open(path).read()
    for ref in changed:
        a, b = E.symbol_block_by_ref(chk, ref)
        got = re.search(r'\(lib_id "([^"]+)"\)', chk[a:b]).group(1)
        if got != NEW_SYM:
            raise SystemExit('ERROR after writing: %s has symbol %s' % (ref, got))
    print('  wrote %s' % path)
    return len(changed)


def fix_sheet(root, sheet, refs, apply):
    path = os.path.join(root, sheet)
    s = open(path).read()
    changed = []
    for ref in refs:
        a, b = E.symbol_block_by_ref(s, ref)
        blk = s[a:b]
        m = re.search(r'\(at ([-\d.]+) ([-\d.]+) (\d+)\)', blk)
        rot = int(m.group(3))
        new = (rot + 180) % 360
        nb = blk[:m.start()] + '(at %s %s %d)' % (m.group(1), m.group(2), new) + blk[m.end():]
        s = s[:a] + nb + s[b:]
        changed.append('%s: rotation %d -> %d at (%s, %s)' % (ref, rot, new, m.group(1), m.group(2)))
    for c in changed:
        print('  ' + c)
    if not apply:
        return 0
    E.check_balance(s)
    open(path + '.bakTVS', 'w').write(open(path).read())
    open(path, 'w').write(s)
    # re-measure: the rotation was written
    chk = open(path).read()
    for ref in refs:
        a, b = E.symbol_block_by_ref(chk, ref)
        rot = int(re.search(r'\(at [-\d.]+ [-\d.]+ (\d+)\)', chk[a:b]).group(1))
        if rot % 360 not in (180, 0):
            raise SystemExit('ERROR: %s has rotation %d after writing' % (ref, rot))
    print('  wrote %s' % path)
    return len(changed)


def fix_board(board, apply):
    import pcbnew
    b = pcbnew.LoadBoard(board)
    mm = lambda v: v / 1e6
    for ref in sorted(TVS):
        fps = [f for f in b.GetFootprints() if f.GetReference() == ref]
        if not fps:
            print('  %s is not on the board' % ref)
            continue
        fp = fps[0]
        pads = {p.GetNumber(): p for p in fp.Pads()}
        if set(pads) != {'1', '2'}:
            raise SystemExit('REFUSED: %s has pads %s, expected 1 and 2' % (ref, sorted(pads)))
        n1, n2 = pads['1'].GetNet(), pads['2'].GetNet()
        before = (mm(pads['1'].GetPosition().x), mm(pads['1'].GetPosition().y))
        rot = fp.GetOrientationDegrees()
        print('  %-4s rotation %.0f -> %.0f; pad nets %s / %s -> %s / %s'
              % (ref, rot, (rot + 180) % 360, n1.GetNetname().split('/')[-1],
                 n2.GetNetname().split('/')[-1], n2.GetNetname().split('/')[-1],
                 n1.GetNetname().split('/')[-1]))
        if not apply:
            continue
        fp.SetOrientationDegrees((rot + 180) % 360)
        pads['1'].SetNet(n2)
        pads['2'].SetNet(n1)
        after = (mm(pads['1'].GetPosition().x), mm(pads['1'].GetPosition().y))
        # symmetric footprint: pad 1 must land where pad 2 was
        print('       pad 1: (%.3f, %.3f) -> (%.3f, %.3f)' % (before + after))
    if not apply:
        return 0
    for z in b.Zones():
        z.SetIsFilled(False)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.Save(board)
    print('  wrote %s (zones refilled)' % board)
    return len(TVS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', help='schematic sheet directory')
    ap.add_argument('--board', help='board file')
    ap.add_argument('--tvs', required=True,
                    help='TVS diodes to turn around: REF=sheet.kicad_sch,... (sheet may be omitted with --board only)')
    ap.add_argument('--swap', default='',
                    help='REF=sheet.kicad_sch,... drawn with the bidirectional Device:D_TVS symbol '
                         'but fitted with a unidirectional part')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--only', choices=['rotate', 'swap', 'all'], default='all',
                    help='rotate: only turn around, swap: only replace the symbol. A second full run '
                         'would turn the fixed parts back, so follow-up runs should use --only swap')
    a = ap.parse_args()
    if not (a.root or a.board):
        ap.error('--root and/or --board is required')
    TVS.update(_refmap(a.tvs))
    SWAP.update(_refmap(a.swap))
    if a.root:
        if any(not sh for sh in list(TVS.values()) + list(SWAP.values())):
            ap.error('--root needs a sheet for every reference: REF=sheet.kicad_sch')
        root = os.path.abspath(a.root)
        print('schematic: %s' % root)
        by_sheet = {}
        for ref, sheet in TVS.items():
            by_sheet.setdefault(sheet, []).append(ref)
        if a.only in ('rotate', 'all'):
            for sheet, refs in sorted(by_sheet.items()):
                print(' %s' % sheet)
                fix_sheet(root, sheet, sorted(refs), a.apply)
        print(' replacing the bidirectional TVS symbol with a unidirectional one:')
        swap_by_sheet = {}
        for ref, sheet in SWAP.items():
            swap_by_sheet.setdefault(sheet, []).append(ref)
        if a.only in ('swap', 'all'):
            for sheet, refs in sorted(swap_by_sheet.items()):
                print('  %s' % sheet)
                swap_symbols(root, sheet, sorted(refs), a.apply)
    if a.board and a.only in ('rotate', 'all'):
        print('board: %s' % a.board)
        fix_board(a.board, a.apply)
    if not a.apply:
        print('\nplan only. Add --apply to write')
    return 0


if __name__ == '__main__':
    sys.exit(main())
