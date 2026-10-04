#!/usr/bin/env python3
"""Export the project board (paths.BOARD) to Specctra DSN for Freerouting.

Key decisions:
  * every footprint gets its own image, with rotations baked into the pad geometry ->
    a component is placed as (place REF x y front 0). This removes any ambiguity in the
    semantics of KiCad pad angles.
  * nets are renamed to N0001... - avoids trouble with quotes and spaces in hierarchical
    names such as "/Motor drivers x4, sensors x8/MOT_B2".
  * nets that have a power pour (config key `plane_nets`) are declared with an empty pin
    list: Freerouting does not route them, but their existing copper stays an obstacle.

Writes work/<board name>.dsn and work/<board name>.map.json (needed by ses_import.py).
"""
import re, sys, json, math, hashlib, fnmatch
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sexp.py is a sibling
from sexp import blocks, sub

from paths import BOARD, NAME, work, sibling   # paths come from paths.py
import project
BRD = BOARD
PRO = sibling(BRD, '.kicad_pro')
OUT = work(NAME + '.dsn')
MAP = OUT.replace('.dsn','.map.json')

SIGNAL = ['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu']
PLANE_NETS = project.plane_nets()          # poured as planes, nothing to route
# RF nets: their copper is laid by calculation and exported as protect (config key
# `rf_net_prefixes`, shared with pick_nets.py and ses_import.py).
RF_PREFIX = tuple(project.get('rf_net_prefixes', ['Net-(ANT', 'RF_']))
U = lambda mm: int(round(mm*10000.0))      # (resolution um 10)

s = open(BRD).read()
pro = json.load(open(PRO))

# ---------- helpers ----------
def rot(px,py,deg):
    """Rotation in the KiCad coordinate system (Y down)."""
    a=math.radians(deg); c,si=math.cos(a),math.sin(a)
    return px*c+py*si, -px*si+py*c

def fnum(blk,tag,n=2,after=0):
    m=re.search(r'\('+tag+r' '+r' '.join([r'(-?[\d.]+)']*n),blk[after:])
    return tuple(float(x) for x in m.groups()) if m else None

# ---------- board outline ----------
edges=[]
for t in ('gr_line','gr_arc','gr_rect'):
    for a,b in blocks(s,t):
        blk=s[a:b]
        if '"Edge.Cuts"' not in blk: continue
        if t=='gr_line':
            x1,y1=fnum(blk,'start'); x2,y2=fnum(blk,'end'); edges.append([(x1,y1),(x2,y2)])
        elif t=='gr_rect':
            x1,y1=fnum(blk,'start'); x2,y2=fnum(blk,'end')
            edges.append([(x1,y1),(x2,y1),(x2,y2),(x1,y2),(x1,y1)])
        else:
            x1,y1=fnum(blk,'start'); mx,my=fnum(blk,'mid'); x2,y2=fnum(blk,'end')
            # circle centre from three points
            d=2*(x1*(my-y2)+mx*(y2-y1)+x2*(y1-my))
            if abs(d)<1e-9: edges.append([(x1,y1),(x2,y2)]); continue
            ux=((x1*x1+y1*y1)*(my-y2)+(mx*mx+my*my)*(y2-y1)+(x2*x2+y2*y2)*(y1-my))/d
            uy=((x1*x1+y1*y1)*(x2-mx)+(mx*mx+my*my)*(x1-x2)+(x2*x2+y2*y2)*(mx-x1))/d
            r=math.hypot(x1-ux,y1-uy)
            a0=math.atan2(y1-uy,x1-ux); am=math.atan2(my-uy,mx-ux); a2=math.atan2(y2-uy,x2-ux)
            def norm(x): 
                while x<0: x+=2*math.pi
                while x>=2*math.pi: x-=2*math.pi
                return x
            sweep_ccw = norm(am-a0) < norm(a2-a0)
            span = norm(a2-a0) if sweep_ccw else -norm(a0-a2)
            pts=[(ux+r*math.cos(a0+span*k/12), uy+r*math.sin(a0+span*k/12)) for k in range(13)]
            edges.append(pts)

# chain into a closed outline
def chain(edges):
    edges=[list(e) for e in edges]; loop=edges.pop(0)
    while edges:
        best=None
        for i,e in enumerate(edges):
            for rev in (0,1):
                p=e[::-1] if rev else e
                d=math.hypot(loop[-1][0]-p[0][0], loop[-1][1]-p[0][1])
                if best is None or d<best[0]: best=(d,i,rev)
        d,i,rev=best
        e=edges.pop(i); loop.extend((e[::-1] if rev else e)[1:])
    return loop
outline=chain(edges)
assert math.hypot(outline[0][0]-outline[-1][0], outline[0][1]-outline[-1][1])<0.01, 'outline is not closed'

# Inward offset of the outline. Freerouting does not apply clearance to the routing area
# boundary and lays tracks right against it, so the board's copper-to-edge rule has to be
# built into the geometry itself: inset = edge clearance (config key
# `copper_edge_clearance`, default 0.5) + half the default track width. Do not take more:
# connector pads close to the edge (e.g. 0.95 mm away) would end up almost on the boundary.
EDGE_INSET = float(project.get('copper_edge_clearance', 0.5)) + 0.1
def _pts_of(blk):
    r"""Points of (polygon (pts ...)) taken by bracket BALANCE.

    A non-greedy r'\(polygon\s*\(pts(.*?)\)\s*\)' lies here: points are written on one
    line, and the outer \) of the pattern steals the closing bracket of the last (xy ...).
    findall, which needs that bracket, returns one point fewer - every keepout went into
    the DSN halved (a rectangle as a triangle), and the router entered the other half as
    allowed space."""
    i = blk.find('(polygon')
    if i < 0: return []
    j = blk.find('(pts', i)
    if j < 0: return []
    d = 0
    for k in range(j, len(blk)):
        if blk[k] == '(': d += 1
        elif blk[k] == ')':
            d -= 1
            if d == 0:
                return [(float(x), float(y))
                        for x, y in re.findall(r'\(xy (-?[\d.]+) (-?[\d.]+)\)', blk[j:k+1])]
    return []

def shrink(poly, d):
    """True inward polygon offset by d along edge normals (mitre).

    Moving vertices "towards the centre" does not work: on an elongated board the radial
    direction at the middle of a long side is far from perpendicular to it, and the inset
    comes out half of what is needed."""
    pts = poly[:-1] if math.hypot(poly[0][0]-poly[-1][0], poly[0][1]-poly[-1][1]) < 1e-6 else poly[:]
    n = len(pts)
    area = sum(pts[i][0]*pts[(i+1)%n][1] - pts[(i+1)%n][0]*pts[i][1] for i in range(n)) / 2
    sgn = 1.0 if area > 0 else -1.0        # the inward normal depends on winding
    def nrm(i):
        x1,y1 = pts[i]; x2,y2 = pts[(i+1)%n]
        dx,dy = x2-x1, y2-y1; L = math.hypot(dx,dy)
        if L < 1e-12: return None
        return (-dy/L*sgn, dx/L*sgn)
    out=[]
    for i in range(n):
        n1 = nrm((i-1) % n); n2 = nrm(i)
        if n1 is None or n2 is None:
            out.append(pts[i]); continue
        sx, sy = n1[0]+n2[0], n1[1]+n2[1]
        dot = n1[0]*n2[0] + n1[1]*n2[1]
        k = d / (1.0 + dot) if (1.0 + dot) > 1e-6 else d
        out.append((pts[i][0] + sx*k, pts[i][1] + sy*k))
    out.append(out[0])
    return out
outline = shrink(outline, EDGE_INSET)
_xs=[p[0] for p in outline]; _ys=[p[1] for p in outline]
print(f'outline with {EDGE_INSET} mm inset: X {min(_xs):.2f}..{max(_xs):.2f} Y {min(_ys):.2f}..{max(_ys):.2f}')

# ---------- keepout zones ----------
# Both top-level zones and zones INSIDE footprints are taken: the latter carry antenna
# keepouts of radio modules and keepouts under card-socket contacts. Without them the
# router lays tracks right into the antenna area of a certified module.
def _fp_keepout_blocks(src):
    out=[]
    for fa,fb in blocks(src,'footprint'):
        fblk=src[fa:fb]
        am=re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)',fblk)
        if not am: continue
        fx,fy=float(am.group(1)),float(am.group(2)); fr=float(am.group(3) or 0)
        for z in sub(fblk,'zone'):
            if '(keepout' not in z: continue
            out.append((z,fx,fy,fr))
    return out
FP_KEEPOUTS=_fp_keepout_blocks(s)
keepouts=[]
for z,fx,fy,fr in FP_KEEPOUTS:
    lys=re.findall(r'"((?:F|B|In\d)\.Cu)"', z[:600])
    raw=_pts_of(z)
    if not raw or not lys: continue
    if len(raw)<3: continue
    # IMPORTANT: the polygon of a zone inside a footprint is in BOARD coordinates, not
    # footprint coordinates. Applying the footprint rotation and offset moves a module's
    # antenna zone off the board (observed X -21.9..22.9), and the router no longer sees it.
    pts=raw
    for ly in lys: keepouts.append((ly,pts))
print('keepout zones from footprints:',len(FP_KEEPOUTS))
for a,b in blocks(s,'zone'):
    blk=s[a:b]
    if '(keepout' not in blk: continue
    lys=re.findall(r'"((?:F|B|In\d)\.Cu)"', blk[:blk.find('(name')] if '(name' in blk else blk[:300])
    head=blk[:blk.find('(filled_polygon')] if '(filled_polygon' in blk else blk
    pts=_pts_of(head)
    if not pts: continue
    for ly in lys: keepouts.append((ly,pts))

# ---------- footprints and pads ----------
comps=[]        # (ref, fx, fy, pins[(pinid, padstack_sig, dx, dy)], )
padstacks={}    # sig -> (name, layers, shape_pts)
net_of={}       # (ref,pinid) -> netname
for a,b in blocks(s,'footprint'):
    blk=s[a:b]
    rm=re.search(r'\(property "Reference" "([^"]+)"',blk)
    if not rm: continue
    ref=rm.group(1)
    am=re.search(r'\n\t\t\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)',blk)
    fx,fy=float(am.group(1)),float(am.group(2)); fr=float(am.group(3) or 0)
    seen={}; pins=[]
    for p in sub(blk,'pad'):
        hm=re.match(r'\(pad "([^"]*)" (\w+) (\w+)',p)
        if not hm: continue
        num,ptype,pshape=hm.groups()
        pa=re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)',p)
        px,py=float(pa.group(1)),float(pa.group(2))
        # The pad angle is the third argument of its (at ...); when absent it is ZERO,
        # not the footprint angle. A former `else fr` rotated pad extents on every rotated
        # footprint, and Freerouting got a distorted obstacle map: space next to the pads
        # of 1206 current-sense resistors (rotated 90 degrees) looked free. That produced
        # violations that stayed at the same level for five rounds in a row.
        pang=float(pa.group(3)) if pa.group(3) is not None else 0.0
        sz=re.search(r'\(size ([\d.]+) ([\d.]+)\)',p)
        w,h=(float(sz.group(1)),float(sz.group(2))) if sz else (0.3,0.3)
        lys=re.search(r'\(layers ([^)]*)\)',p)
        lay=re.findall(r'"([^"]+)"',lys.group(1)) if lys else []
        cu=[]
        for L in lay:
            if L=='*.Cu': cu=list(SIGNAL); break
            if L.endswith('.Cu'): cu.append(L)
        if not cu:
            if ptype=='np_thru_hole': cu=list(SIGNAL)
            else: continue
        nm=re.search(r'\(net "([^"]*)"\)',p)
        net=nm.group(1) if nm else ''
        # pad position in board coordinates
        dx,dy=rot(px,py,fr)
        # pad polygon relative to its centre, rotation baked in
        if pshape=='circle':
            r=w/2; poly=[(r*math.cos(2*math.pi*k/16), r*math.sin(2*math.pi*k/16)) for k in range(16)]
        else:   # rect / roundrect / oval / custom -> bounding rectangle
            hw,hh=w/2,h/2
            poly=[rot(sx*hw,sy*hh,pang) for sx,sy in ((-1,-1),(1,-1),(1,1),(-1,1))]
        key=num if num else 'X'
        n=seen.get(key,0); seen[key]=n+1
        pid=key if n==0 else f'{key}@{n}'
        sig=hashlib.md5((','.join(sorted(cu))+'|'+
              ';'.join(f'{x:.4f},{y:.4f}' for x,y in poly)).encode()).hexdigest()[:12]
        if sig not in padstacks: padstacks[sig]=('PS_'+sig, cu, poly)
        pins.append((pid,sig,dx,dy))
        if net: net_of[(ref,pid)]=net
    comps.append((ref,fx,fy,pins))

# ---------- existing copper ----------
wires=[]; vias=[]
for a,b in blocks(s,'segment'):
    blk=s[a:b]
    st=fnum(blk,'start'); en=fnum(blk,'end')
    w=float(re.search(r'\(width ([\d.]+)\)',blk).group(1))
    ly=re.search(r'\(layer "([^"]+)"\)',blk).group(1)
    nm=re.search(r'\(net "([^"]*)"\)',blk); net=nm.group(1) if nm else ''
    wires.append((net,ly,w,st,en))
for a,b in blocks(s,'via'):
    blk=s[a:b]
    at=fnum(blk,'at'); sz=float(re.search(r'\(size ([\d.]+)\)',blk).group(1))
    dr=float(re.search(r'\(drill ([\d.]+)\)',blk).group(1))
    nm=re.search(r'\(net "([^"]*)"\)',blk); net=nm.group(1) if nm else ''
    vias.append((net,at,sz,dr))

# ---------- net classes ----------
pats=pro['net_settings']['netclass_patterns']
classes={c['name']:c for c in pro['net_settings']['classes']}

# Clearances from the board's .kicad_dru: rules such as
#   (constraint clearance (min 0.25mm)) (condition "A.NetClass == 'POWER' && ...")
# Freerouting does not know them, but KiCad flags them - without this a run produces
# violations of those custom rules.
DRU = sibling(BRD, '.kicad_dru')
dru_clear = {}
try:
    dt = open(DRU).read()
    for m in re.finditer(r'\(rule\s+"[^"]+"\s*\(constraint clearance \(min ([\d.]+)mm\)\)\s*'
                         r'\(condition "([^"]*)"\)\)', dt):
        mm = float(m.group(1)); cond = m.group(2)
        cm = re.search(r"A\.NetClass\s*==\s*'([^']+)'", cond)
        if cm: dru_clear[cm.group(1)] = max(dru_clear.get(cm.group(1), 0), mm)
    print('class clearances from .kicad_dru:', dru_clear)
except FileNotFoundError:
    print('warning: .kicad_dru not found, class clearances taken from the net classes')
def netclass(n):
    base=n.split('/')[-1]
    for p in pats:
        if fnmatch.fnmatch(base,p['pattern']): return p['netclass']
    return 'Default'

allnets=sorted({v for v in net_of.values()})
alias={n:f'N{i+1:04d}' for i,n in enumerate(allnets)}
json.dump({'alias':alias,'inv':{v:k for k,v in alias.items()}},open(MAP,'w'))

pins_of={}
for (ref,pid),net in net_of.items():
    pins_of.setdefault(net,[]).append(f'{ref}-{pid}')

# ---------- output ----------
o=[]
o.append(f'(pcb {NAME}.dsn')
o.append('  (parser')
o.append('    (string_quote ")')
o.append('    (space_in_quoted_tokens on)')
o.append('    (host_cad "KiCad")')
o.append('    (host_version "10.0.6")')
o.append('  )')
o.append('  (resolution um 10)')
o.append('  (unit um)')
o.append('  (structure')
for i,L in enumerate(SIGNAL):
    o.append(f'    (layer {L}')
    o.append('      (type signal)')
    o.append(f'      (property (index {i}))')
    o.append('    )')
o.append('    (boundary')
o.append('      (path pcb 0 '+' '.join(f'{U(x)} {U(-y)}' for x,y in outline)+')')
o.append('    )')
# Keepouts are exported GROWN. As with the board outline, Freerouting does not apply
# clearance to a keepout boundary and lays a track right against it: three
# items_not_allowed violations were observed where a track edge entered the zone by
# microns. 0.55 = clearance 0.15 + via radius 0.4 mm. 0.30 would cover the track width,
# but the router also places vias at the boundary, and they are wider: at 0.30 an
# items_not_allowed from a via edge was observed.
KEEPOUT_GROW = 0.55
for ly,pts in keepouts:
    g = shrink(list(pts), -KEEPOUT_GROW)
    body = ' '.join(f'{U(x)} {U(-y)}' for x,y in g)
    o.append(f'    (keepout "" (polygon {ly} 0 {body}))')
    # (keepout ...) in Specctra forbids WIRES; vias are forbidden separately. The board
    # zones carry (vias not_allowed), so the ban is duplicated - without it the router
    # placed vias under a module antenna.
    o.append(f'    (via_keepout "" (polygon {ly} 0 {body}))')
o.append('    (via PS_VIA_600_300 PS_VIA_800_400)')
o.append('    (rule')
o.append('      (width 2000)')
o.append('      (clearance 1500)')
o.append('      (clearance 1500 (type default_smd))')
o.append('      (clearance 1500 (type smd_smd))')
o.append('    )')
o.append('  )')
o.append('  (placement')
for ref,fx,fy,pins in comps:
    o.append(f'    (component IMG_{ref}')
    o.append(f'      (place {ref} {U(fx)} {U(-fy)} front 0)')
    o.append('    )')
o.append('  )')
o.append('  (library')
for ref,fx,fy,pins in comps:
    o.append(f'    (image IMG_{ref}')
    for pid,sig,dx,dy in pins:
        o.append(f'      (pin {padstacks[sig][0]} {pid} {U(dx)} {U(-dy)})')
    o.append('    )')
for sig,(name,cu,poly) in padstacks.items():
    o.append(f'    (padstack {name}')
    for L in cu:
        o.append(f'      (shape (polygon {L} 0 '+' '.join(f'{U(x)} {U(-y)}' for x,y in poly)+'))')
    o.append('      (attach off)')
    o.append('    )')
for nm,d in (('PS_VIA_600_300',0.6),('PS_VIA_800_400',0.8)):
    o.append(f'    (padstack {nm}')
    for L in SIGNAL:
        o.append(f'      (shape (circle {L} {U(d)}))')
    o.append('      (attach off)')
    o.append('    )')
o.append('  )')
o.append('  (network')
ONLY=None
_of=os.environ.get('ONLY_NETS_FILE') if 'os' in dir() else None
import os as _os
_of=_os.environ.get('ONLY_NETS_FILE')
if _of:
    ONLY={l.strip() for l in open(_of) if l.strip()}
    print('ONLY_NETS mode: nets to route',len(ONLY))
for n in allnets:
    if n in PLANE_NETS or (ONLY is not None and n.split('/')[-1] not in ONLY):
        o.append(f'    (net {alias[n]})')          # copper exists, nothing to route
    else:
        o.append(f'    (net {alias[n]}')
        o.append('      (pins '+' '.join(sorted(pins_of[n]))+')')
        o.append('    )')
byclass={}
for n in allnets: byclass.setdefault(netclass(n),[]).append(n)
for cname,ns in byclass.items():
    c=classes.get(cname,classes['Default'])
    via = 'PS_VIA_800_400' if (c.get('via_diameter') or 0.6)>0.7 else 'PS_VIA_600_300'
    o.append(f'    (class C_{cname} '+' '.join(alias[n] for n in ns))
    o.append(f'      (circuit (use_via {via}))')
    o.append('      (rule')
    o.append(f'        (width {U(c.get("track_width",0.2))})')
    _clr = max(0.15, c.get("clearance", 0.15), dru_clear.get(cname, 0))
    o.append(f'        (clearance {U(_clr)})')
    o.append('      )')
    o.append('    )')
o.append('  )')
o.append('  (wiring')
KEEP = set(PLANE_NETS)
kept=0
ALLPROT = _of is not None
for net,ly,w,st,en in wires:
    base=net.split('/')[-1]
    if not ALLPROT and base not in KEEP and not base.startswith(RF_PREFIX): continue
    if net not in alias: continue
    o.append(f'    (wire (path {ly} {U(w)} {U(st[0])} {U(-st[1])} {U(en[0])} {U(-en[1])}) (net {alias[net]}) (type protect))'); kept+=1
import os
DROP_STITCH = os.environ.get('DROP_STITCH')=='1'
# Vias of ALL plane nets, not just ground: they are through-holes and cut every channel.
# They are not removed from the board and the import keeps them - the router just does not see them.
DROP_PLANE_VIAS = os.environ.get('DROP_PLANE_VIAS')=='1'
nvia=0
for net,at,sz,dr in vias:
    if net not in alias: continue
    base=net.split('/')[-1]
    if not ALLPROT and base not in KEEP and not base.startswith(RF_PREFIX): continue
    if DROP_STITCH and base=='GND': continue
    if DROP_PLANE_VIAS and base in PLANE_NETS: continue
    nm='PS_VIA_800_400' if sz>0.7 else 'PS_VIA_600_300'
    o.append(f'    (via {nm} {U(at[0])} {U(-at[1])} (net {alias[net]}) (type protect))'); kept+=1
o.append('  )')
o.append(')')
open(OUT,'w').write('\n'.join(o)+'\n')
print('footprints',len(comps),'| padstacks',len(padstacks),'| nets',len(allnets),
      '| outline points',len(outline),'| keepouts',len(keepouts),'| protected copper',kept)
print('->',OUT)
