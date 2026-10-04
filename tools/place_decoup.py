#!/usr/bin/env python3
"""Pull decoupling capacitors close to an IC's power pins.

Usage:  python3 place_decoup.py U1 +3V3 C10,C11,C12 [--apply]

Assumes power and ground are planes, so a capacitor needs its own vias next to the pin
rather than long tracks: the task is purely geometric.
Assignment is greedy, re-evaluating the worst-served pin after every placement -
otherwise the algorithm serves the worst pins and creates new worst ones in their place.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import re, sys, math
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py lives next to this file
from sexp import blocks, sub
from paths import BOARD as BRD          # see paths.py
IC, NET, CAPS = sys.argv[1], sys.argv[2], sys.argv[3].split(',')
GAP=0.20
s=open(BRD).read()
def rot(px,py,deg):
    a=math.radians(deg); c,si=math.cos(a),math.sin(a); return px*c+py*si,-px*si+py*c
info={}; boxes=[]; pins=[]
for a,b in blocks(s,'footprint'):
    blk=s[a:b]
    ref=re.search(r'\(property "Reference" "([^"]+)"',blk).group(1)
    am=re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)',blk)
    fx,fy,fr=float(am.group(1)),float(am.group(2)),float(am.group(3) or 0)
    xs=[];ys=[]
    for g in sub(blk,'fp_rect')+sub(blk,'fp_line'):
        if 'CrtYd' not in g: continue
        for m in re.finditer(r'\((?:start|end) (-?[\d.]+) (-?[\d.]+)\)',g):
            dx,dy=rot(float(m.group(1)),float(m.group(2)),fr); xs.append(dx); ys.append(dy)
    if not xs: continue
    hw,hh=(max(xs)-min(xs))/2,(max(ys)-min(ys))/2
    info[ref]=(fx,fy,fr,hw,hh,a,b)
    if ref not in CAPS: boxes.append((fx-hw,fy-hh,fx+hw,fy+hh,ref))
    if ref==IC:
        for p in sub(blk,'pad'):
            nm=re.search(r'\(net "([^"]*)"\)',p); pa=re.search(r'\(at (-?[\d.]+) (-?[\d.]+)',p)
            if not nm or not pa or nm.group(1)!=NET: continue
            dx,dy=rot(float(pa.group(1)),float(pa.group(2)),fr)
            pins.append((fx+dx,fy+dy))
assert pins, f'{IC} has no pins on net {NET}'
assert all(c in info for c in CAPS), 'not all capacitors were found'
print(f'{IC}: pins on {NET}: {len(pins)}; moving {len(CAPS)} capacitors')
placed=[]; result={}; served=[]
def worst():
    best=None
    for px,py in pins:
        d=min((math.hypot(px-sx,py-sy) for sx,sy in served), default=999)
        if best is None or d>best[0]: best=(d,px,py)
    return best
# smallest (high-frequency) first - proximity matters most for them
order=sorted(CAPS,key=lambda c: info[c][3]*info[c][4])
for ref in order:
    _,px,py=worst()
    fx,fy,fr,hw,hh,_,_=info[ref]
    best=None; r=max(hw,hh)+0.6
    while r<=14.0 and best is None:
        steps=max(24,int(2*math.pi*r/0.15))
        for k in range(steps):
            ang=2*math.pi*k/steps
            x,y=px+r*math.cos(ang), py+r*math.sin(ang)
            ok=True
            for bx0,by0,bx1,by1,_ in boxes+placed:
                if not (x+hw+GAP<=bx0 or x-hw-GAP>=bx1 or y+hh+GAP<=by0 or y-hh-GAP>=by1): ok=False; break
            if ok:
                d=math.hypot(x-px,y-py)
                if best is None or d<best[0]: best=(d,x,y)
        r+=0.2
    assert best, f'no place found for {ref}'
    d,x,y=best
    placed.append((x-hw,y-hh,x+hw,y+hh,ref)); result[ref]=(round(x,3),round(y,3)); served.append((x,y))
    old=min(math.hypot(info[ref][0]-p[0],info[ref][1]-p[1]) for p in pins)
    print(f'  {ref:6s} ({hw*2:.1f}x{hh*2:.1f} mm) -> {d:5.2f} mm from the pin (was {old:6.2f})   at {x:.2f},{y:.2f}')
if '--apply' not in sys.argv:
    print('\ndry run shown. To write: --apply'); sys.exit(0)
open(BRD+f'.bakDec{IC}','w').write(s)
out=[];prev=0
for ref,(nx,ny) in sorted(result.items(), key=lambda kv: info[kv[0]][5]):
    a,b=info[ref][5],info[ref][6]
    nb=re.sub(r'(\n\t\t\(at )-?[\d.]+ -?[\d.]+', lambda m: m.group(1)+f'{nx} {ny}', s[a:b], count=1)
    out.append(s[prev:a]); out.append(nb); prev=b
out.append(s[prev:])
open(BRD,'w').write(''.join(out))
print(f'\nmoved: {len(result)}')
