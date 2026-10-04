"""Point edits of a board from an operation list (pcbnew, one LoadBoard, SaveBoard at the end).

Usage: python3 tools/util/edit_ops.py BOARD.kicad_pcb OPS.json [--apply]

Operations (net names are short names, without the sheet path):
  {"op":"del_tracks_box","net":"GND","layer":"F.Cu","box":[x0,y0,x1,y1]}  net's segments on a layer with both ends in the box
  {"op":"del_via_at","net":"GND","x":..,"y":..,"r":0.1,"all":false}       one via (all: every via within r)
  {"op":"del_net_run","net":"NET","keep":3.0}                              remove the run, keep escape necks within keep mm of the net's pads
  {"op":"del_net","net":"NET"}                                             remove ALL copper of the net (for a reroute)
  {"op":"del_seg","net":"GND","layer":"F.Cu","a":[x,y],"b":[x,y]}          one segment by exact ends (+-0.002 mm, either order)
  {"op":"renet_box","net":"NET","new":"NET2","box":[x0,y0,x1,y1]}          move the net's copper inside the box to another net
  {"op":"add_via","net":"NET","x":..,"y":..,"size":0.5,"drill":0.2}
  {"op":"add_seg","net":"NET","layer":"F.Cu","a":[x,y],"b":[x,y],"w":0.15}
  {"op":"set_width","net":"+3V3","layer":"In2.Cu","a":[x,y],"b":[x,y],"w":0.15}  width of one segment (undo a widening)
Without --apply nothing is saved. After saving, the nets of new items are verified by reloading
the board in a separate process.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, json, math, pcbnew
bp, ops = sys.argv[1], json.load(open(sys.argv[2])); apply = '--apply' in sys.argv
b = pcbnew.LoadBoard(bp); MM = 1e6
def V(x, y): return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))
# short net name -> NETINFO_ITEM (from pads and copper; KiCad 10 SWIG has no GetNetsByName)
NETS = {}
for fp in b.GetFootprints():
    for pd in fp.Pads():
        if pd.GetNetname(): NETS.setdefault(pd.GetNetname().split('/')[-1], pd.GetNet())
for t in b.GetTracks():
    if t.GetNetname(): NETS.setdefault(t.GetNetname().split('/')[-1], t.GetNet())

# all removals first, then additions: after b.Add(...) iterating GetTracks() breaks in KiCad 10 SWIG
ops = sorted(ops, key=lambda o: 0 if o['op'].startswith('del') else (1 if o['op'] == 'renet_box' else 2))
KEEP = []
GEOM = False   # geometry changed without new items (set_width): zones still need a refill
TR = list(b.GetTracks())   # single call: after Remove/Add a second GetTracks() does not iterate in KiCad 10 SWIG
GONE = set()
for o in ops:
    if o['op'] == 'del_tracks_box':
        x0, y0, x1, y1 = o['box']; L = b.GetLayerID(o['layer']); n = 0
        for t in TR:
            if id(t) in GONE or t.GetClass() != 'PCB_TRACK' or t.GetNetname().split('/')[-1] != o['net'] or t.GetLayer() != L: continue
            s, e = t.GetStart(), t.GetEnd()
            if all(x0 <= p.x / MM <= x1 and y0 <= p.y / MM <= y1 for p in (s, e)): b.Remove(t); GONE.add(id(t)); n += 1
        print('  segments removed %s/%s in box: %d' % (o['net'], o['layer'], n))
    elif o['op'] == 'del_net':
        n = 0
        for t in TR:
            if id(t) in GONE or t.GetNetname().split('/')[-1] != o['net']: continue
            b.Remove(t); GONE.add(id(t)); n += 1
        print('  all copper of %s removed: %d items' % (o['net'], n))
    elif o['op'] == 'del_net_run':
        # Reroute at the net-class width: a class-width escape does not fit out of a fine-pitch pad
        # (0.4 mm + 0.25 mm clearance does not fit a 0.5 mm pitch), so keep the necks at the pads
        # and remove only the run between them.
        keep = float(o.get('keep', 3.0)); pads = []
        for fp in b.GetFootprints():
            for pd in fp.Pads():
                if pd.GetNetname().split('/')[-1] == o['net']: pads.append((pd.GetPosition().x / MM, pd.GetPosition().y / MM))
        n = kept = 0
        for t in TR:
            if id(t) in GONE or t.GetNetname().split('/')[-1] != o['net']: continue
            if t.GetClass() == 'PCB_VIA':
                p_ = t.GetPosition(); pts = [(p_.x / MM, p_.y / MM)]
            else:
                s_, e_ = t.GetStart(), t.GetEnd(); pts = [(s_.x / MM, s_.y / MM), (e_.x / MM, e_.y / MM)]
            if pads and all(min(math.hypot(x - px, y - py) for px, py in pads) <= keep for x, y in pts): kept += 1; continue
            b.Remove(t); GONE.add(id(t)); n += 1
        print('  %s: run removed %d items, necks kept at pads %d' % (o['net'], n, kept))
    elif o['op'] == 'del_seg':
        L = b.GetLayerID(o['layer']); n = 0; (ax, ay), (bx, by) = o['a'], o['b']
        def near(p, x, y): return abs(p.x / MM - x) <= 0.002 and abs(p.y / MM - y) <= 0.002
        for t in TR:
            if id(t) in GONE or t.GetClass() != 'PCB_TRACK' or t.GetNetname().split('/')[-1] != o['net'] or t.GetLayer() != L: continue
            s, e = t.GetStart(), t.GetEnd()
            if (near(s, ax, ay) and near(e, bx, by)) or (near(s, bx, by) and near(e, ax, ay)): b.Remove(t); GONE.add(id(t)); n += 1
        print('  segment removed %s/%s (%.3f,%.3f)-(%.3f,%.3f): %d' % (o['net'], o['layer'], ax, ay, bx, by, n))
    elif o['op'] == 'set_width':
        # Undo a widening: the width is set on one segment found by its ends. widen_net.py widens a
        # whole net, while only individual segments end up in violations - revert just those.
        L = b.GetLayerID(o['layer']); n = 0; (ax, ay), (bx, by) = o['a'], o['b']
        def near_w(p, x, y): return abs(p.x / MM - x) <= 0.002 and abs(p.y / MM - y) <= 0.002
        for t in TR:
            if id(t) in GONE or t.GetClass() != 'PCB_TRACK' or t.GetNetname().split('/')[-1] != o['net'] or t.GetLayer() != L: continue
            s_, e_ = t.GetStart(), t.GetEnd()
            if (near_w(s_, ax, ay) and near_w(e_, bx, by)) or (near_w(s_, bx, by) and near_w(e_, ax, ay)):
                t.SetWidth(int(round(o['w'] * MM))); n += 1
        GEOM = True
        print('  width %s/%s (%.3f,%.3f)-(%.3f,%.3f) -> %.3f mm: %d' % (o['net'], o['layer'], ax, ay, bx, by, o['w'], n))
        if n == 0: sys.exit('ERROR: segment not found, width unchanged')
    elif o['op'] == 'del_via_at':
        n = 0
        for t in TR:   # by default ONE via per operation (duplicates at one point need separate ops); "all": true removes all
            if id(t) not in GONE and t.GetClass() == 'PCB_VIA' and t.GetNetname().split('/')[-1] == o['net'] and math.hypot(t.GetPosition().x / MM - o['x'], t.GetPosition().y / MM - o['y']) <= o.get('r', 0.1):
                b.Remove(t); GONE.add(id(t)); n += 1
                if not o.get('all'): break
        print('  vias removed %s at (%.2f,%.2f): %d' % (o['net'], o['x'], o['y'], n))
    elif o['op'] == 'renet_box':
        # move the net's copper (segments and vias) inside the box to net `new`: pads stay put, nets move
        x0, y0, x1, y1 = o['box']; n = 0
        for t in TR:
            if id(t) in GONE or t.GetNetname().split('/')[-1] != o['net']: continue
            if t.GetClass() == 'PCB_VIA':
                p = t.GetPosition(); inside = x0 <= p.x / MM <= x1 and y0 <= p.y / MM <= y1
            else:
                s_, e_ = t.GetStart(), t.GetEnd(); inside = all(x0 <= q.x / MM <= x1 and y0 <= q.y / MM <= y1 for q in (s_, e_))
            if inside: t.SetNet(NETS[o['new']]); n += 1
        print('  renetted %s -> %s in box: %d' % (o['net'], o['new'], n))
    elif o['op'] == 'add_via':
        v = pcbnew.PCB_VIA(b); v.SetPosition(V(o['x'], o['y'])); v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetWidth(int(o.get('size', 0.5) * MM)); v.SetDrill(int(o.get('drill', 0.2) * MM))
        b.Add(v); v.SetNetCode(NETS[o['net']].GetNetCode()); KEEP.append(v)   # set the net AFTER Add and by code: SetNet before Add on a board with removals yielded GND
        if v.GetNetname().split('/')[-1] != o['net']: sys.exit('ERROR: via has net %r instead of %r' % (v.GetNetname(), o['net']))
        print('  + via %s (%.3f,%.3f) %.2f/%.2f' % (o['net'], o['x'], o['y'], o.get('size', 0.5), o.get('drill', 0.2)))
    elif o['op'] == 'add_seg':
        t = pcbnew.PCB_TRACK(b); t.SetStart(V(*o['a'])); t.SetEnd(V(*o['b'])); t.SetWidth(int(o.get('w', 0.15) * MM)); t.SetLayer(b.GetLayerID(o['layer']))
        b.Add(t); t.SetNetCode(NETS[o['net']].GetNetCode()); KEEP.append(t)
        if t.GetNetname().split('/')[-1] != o['net']: sys.exit('ERROR: segment has net %r instead of %r' % (t.GetNetname(), o['net']))
        print('  + seg %s %s (%.3f,%.3f)-(%.3f,%.3f) w%.2f' % (o['net'], o['layer'], *o['a'], *o['b'], o.get('w', 0.15)))
if apply:
    if KEEP or GEOM:
        # SaveBoard builds connectivity and PROPAGATES nets from pads/fills onto new items: a new via on top of a
        # stale GND fill (the clearance hole of a removed neighbour not yet recomputed) was saved as GND. Refilling
        # before saving gives the fill clearance from the new items, so they keep their net. Verified by a reload
        # in a separate process.
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(bp, b); print('saved', bp)
    if KEEP:
        import subprocess
        chk = subprocess.run([sys.executable, '-c', """
import pcbnew, json, sys, math
b = pcbnew.LoadBoard(sys.argv[1]); MM = 1e6; bad = 0
for o in json.load(open(sys.argv[2])):
    if o['op'] == 'add_via':
        for t in b.GetTracks():
            if t.GetClass() == 'PCB_VIA' and math.hypot(t.GetPosition().x / MM - o['x'], t.GetPosition().y / MM - o['y']) < 0.005 and t.GetNetname().split('/')[-1] != o['net']:
                bad += 1; print('  WRONG NET after save:', o, '->', t.GetNetname())
    elif o['op'] == 'add_seg':
        for t in b.GetTracks():
            if t.GetClass() == 'PCB_TRACK' and b.GetLayerName(t.GetLayer()) == o['layer'] and abs(t.GetStart().x / MM - o['a'][0]) < 0.005 and abs(t.GetStart().y / MM - o['a'][1]) < 0.005 and abs(t.GetEnd().x / MM - o['b'][0]) < 0.005 and abs(t.GetEnd().y / MM - o['b'][1]) < 0.005 and t.GetNetname().split('/')[-1] != o['net']:
                bad += 1; print('  WRONG NET after save:', o, '->', t.GetNetname())
sys.exit(1 if bad else 0)
""", bp, sys.argv[2]], capture_output=True, text=True)
        out = [l for l in chk.stdout.splitlines() if 'WRONG NET' in l]
        if chk.returncode: sys.exit('\n'.join(out) + '\nERROR: nets of new items changed on save - the board is written but needs fixing')
        print('  nets of new items after reload: correct')
