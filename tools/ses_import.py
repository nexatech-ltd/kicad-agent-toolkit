#!/usr/bin/env python3
"""Import a Specctra SES back into the project board (paths.BOARD).

The output scale is self-calibrated: Freerouting returns coordinates 10x larger than
the input at the same declared resolution, so the divisor is taken from the placement
section, using the known position of footprints on the board.

Copper of plane nets and of RF nets is kept from the source board, not taken from the
SES: it was exported as protect, and there is no reason to rely on it coming back.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import re,sys,json,uuid,math
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py is a sibling
from sexp import blocks

from paths import KAT_WORK, BOARD, NAME  # paths come from paths.py
import project
SCR=KAT_WORK
BRD=BOARD
SES=SCR+'/'+NAME+'.ses'
# <name>.map.json maps N0001... to net names. The SES cannot be imported without it,
# so it lives in work/ inside the project, not in a temporary directory.
inv=json.load(open(SCR+'/'+NAME+'.map.json'))['inv']
VIA={'PS_VIA_600_300':(0.6,0.3),'PS_VIA_800_400':(0.8,0.4)}

# --- protection against being overwritten by the KiCad editor ---------------------
# An open board editor once saved its in-memory version over command-line edits and
# rolled back about an hour of work. A lock file ~<name>.lck next to the board means the
# editor holds it open; writing at that moment is pointless.
import os as _os
_lck = _os.path.join(_os.path.dirname(_os.path.abspath(BRD)), '~' + _os.path.basename(BRD) + '.lck')
if _os.path.exists(_lck):
    raise SystemExit('REFUSED: the board is open in the KiCad editor (' + _lck + ').\n'
                     'Close the board editor without saving and retry.')
PLANE=project.plane_nets()
# RF copper is never rerouted (same config key as pick_nets.py / dsn_export.py).
RF_PREFIX=tuple(project.get('rf_net_prefixes', ['Net-(ANT', 'RF_']))
# Incremental mode (ONLY_NETS_FILE) pairs with the export: the router completes only the
# listed nets, receives all other copper as protect and does NOT return all of it in the
# SES. So copper of nets outside the list must come from the SOURCE board, otherwise it
# is lost: an import without this gave 372 unconnected instead of 254.
_ONLY = None
_onf = _os.environ.get('ONLY_NETS_FILE')
if _onf and _os.path.exists(_onf):
    _ONLY = {ln.strip() for ln in open(_onf) if ln.strip()}
    print(f'  ONLY_NETS mode: board copper of all nets is kept except {len(_ONLY)} routed ones')

def keep(net):
    b=net.split('/')[-1]
    if _ONLY is not None:
        return b not in _ONLY            # everything not handed to the router
    return b in PLANE or b.startswith(RF_PREFIX)

txt=open(SES).read()
brd=open(BRD).read()

# ---------- scale self-calibration ----------
pos={}
for a,b in blocks(brd,'footprint'):
    blk=brd[a:b]
    r=re.search(r'\(property "Reference" "([^"]+)"',blk)
    at=re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)',blk)
    if r and at: pos[r.group(1)]=(float(at.group(1)),float(at.group(2)))
ks=[]
for m in re.finditer(r'\(place (\S+) (-?\d+) (-?\d+) ',txt):
    ref=m.group(1).strip('"')
    if ref not in pos: continue
    bx,by=pos[ref]
    if abs(bx)>1: ks.append(int(m.group(2))/bx)
    if abs(by)>1: ks.append(-int(m.group(3))/by)
assert ks, 'could not calibrate the SES scale'
K=round(sum(ks)/len(ks))
spread=max(abs(k-K) for k in ks)
assert spread < K*1e-3, f'SES scale is inconsistent: K={K}, spread {spread}'
print(f'SES scale: 1 mm = {K} units (checked on {len(ks)} footprint coordinates)')

# ---------- parse network_out ----------
i=txt.find('(network_out')
assert i>0,'SES has no network_out'
body=txt[i:]
segs=[]; vias=[]; nets_seen=0
for m in re.finditer(r'\(net\s+(\S+)\s',body):
    alias=m.group(1).strip('"'); net=inv.get(alias)
    if net is None: continue
    nets_seen+=1
    j=m.end(); nxt=body.find('\n      (net ',j)
    chunk=body[j:nxt if nxt>0 else len(body)]
    if keep(net): continue
    for wm in re.finditer(r'\(path\s+(\S+)\s+([\d.]+)((?:\s+-?\d+)+)\s*\)',chunk):
        ly=wm.group(1).strip('"'); w=float(wm.group(2))/K
        c=[int(v) for v in wm.group(3).split()]
        pts=[(c[k]/K, -c[k+1]/K) for k in range(0,len(c),2)]
        for k in range(len(pts)-1):
            if pts[k]!=pts[k+1]: segs.append((net,ly,w,pts[k],pts[k+1]))
    for vm in re.finditer(r'\(via\s+(\S+)\s+(-?\d+)\s+(-?\d+)',chunk):
        nm=vm.group(1).strip('"')
        if nm not in VIA: continue
        sz,dr=VIA[nm]
        vias.append((net,(int(vm.group(2))/K, -int(vm.group(3))/K),sz,dr))
assert nets_seen>0, 'no net recognised in the SES'
# parse completeness check: paths declared in the SES vs paths parsed
declared_paths=len(re.findall(r'\(path\s',body))
declared_vias=len(re.findall(r'\(via\s',body))
parsed_paths=sum(1 for _ in re.finditer(r'\(path\s+(\S+)\s+([\d.]+)((?:\s+-?\d+)+)\s*\)',body))
assert parsed_paths==declared_paths, (
    f'parsed {parsed_paths} of {declared_paths} paths - SES format differs from what is expected')
print(f'parse check: paths {parsed_paths}/{declared_paths}, vias declared {declared_vias}')
print('from SES: nets',nets_seen,'| segments',len(segs),'| vias',len(vias))
ws=sorted({round(s[2],3) for s in segs})
print('  track widths from SES:',ws[:10])
xs=[p[0] for s in segs for p in (s[3],s[4])]
ys=[p[1] for s in segs for p in (s[3],s[4])]
if xs: print(f'  routing extent X {min(xs):.1f}..{max(xs):.1f}  Y {min(ys):.1f}..{max(ys):.1f}')

def _edge_bbox(src, margin=5.0):
    """Bounding box of the board's Edge.Cuts graphics, mm, with a margin (sanity check only)."""
    ex, ey = [], []
    for t in ('gr_line', 'gr_arc', 'gr_rect', 'gr_circle', 'gr_poly'):
        for a, b in blocks(src, t):
            blk = src[a:b]
            if '"Edge.Cuts"' not in blk: continue
            for x, y in re.findall(r'\((?:start|end|mid|center|xy) (-?[\d.]+) (-?[\d.]+)\)', blk):
                ex.append(float(x)); ey.append(float(y))
    if not ex: return None
    return min(ex)-margin, max(ex)+margin, min(ey)-margin, max(ey)+margin
# A scale error (e.g. a wrong 10x divisor) puts the routing far outside the board.
_bb=_edge_bbox(brd)
assert not xs or _bb is None or (_bb[0]<min(xs) and max(xs)<_bb[1] and _bb[2]<min(ys) and max(ys)<_bb[3]), \
    'routing lies outside the board outline'

# ---------- rewrite the board ----------
open(BRD+'.bakSES','w').write(brd)
kept=[]; cuts=[]
for tag in ('segment','via'):
    for a,b in blocks(brd,tag):
        blk=brd[a:b]
        nm=re.search(r'\(net "([^"]*)"\)',blk); net=nm.group(1) if nm else ''
        if net and keep(net): kept.append(blk)
        cuts.append((a,b))
cuts.sort()
out=[]; prev=0
for a,b in cuts:
    out.append(brd[prev:a]); prev=b
    while prev<len(brd) and brd[prev]=='\n': prev+=1
base=''.join(out)+brd[prev:]
head=base.rstrip()
assert head.endswith(')')
head=head[:-1].rstrip('\n')

add=['\t'+k.strip() for k in kept]
for net,ly,w,p1,p2 in segs:
    add.append(f'\t(segment\n\t\t(start {p1[0]:.4f} {p1[1]:.4f})\n\t\t(end {p2[0]:.4f} {p2[1]:.4f})\n'
               f'\t\t(width {w:.4f})\n\t\t(layer "{ly}")\n\t\t(net "{net}")\n\t\t(uuid "{uuid.uuid4()}")\n\t)')
for net,at,sz,dr in vias:
    add.append(f'\t(via\n\t\t(at {at[0]:.4f} {at[1]:.4f})\n\t\t(size {sz})\n\t\t(drill {dr})\n'
               f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(tenting front back)\n\t\t(net "{net}")\n\t\t(uuid "{uuid.uuid4()}")\n\t)')
open(BRD,'w').write(head+'\n'+'\n'.join(add)+'\n)\n')
print('written: kept copper',len(kept),'+ from SES',len(segs)+len(vias))
