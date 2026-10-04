"""Pin map of components from a KiCad netlist (multi-line s-expression format).

Usage: python3 tools/util/pinmap.py NETLIST.net U1 [U2 ...]

Prints pin number -> pin name -> net -> pin type for each reference. Handy for checking a
pinout against the datasheet after a package or variant change.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, re
refs = set(sys.argv[2:]); out = {r: {} for r in refs}
net = None; cur = {}
for line in open(sys.argv[1]):
    s = line.strip()
    m = re.match(r'\(name "(.*)"\)$', s)
    if m and re.match(r'^\t{3}\(name ', line.rstrip('\n')): net = m.group(1)
    if s == '(node': cur = {}
    for k in ('ref', 'pin', 'pinfunction', 'pintype'):
        m = re.match(r'\(%s "(.*)"\)$' % k, s)
        if m: cur[k] = m.group(1)
    if s == ')' and cur.get('ref') in refs and 'pin' in cur:
        out[cur['ref']][cur['pin']] = (cur.get('pinfunction', ''), (net or '').split('/')[-1], cur.get('pintype', ''))
        cur = {}
for r in sys.argv[2:]:
    print('=== %s - pins in netlist: %d' % (r, len(out[r])))
    for p in sorted(out[r], key=lambda k: (len(k), k)):
        fn, n, tp = out[r][p]
        print('  %-4s %-14s %-24s %s' % (p, fn, n, tp))
