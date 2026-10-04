#!/usr/bin/env python3
"""Power-up sequencing: does a rail's enable depend on a rail that does not exist yet.

A defect class no connectivity check sees, and one that bricks the board: rail A is enabled by a
signal from rail B, while B is derived from A. The board never comes up, and the schematic looks
right - both wires are there.

The check builds the power tree from the netlist: for every source it finds the input, output and
enable pins, then looks at where the enable comes from. Three cases are acceptable: the enable
comes from a rail HIGHER in the chain (or the source's own input - "always on"), from a GPIO of a
controller chip, or via a resistor pull-up to a higher rail. Anything else is a question.

Config keys (pin names are netlist pinfunctions; see defaults below):
  power_in_pins, power_out_pins, enable_pins  - name prefixes that identify the pin role
  sequence_controllers                         - refs whose GPIOs are legitimate enable sources

    python3 check_power_sequence.py work/<board>.net
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
from sexp import find, parse, val

# Pin names by which a role is recognised. A pin name in the netlist is its pinfunction.
IN_NAMES = tuple(project.get('power_in_pins',
                             ['VIN', 'PVIN', 'AVIN', 'VBUS', 'VBAT', 'VDD', 'VCC', 'VCC_IN']))
OUT_NAMES = tuple(project.get('power_out_pins',
                              ['VOUT', 'OUT', 'SW', 'LX', 'SYS', 'VSYS', 'LDO']))
EN_NAMES = tuple(project.get('enable_pins',
                             ['EN', 'ENABLE', 'ENA', '~{EN}', 'CE', '~{CE}', '~{SHDN}', 'SHDN']))
CONTROLLERS = tuple(project.get('sequence_controllers', []))  # whose GPIO is a legitimate enable source


def base(n):
    return n.split('/')[-1]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else paths.NETLIST
    t = parse(open(path).read())[0]
    nets = {}
    for nt in find(find(t, 'nets')[0], 'net'):
        nets[val(nt, 'name')] = [(val(nd, 'ref'), val(nd, 'pin'),
                                  val(nd, 'pinfunction', ''), val(nd, 'pintype', ''))
                                 for nd in find(nt, 'node')]
    pin_net = {}
    for name, nodes in nets.items():
        for ref, pin, fn, pt in nodes:
            pin_net[(ref, pin)] = base(name)

    # pin roles per package
    role = {}
    for name, nodes in nets.items():
        for ref, pin, fn, pt in nodes:
            f = (fn or '').split('_')[0].upper()
            full = (fn or '').upper()
            r = None
            # An enable pin is an INPUT. Some controllers have a similarly named pin (e.g.
            # ~XX_EN) that is an open-drain output marked no_connect: taking it for an enable
            # produced a false "source not recognised".
            is_input = pt in ('input', 'power_in', 'bidirectional')
            if 'no_connect' in (pt or ''):
                continue
            if is_input and any(full.startswith(x) or f == x for x in EN_NAMES):
                r = 'en'
            elif any(full.startswith(x) for x in OUT_NAMES) or pt == 'power_out':
                r = 'out'
            elif any(full.startswith(x) for x in IN_NAMES) or pt == 'power_in':
                r = 'in'
            if r:
                role.setdefault(ref, {}).setdefault(r, []).append((pin, fn, base(name)))

    srcs = {ref: v for ref, v in role.items()
            if 'out' in v and 'in' in v and ref.startswith('U')}
    print('power sources found: %d' % len(srcs))
    print()
    issues = []
    for ref in sorted(srcs):
        v = srcs[ref]
        ins = sorted({n for _, _, n in v['in']})
        outs = sorted({n for _, _, n in v['out']})
        ens = sorted({(f, n) for _, f, n in v.get('en', [])})
        print('  %-5s in %-26s out %s' % (ref, ','.join(ins)[:26], ','.join(outs)[:40]))
        if not ens:
            print('        enable: no pin (always on)')
            continue
        for fn, en_net in ens:
            how = None
            if en_net in ins:
                how = 'on its own input - always on once the input is present'
            else:
                drivers = [r for r, p, f, pt in nets.get(
                    next((k for k in nets if base(k) == en_net), ''), [])
                    if r != ref]
                ctrl = [r for r in drivers if r in CONTROLLERS]
                res = [r for r in drivers if r.startswith('R')]
                if ctrl:
                    how = 'GPIO %s' % ','.join(sorted(set(ctrl)))
                elif res:
                    ups = set()
                    for r in res:
                        for (rr, pp) in pin_net:
                            if rr == r and pin_net[(rr, pp)] != en_net:
                                ups.add(pin_net[(rr, pp)])
                    how = 'via resistor %s to %s' % (','.join(sorted(set(res))),
                                                      ','.join(sorted(ups)) or '?')
                    if not (ups & set(ins)) and 'GND' not in ups:
                        issues.append('%s.%s is enabled from %s via %s - check that this '
                                      'rail comes up earlier' % (ref, fn, en_net, ','.join(sorted(res))))
                else:
                    how = 'source not recognised: %s' % (','.join(sorted(set(drivers))) or 'none')
                    issues.append('%s.%s (%s): enable source not recognised' % (ref, fn, en_net))
            print('        enable %-14s <- %s' % (fn, how))
    print()
    if issues:
        print('QUESTIONS (%d):' % len(issues))
        for i in issues:
            print('   ', i)
    else:
        print('power-up sequence is closed: every source is enabled'
              ' from a higher rail or a GPIO')
    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main())
