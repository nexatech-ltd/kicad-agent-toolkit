#!/usr/bin/env python3
"""Remove the copper of a given net class - for re-routing by the router.

Needed where partial routing blocks a structure that matters more than it does.
A motor phase track can be routed ten ways, while a power pin is tied to its
package: if a phase stub occupies the only pocket for a via into the plane,
the stub is what has to go.

Works only on nets the router actually routes: plane nets (project.plane_nets())
are handed to it as pours with an empty pin list, so copper removed from them
would never come back.

Usage:   python3 strip_class.py MOTOR              - plan for a class
         python3 strip_class.py MOTOR --apply      - write
         python3 strip_class.py --net STEP1        - plan for one net

The board comes from the BRD environment variable (default: paths.BOARD).
"""
import json, re, sys, os, math, fnmatch, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py lives next to this file
from sexp import blocks
import paths, project

BRD  = os.environ.get('BRD') or paths.BOARD
PRO  = paths.sibling(BRD, '.kicad_pro')
PLANE_NETS = project.plane_nets()

BY_NET = '--net' in sys.argv
if BY_NET:
    i = sys.argv.index('--net')
    if i + 1 >= len(sys.argv):
        raise SystemExit(__doc__)
    WANT = sys.argv[i + 1]
elif len(sys.argv) < 2 or sys.argv[1].startswith('--'):
    raise SystemExit(__doc__)
else:
    WANT = sys.argv[1]
APPLY = '--apply' in sys.argv

_lck = os.path.join(os.path.dirname(os.path.abspath(BRD)), '~' + os.path.basename(BRD) + '.lck')
if os.path.exists(_lck) and APPLY:
    raise SystemExit(f'REFUSED: the board is open in the KiCad editor ({_lck}).')

s = open(BRD).read()
pats = json.load(open(PRO))['net_settings'].get('netclass_patterns', [])

def netclass(n):
    base = n.split('/')[-1]
    for p in pats:
        if fnmatch.fnmatch(base, p['pattern']):
            return p['netclass']
    return 'Default'

hits, nets = [], collections.Counter()
for tag in ('segment', 'via', 'arc'):
    for a, b in blocks(s, tag):
        blk = s[a:b]
        nm = re.search(r'\(net "([^"]*)"\)', blk)
        if not nm: continue
        net = nm.group(1)
        base = net.split('/')[-1]
        if base in PLANE_NETS: continue
        if (base != WANT) if BY_NET else (netclass(net) != WANT): continue
        L = 0.0
        st = re.search(r'\(start (-?[\d.]+) (-?[\d.]+)\)', blk)
        en = re.search(r'\(end (-?[\d.]+) (-?[\d.]+)\)', blk)
        if st and en:
            L = math.hypot(float(en.group(1)) - float(st.group(1)),
                           float(en.group(2)) - float(st.group(2)))
        hits.append((a, b, tag, base, L))
        nets[base] += 1

print(f'{"net" if BY_NET else "class"} {WANT}: {len(hits)} objects in {len(nets)} nets, '
      f'total length {sum(h[4] for h in hits):.1f} mm')
by = collections.Counter(h[2] for h in hits)
print('  by type:', dict(by))
print('  nets:', ', '.join(sorted(nets)))
if not hits:
    sys.exit(0)
if not APPLY:
    flag = f'--net {WANT}' if BY_NET else WANT
    print(f'\nplan shown. To write: python3 strip_class.py {flag} --apply')
    sys.exit(0)

open(BRD + f'.bakStrip{WANT}', 'w').write(s)
out, prev = [], 0
for a, b, *_ in sorted(hits, key=lambda x: x[0]):
    out.append(s[prev:a]); prev = b
out.append(s[prev:])
open(BRD, 'w').write(''.join(out))
print(f'\nremoved {len(hits)} objects, backup .bakStrip{WANT}')
