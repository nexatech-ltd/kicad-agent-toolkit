#!/usr/bin/env python3
"""Rename a net on the board, both on pads and on copper.

Why separate from sync_board.py. That tool takes the netlist as the truth and can change a
pad's net, but a rename cannot be inferred from the netlist: it sees the new name and does not
know that the old one corresponds to it. The copper stays on the old name and becomes a
separate net, so the board breaks silently.

Typical case: `--schematic-parity` findings on an auto-named net such as a crystal net
`Net-(C1-Pad1)`: the members match exactly in schematic and board, but KiCad does not pair
auto-named nets. The fix is to name the net in the schematic, and then rename the copper here.

    python3 rename_net.py "Net-(C1-Pad1)" "/MCU/HSE_XTAL" [--board B.kicad_pcb]
    python3 rename_net.py "Net-(C1-Pad1)" "/MCU/HSE_XTAL" [--board B.kicad_pcb] --apply
"""
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths

BRD = paths.BOARD


def main():
    # --board selects the board explicitly; default is the project board (paths.BOARD).
    argv = sys.argv[1:]
    brd = BRD
    if '--board' in argv:
        i = argv.index('--board'); brd = argv[i + 1]; argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith('--')]
    if len(args) != 2:
        print(__doc__)
        return 1
    old, new = args
    apply = '--apply' in sys.argv
    lck = brd + '.lck'
    if apply and os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')
    txt = open(brd).read()
    pat = '(net "%s")' % old
    n = txt.count(pat)
    print('net %r -> %r' % (old, new))
    print('  (net "...") occurrences on the board: %d' % n)
    if not n:
        print('  REFUSED: no such net on the board; check the full name, including the sheet path')
        return 1
    if txt.count('(net "%s")' % new):
        print('  REFUSED: a net with the new name already exists; merging is not supported')
        return 1
    # where exactly: by block type, to show that the rename is complete
    kinds = {}
    for tag in ('pad', 'segment', 'arc', 'via', 'zone'):
        kinds[tag] = len(re.findall(r'\(%s\b[^\n]*(?:\n(?!\t\t?\()[^\n]*)*' % tag, ''))
    for tag in ('pad', 'segment', 'arc', 'via'):
        # rough: count name occurrences within blocks of this tag
        cnt = 0
        for m in re.finditer(r'\(%s\b' % tag, txt):
            seg = txt[m.start():m.start() + 1200]
            if pat in seg.split('\n(')[0] or pat in seg[:1200]:
                cnt += 1
        kinds[tag] = cnt
    print('  of them, approximately: pads %d, segments %d, arcs %d, vias %d'
          % (kinds['pad'], kinds['segment'], kinds['arc'], kinds['via']))
    if not apply:
        print('  plan only, add --apply to write')
        return 0
    shutil.copy2(brd, brd + '.bakRename')
    open(brd, 'w').write(txt.replace(pat, '(net "%s")' % new))
    left = open(brd).read().count(pat)
    print('  renamed, occurrences of the old name left: %d (must be 0)' % left)
    print('  backup .bakRename')
    return 0 if left == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
