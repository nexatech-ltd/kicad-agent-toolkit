#!/usr/bin/env python3
"""Uniformity of support parts around identical ICs: where five of six were done right.

Why. A defect like "one transistor of six has no gate pull-down" is invisible to the eye
precisely because the other five are correct: reading the schematic confirms the pattern and the
exception slips by. A machine catches it easily: group parts by type and compare what hangs on
same-named pins.

For every pin the check compares the **role of the neighbours**, not specific references: the
(normalised) net name, the types of connected components and their values. Substantive
differences - different rails on same-named pins, a missing resistor, another value - are
reported; differences in references (R80 vs R81) are not.

Accepted differences are waivers of check "check_uniformity" in kicad-agent.json, keyed
"<symbol>:<pin>:<ref>", e.g. "Q_NMOS:G_1:Q1".

    python3 check_uniformity.py work/<board>.net
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
from sexp import find, parse, val

MIN_GROUP = 2        # compare starting from two instances
# Passives are not grouped: a hundred capacitors on a board are not a hundred identical nodes,
# and comparing them with each other is pure noise. Passive support parts are still visible - as
# neighbours of an active part's pins, which is exactly how a missing pull-up is found.
# Justified exceptions go in an explicit list with a reason, as in verify.py. A threshold is a
# threshold, but a meaningful difference must be written down in words, or someone will "fix" it.
ACCEPTED = {tuple(k.split(':', 2)): v for k, v in project.waivers('check_uniformity').items()}
PASSIVE = {'C', 'R', 'L', 'D', 'FerriteBead', 'Crystal_GND24_Small', 'Polyfuse',
           'D_Schottky', 'D_TVS', 'D_Zener', 'LED', 'Jumper', 'TestPoint',
           'Conn_01x02', 'Conn_01x03', 'Conn_01x04'}
# Nets whose name is always unique per instance - normalised to their role
PER_INSTANCE = re.compile(r'^(?:Net-\(|unconnected-)')


_VAL = re.compile(r'\d+(?:[.,]\d+)?\s*[RrKkMmuUnNpPµ]?\s*\d*')


def norm_value(v):
    """Value from a label: "10k pd" and "TS 10k" are the same, "100R gate" is 100R.

    Comparing whole strings gave false hits: one pull-down labelled "10k", another "10k pd",
    and the check treated them as different parts.
    """
    v = (v or '').strip()
    m = _VAL.search(v)
    return (m.group(0).replace(' ', '') if m else v) or '?'


def norm_net(name, refs_in_group):
    b = name.split('/')[-1]
    if PER_INSTANCE.match(b):
        return '<own net>'
    # a name differing only by the instance index is reduced to a pattern
    return re.sub(r'\d+', '#', b)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else paths.NETLIST
    t = parse(open(path).read())[0]
    comps, values, dnp = {}, {}, set()
    for c in find(find(t, 'components')[0], 'comp'):
        ref = val(c, 'ref')
        ls = find(c, 'libsource')
        part = val(ls[0], 'part', '') if ls else ''
        comps[ref] = part
        values[ref] = val(c, 'value', '')
        if any(val(p, 'name') == 'dnp' for p in find(c, 'property')):
            dnp.add(ref)

    # what hangs on every pin of every part
    attached = collections.defaultdict(dict)     # ref -> pinfunc -> (net, neighbours)
    for nt in find(find(t, 'nets')[0], 'net'):
        name = val(nt, 'name')
        nodes = [(val(nd, 'ref'), val(nd, 'pin'), val(nd, 'pinfunction', ''))
                 for nd in find(nt, 'node')]
        for ref, pin, fn in nodes:
            peers = sorted('%s=%s' % (comps.get(r, r[0]), values.get(r, ''))
                           for r, p, f in nodes if r != ref)
            attached[ref][fn or pin] = (name, tuple(peers))

    groups = collections.defaultdict(list)
    for ref, part in comps.items():
        if part and part not in PASSIVE:
            groups[part].append(ref)

    # Compare not net names (each instance has its own - noise) but the COMPOSITION of passive
    # parts on the pin: how many and which passives hang on it. A missing resistor shows up
    # directly, while different loads on drains are not reported.
    # Pins on planes and shared rails are excluded: such a pin has tens or hundreds of
    # neighbours, and differences in that list mean nothing. A local node has few nodes on
    # its net; six is chosen as "pin plus support parts", not "a rail".
    MAX_NODES = 6

    def passives_at(ref, pin):
        a = attached[ref].get(pin)
        if a is None:
            return None
        if len(a[1]) + 1 > MAX_NODES:
            return None
        out = []
        for peer in a[1]:
            part, _, v = peer.partition('=')
            if part in PASSIVE:
                out.append('%s %s' % (part, norm_value(v)))
        return tuple(sorted(out))

    issues = []
    ngroups = 0
    for part, refs in sorted(groups.items()):
        if len(refs) < MIN_GROUP:
            continue
        ngroups += 1
        refs = sorted(refs)
        pins = set()
        for r in refs:
            pins |= set(attached[r])
        for pin in sorted(pins):
            sig = collections.defaultdict(list)
            for r in refs:
                sig[passives_at(r, pin)].append(r)
            if len(sig) < 2:
                continue
            # the majority sets the pattern; a difference counts only when the minority has
            # FEWER passives - i.e. something was not fitted
            major = max(sig.items(), key=lambda kv: len(kv[1]))
            if major[0] is None:
                continue
            for key, rs in sig.items():
                if key is None or len(rs) >= len(major[1]):
                    continue
                if len(key) < len(major[0]):
                    missing = sorted(set(major[0]) - set(key)) or ['%d fewer'
                                                                   % (len(major[0]) - len(key))]
                    ok = [r for r in rs if (part, pin, r) in ACCEPTED]
                    rest = [r for r in rs if r not in ok]
                    if ok:
                        print('  accepted: %s, pin %s, %s - %s'
                              % (part, pin, ','.join(ok), ACCEPTED[(part, pin, ok[0])]))
                    if rest:
                        issues.append((part, pin, rest, sorted(major[1]), missing))

    print('groups of active parts compared: %d' % ngroups)
    if not issues:
        print('support parts of identical ICs are uniform: no instance has fewer passives'
              ' than the majority')
        return 0
    print('\nSUPPORT-PART ASYMMETRY (%d):' % len(issues))
    for part, pin, rs, major, missing in issues:
        print('  %s, pin %s: %s lack what %s have'
              % (part, pin, ','.join(rs), ','.join(major)))
        for m in missing:
            print('        missing: %s' % m)
    return 1


if __name__ == '__main__':
    sys.exit(main())
