#!/usr/bin/env python3
"""Board pads without a net: the one thing sync_board/sync_fields do not cover.

Updating the board from the schematic (F8 in KiCad) does two different things. The mutating half
is covered by `sync_board.py` (footprints, pin nets, values, DNP attribute) and `sync_fields.py`
(procurement fields): both run from the command line and can confirm zero changes. But the F8
REPORT contains a class of findings that neither ERC, DRC nor parity produce: "footprint pad has
no net". Dozens of them can exist on a board while all three checks are clean.

What an empty pad net means. A schematic pin always has a net: a real one or the automatic
`unconnected-(REF-PIN-PadN)`. So an empty net on the board is a footprint pad with NO matching
symbol pin. That is legitimate (mounting holes, fiducials, thermal pads, merged pads of fine-pitch
packages) or not (the symbol does not describe a pin that is physically soldered, so it is
soldered to nothing).

    python3 tools/check_pads_no_net.py --board B.kicad_pcb --net work/B.net
"""
import argparse
import collections
import os
import re
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project

MM = 1e6

# Reviewed pads without a net, justified by the manufacturer's documentation. Config key
# `no_net_pads`: {"REF": {"pads": ["5", "6", ...], "reason": "..."}}. The check requires
# the set to match EXACTLY: a newly unconnected pad lands in "questionable" instead of dissolving
# into the accepted ones.
ACCEPTED_PADS = {ref: (frozenset(str(p) for p in v.get('pads', [])), v.get('reason', ''))
                 for ref, v in project.get('no_net_pads', {}).items()}

# Legitimate reasons for an empty net. Key: prefix of the footprint library name.
ALLOWED_FP = (
    ('MountingHole', 'mounting hole: mechanical, the symbol has no pin by design'),
    ('Fiducial', 'fiducial for the pick-and-place machine: no net'),
    ('TestPoint_Bridge', 'bridge test point: both halves are described by one pin'),
)


def netlist_nodes(path):
    """{(ref, pad): net} from the netlist, including the automatic unconnected- nets."""
    t = open(path, encoding='utf-8').read()
    out = {}
    for blk in re.split(r'\(net\s+\(code "\d+"\)\s+', t)[1:]:
        m = re.search(r'\(name "([^"]+)"\)', blk)
        if not m:
            continue
        net = m.group(1)
        for ref, pad in re.findall(r'\(ref "([^"]+)"\)\s+\(pin "([^"]+)"\)', blk):
            out[(ref, pad)] = net
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--net', required=True)
    a = ap.parse_args()

    nodes = netlist_nodes(a.net)
    b = pcbnew.LoadBoard(a.board)
    cu = [b.GetLayerID(n) for n in ('F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu')]

    ok = collections.Counter()
    suspect = []
    nocopper = collections.Counter()
    seen_accepted = collections.defaultdict(set)
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        lib = fp.GetFPIDAsString().split(':')[-1]
        for p in fp.Pads():
            if p.GetNetname():
                continue
            num = p.GetNumber()
            on_cu = any(p.IsOnLayer(l) for l in cu)
            why = next((w for pre, w in ALLOWED_FP if lib.startswith(pre)), None)
            if why:
                ok[why] += 1
            elif not on_cu:
                nocopper['%s (%s)' % (ref, lib)] += 1
            elif (ref, num) in nodes:
                # the pin HAS a net in the schematic but none on the board: out of sync
                suspect.append((ref, num, lib, 'net "%s" in the schematic, empty on the board; '
                                'run sync_board.py --apply' % nodes[(ref, num)]))
            elif not num:
                ok['pad without a number: mechanical part of the footprint'] += 1
            elif ref in ACCEPTED_PADS and num in ACCEPTED_PADS[ref][0]:
                seen_accepted[ref].add(num)
            else:
                suspect.append((ref, num, lib,
                                'the symbol has no pin %s; the pad is soldered to nothing' % num))

    print('pads without a net: reviewed %d, legitimate %d, no copper %d, questionable %d'
          % (sum(len(v) for v in seen_accepted.values()), sum(ok.values()),
             sum(nocopper.values()), len(suspect)))
    for ref in sorted(seen_accepted):
        want, why = ACCEPTED_PADS[ref]
        got = seen_accepted[ref]
        print('   reviewed %-6s %2d pads: %s' % (ref, len(got), why.split(';')[0]))
        if got != want:
            # the set changed: this is NOT what was reviewed, so do not stay silent
            for num in sorted(got - want):
                suspect.append((ref, num, '-', 'pad is not in the reviewed set'))
            for num in sorted(want - got):
                print('        WARNING: pad %s is no longer free; the entry is stale' % num)
    for why, k in ok.most_common():
        print('   legit    %3d  %s' % (k, why))
    if nocopper:
        print('   no copper (paste/mechanical, harmless for assembly):')
        for what, k in nocopper.most_common(8):
            print('        %-34s %d' % (what, k))
    if suspect:
        print('\nQUESTIONABLE (%d):' % len(suspect))
        for ref, num, lib, why in sorted(suspect):
            print('   %-7s pad %-4s %-32s %s' % (ref, num, lib, why))
        return 1
    print('\nRESULT: no pads without a net that need a decision')
    return 0


if __name__ == '__main__':
    sys.exit(main())
