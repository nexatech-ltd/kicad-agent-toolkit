#!/usr/bin/env python3
"""Mark non-purchased items so the fab kit matches the component registry.

Found through a parity warning ("Footprint attributes don't match symbol: 'Exclude from
bill of materials' settings differ") on a new test point. The root cause was a kit defect:
bom.csv had rows with an EMPTY order code, and positions.csv had placements for test
points, mounting holes and a printed antenna. The assembler was told to "place" copper
features of the board, although the registry (not-purchased.yaml) already listed them as
non-purchased: the documents agreed, the kit did not.

Same class as DNP parts sent to assembly: a row without a code invites the assembler to
substitute something of their own.

What is set:
  * in the schematic `(in_bom no)`, otherwise `sch export bom` lists them;
  * on the board the `exclude_from_bom` and `exclude_from_pos_files` attributes, otherwise
    `pcb export pos` lists placements. Both are needed together, also because a mismatch
    between schematic and board on this attribute produces a parity warning.

`on_board` is NOT touched: the items physically exist on the board.

The registry file lists designators under "refs" (or "designators") keys at any depth.

    python3 tools/mark_not_purchased.py --root R --board B [--apply]
"""
import argparse
import glob
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import project


def wanted_refs(path):
    d = yaml.safe_load(open(path, encoding='utf-8'))
    acc = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ('refs', 'designators') and isinstance(v, list):
                    acc.update(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    return acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=paths.KAT_ROOT)
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--registry', default=None,
                    help='not-purchased.yaml (default: <component_registry>/not-purchased.yaml)')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    reg = a.registry or os.path.join(a.root, project.get('component_registry', os.path.join('doc', 'components')),
                                     'not-purchased.yaml')
    refs = wanted_refs(reg)
    print('non-purchased designators in the registry: %d' % len(refs))

    # --- schematic
    sch_done, sch_files = [], []
    for f in sorted(glob.glob(os.path.join(a.root, '*.kicad_sch'))):
        s = open(f, encoding='utf-8').read()
        orig = s
        for ref in sorted(refs):
            for m in re.finditer(r'\(property "Reference" "%s"' % re.escape(ref), s):
                j = s.rfind('\t(symbol', 0, m.start())
                if j < 0:
                    continue
                blk = s[j:m.start()]
                bm = re.search(r'\(in_bom (\w+)\)', blk)
                if bm and bm.group(1) == 'yes':
                    at = j + bm.start(1)
                    s = s[:at] + 'no' + s[at + 3:]
                    sch_done.append(ref)
                break
        if s != orig:
            sch_files.append((f, s))
    print('schematic: in_bom cleared on %d symbols: %s'
          % (len(sch_done), ', '.join(sorted(set(sch_done)))))

    # --- board
    import pcbnew
    b = pcbnew.LoadBoard(a.board)
    brd_done = []
    for fp in b.GetFootprints():
        r = fp.GetReference()
        if r not in refs:
            continue
        if not (fp.IsExcludedFromBOM() and fp.IsExcludedFromPosFiles()):
            fp.SetExcludedFromBOM(True)
            fp.SetExcludedFromPosFiles(True)
            brd_done.append(r)
    print('board: footprints marked: %d' % len(brd_done))

    if not a.apply:
        print('this is a plan; add --apply to write')
        return 0
    for f, s in sch_files:
        open(f + '.bakNotPurch', 'w', encoding='utf-8').write(open(f, encoding='utf-8').read())
        open(f, 'w', encoding='utf-8').write(s)
        print('written %s' % os.path.basename(f))
    if brd_done:
        pcbnew.SaveBoard(a.board, b)
        print('board saved')
    return 0


if __name__ == '__main__':
    sys.exit(main())
