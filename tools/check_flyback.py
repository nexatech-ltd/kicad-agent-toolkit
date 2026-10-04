#!/usr/bin/env python3
"""Flyback diodes on switches: cathode must be on the supply, anode on the drain.

A defect class neither ERC nor DRC sees: the diode sits between the switch drain and the supply
rail, but reversed. The schematic looks right, yet the first time the switch turns on the rail is
shorted to ground through the diode and the transistor - both burn.

Netlist algorithm: for every switch (reference Q*) take the drain/collector net; for every
two-pin diode (D*, except TVS/ESD/LED/Schottky) with one pad on that net and the other on a supply
rail, require pad 1 (K) on the rail and pad 2 (A) on the drain.

Supply rails come from config key "supply_nets" (list of net names).

    python3 check_flyback.py work/<board>.net
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project
from sync_board import read_netlist

SUPPLY = tuple(project.get('supply_nets', ['+5V', '+3V3', '+12V', '+24V', 'VIN']))


def main():
    nl = sys.argv[1] if len(sys.argv) > 1 else paths.NETLIST
    c = read_netlist(nl)
    drains = {}
    for ref, x in c.items():
        if not ref.startswith('Q'):
            continue
        for p, net in x['pads'].items():
            f = x['funcs'].get(p, '')
            if f.split('_')[0] in ('D', 'C') or f.startswith('D_'):
                drains[net] = ref
    bad, ok = [], 0
    for ref, x in c.items():
        if not ref.startswith('D') or len(x['pads']) != 2:
            continue
        v = (x['value'] + ' ' + x.get('desc', '')).upper()
        if any(k in v for k in ('TVS', 'SMAJ', 'ESD', 'PESD', 'LED', 'SCHOTTKY', 'BAT54')):
            continue
        n1, n2 = x['pads'].get('1'), x['pads'].get('2')
        pair = None
        if n1 in drains and n2 in SUPPLY:
            pair = ('K on drain', n1, n2)
        elif n2 in drains and n1 in SUPPLY:
            ok += 1
        if pair:
            bad.append('%s (%s): K(1)=%s, A(2)=%s - switch %s will forward-bias the diode directly'
                       % (ref, x['value'], n1, n2, drains[n1]))
    if bad:
        print('FAILED  flyback diodes reversed - %d:' % len(bad))
        for b in bad:
            print('   ' + b)
        return 1
    print('PASSED  flyback diodes: cathode on supply, anode on drain - checked %d' % ok)
    return 0


if __name__ == '__main__':
    sys.exit(main())
