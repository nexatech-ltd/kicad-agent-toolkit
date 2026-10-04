"""Which component pins sit on a net, read from a KiCad netlist (multi-line s-expression format).

Usage: python3 tools/util/netnodes.py NETLIST.net NET [NET ...]

Prints reference, pin number, pin function and pin type for every node of each named net
(short names, the sheet path is stripped).
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, re
want = set(sys.argv[2:]); net=None; cur={}; out={w:[] for w in want}
for line in open(sys.argv[1]):
    s=line.strip()
    if re.match(r'^\t{3}\(name ', line.rstrip('\n')):
        m=re.match(r'\(name "(.*)"\)$', s); net=m.group(1).split('/')[-1]
    if s=='(node': cur={}
    for k in ('ref','pin','pinfunction','pintype'):
        m=re.match(r'\(%s "(.*)"\)$'%k, s)
        if m: cur[k]=m.group(1)
    if s==')' and 'ref' in cur and 'pin' in cur and net in want:
        out[net].append((cur['ref'], cur['pin'], cur.get('pinfunction',''), cur.get('pintype',''))); cur={}
for w in sys.argv[2:]:
    print('=== %s - nodes %d' % (w, len(out[w])))
    for r,p,f,t in out[w]: print('  %-6s pin %-4s %-14s %s' % (r,p,f,t))
