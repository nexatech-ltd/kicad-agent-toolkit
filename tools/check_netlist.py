#!/usr/bin/env python3
"""Static netlist check - what neither ERC nor DRC sees.

KiCad ERC checks pin-type compatibility pairwise and says nothing about what is visible only
from the whole composition of a net: an open-collector output without a pull-up, a strap without
a resistor, an I2C bus with three pairs of pull-ups, a net without a source. None of these shows
before power-up, and half of them not even after - they give "sometimes does not start".

Checks run on the netlist (kicad-cli sch export netlist), because the pin type (pintype) exists
only there; the board keeps just pads.

Config keys: "supply_nets" (rails in addition to nets driven by power_out pins),
"netlist_skip_nets" (nets not analysed, default ["GND"]).

    kicad-cli sch export netlist --format kicadsexpr -o net.net board.kicad_sch
    python3 check_netlist.py net.net
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project
from sexp import parse, find, val

PULLUP_KINDS = ('open_collector', 'open_emitter')
DRIVERS = ('output', 'power_out', 'tri_state', 'bidirectional', 'open_collector',
           'open_emitter')
# Plane nets and anything that needs no analysis
SKIP = set(project.get('netlist_skip_nets', ['GND']))


def load(path):
    t = parse(open(path).read())[0]
    comps = {}
    dnp = set()
    for c in find(find(t, 'components')[0], 'comp'):
        ref = val(c, 'ref')
        comps[ref] = val(c, 'value', '')
        # the netlist dnp property has no value and is present only on unfitted parts
        if any(val(p, 'name') == 'dnp' for p in find(c, 'property')):
            dnp.add(ref)
    nets = {}
    for nt in find(find(t, 'nets')[0], 'net'):
        name = val(nt, 'name')
        nodes = []
        for nd in find(nt, 'node'):
            nodes.append((val(nd, 'ref'), val(nd, 'pin'), val(nd, 'pinfunction', ''),
                          val(nd, 'pintype', '')))
        nets[name] = nodes
    return comps, nets, dnp


def ohms(value):
    """'4k7' -> 4700.0; '0R' -> 0.0; otherwise None."""
    if not value:
        return None
    v = value.strip().split()[0]
    m = re.fullmatch(r'(\d+(?:\.\d+)?)([RrKkMm]?)(\d*)', v)
    if not m:
        return None
    a, suf, b = m.groups()
    mult = {'': 1, 'r': 1, 'R': 1, 'k': 1e3, 'K': 1e3, 'm': 1e6, 'M': 1e6}[suf]
    num = float(a + ('.' + b if b else ''))
    return num * mult


def base(name):
    return name.split('/')[-1]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else paths.NETLIST
    comps, nets, dnp = load(path)
    print('netlist: %d nets, %d components (%d DNP), %d nodes'
          % (len(nets), len(comps), len(dnp), sum(len(v) for v in nets.values())))
    # net of every pin - needed to find the "other end" of a resistor
    pin_net = {}
    for name, nodes in nets.items():
        for ref, pin, fn, pt in nodes:
            pin_net[(ref, pin)] = name
    rails = {base(n) for n in nets if any(pt == 'power_out' for _, _, _, pt in nets[n])}
    rails |= set(project.get('supply_nets', []))

    def pulls_to_rail(net_name):
        """Resistors on the net whose other end sits on a supply rail."""
        out = []
        for ref, pin, fn, pt in nets[net_name]:
            if not ref.startswith('R'):
                continue
            if ref in dnp:
                continue        # a DNP pull-up is not on the board
            other = [p for (r, p) in pin_net if r == ref and p != pin]
            for p in other:
                tgt = base(pin_net[(ref, p)])
                if tgt in rails or tgt == 'GND':
                    out.append((ref, comps.get(ref, ''), tgt))
        return out

    issues = collections.defaultdict(list)

    for name, nodes in nets.items():
        b = base(name)
        if b in SKIP or b.startswith('unconnected-'):
            continue
        kinds = collections.Counter(pt for _, _, _, pt in nodes)

        # 1. open collector without a pull-up
        if any(k in kinds for k in PULLUP_KINDS):
            pulls = [p for p in pulls_to_rail(name) if p[2] != 'GND']
            if not pulls:
                who = [f'{r}.{fn or p}' for r, p, fn, pt in nodes if pt in PULLUP_KINDS]
                issues['open collector without pull-up'].append((b, ', '.join(who)))

        # 2. single-node net
        if len(nodes) == 1:
            issues['single-node net'].append((b, '%s.%s' % (nodes[0][0], nodes[0][2] or nodes[0][1])))

        # 3. a net with an input and NEITHER a source nor a passive path to anything.
        # Deliberately narrow: a first version flagged ~100 nets, nearly all legitimate - a
        # supply rail is power_in pins plus capacitors, a bootstrap node is a capacitor and a
        # pin, a strap is a resistor to ground. All "sourceless" by the letter and fine in
        # substance. The real defect is an input reached by no output, resistor or
        # capacitor: it floats.
        if len(nodes) > 1 and not any(pt in DRIVERS for _, _, _, pt in nodes):
            has_passive = any(pt == 'passive' for _, _, _, pt in nodes)
            has_pwr_in = any(pt == 'power_in' for _, _, _, pt in nodes)
            if not has_passive and not has_pwr_in:
                issues['input without source'].append(
                    (b, ', '.join('%s.%s[%s]' % (r, fn or p, pt) for r, p, fn, pt in nodes)))

        # There is deliberately no "strap without resistor" check. It was written and flagged
        # ~20 nets on a correct board with zero real defects: motor-driver charge-pump nodes,
        # converter bootstraps, crystal nodes, MCU NRST (internal pull-up), antenna feed, and
        # transistor gates driven through a resistor. A check with that many false hits on a
        # good board is harmful: people stop reading it. The real equivalent is "input without
        # source" above.
        # 4. two or more hard outputs on one net
        hard = [(r, fn or p) for r, p, fn, pt in nodes if pt in ('output', 'power_out')]
        if len(hard) > 1 and b not in rails:
            issues['multiple hard outputs'].append(
                (b, ', '.join('%s.%s' % x for x in hard)))

        # 5. strap on an input without a resistor
        if all(pt in ('input', 'passive') for _, _, _, pt in nodes) and len(nodes) <= 3:
            if not any(r.startswith('R') for r, _, _, _ in nodes):
                pass    # covered by check 3

    # 6. buses with pull-ups: how many pairs and the equivalent resistance
    print('\n=== pull-ups on two-wire buses ===')
    for name, nodes in sorted(nets.items()):
        b = base(name)
        if not re.search(r'(SDA|SCL|UART|_IO$)', b):
            continue
        pulls = [p for p in pulls_to_rail(name) if p[2] != 'GND']
        if not pulls:
            continue
        vals = [ohms(v) for _, v, _ in pulls]
        vals = [v for v in vals if v]
        eq = 1.0 / sum(1.0 / v for v in vals) if vals else None
        flag = ''
        if eq and eq < 2000:
            flag = '  <-- strong: the device must sink %.1f mA at 3.3 V' % (3.3 / eq * 1000)
        print('  %-12s pull-ups %d (%s) -> equivalent %s%s'
              % (b, len(pulls), ', '.join('%s=%s' % (r, v) for r, v, _ in pulls),
                 ('%.0f ohm' % eq) if eq else '?', flag))

    print()
    total = 0
    for k in sorted(issues):
        rows = issues[k]
        total += len(rows)
        print('=== %s: %d ===' % (k, len(rows)))
        for b, who in sorted(rows)[:20]:
            print('    %-18s %s' % (b, who[:96]))
        if len(rows) > 20:
            print('    ... and %d more' % (len(rows) - 20))
        print()
    print('findings total: %d' % total)
    return 0


if __name__ == '__main__':
    sys.exit(main())
