#!/usr/bin/env python3
"""Drilling cleanup: remove duplicate vias and same-net vias that are too close, driven by a DRC report.
  python3 tools/fix_holes.py --board B --drc D.json [--apply]
holes_co_located: several identical vias at one point -> keep one.
hole_to_hole (< min_hole_to_hole): both vias of the same net -> remove the one with fewer tracks attached
  (a plane stitch with no tracks goes first); a via against a PTH pad of the same net -> remove the via;
  different nets -> report only (needs rerouting). DRC after the run is the judge: unconnected items
  and errors must not increase."""
import sys, json, math, argparse, pcbnew
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--drc', required=True); ap.add_argument('--apply', action='store_true')
a = ap.parse_args(); MM = 1e6
b = pcbnew.LoadBoard(a.board); d = json.load(open(a.drc))
TR = list(b.GetTracks())
vias = [t for t in TR if t.GetClass() == 'PCB_VIA']
segs = [t for t in TR if t.GetClass() == 'PCB_TRACK']
def via_at(x, y, tol=0.003):
    return [v for v in vias if abs(v.GetPosition().x / MM - x) <= tol and abs(v.GetPosition().y / MM - y) <= tol]
def n_tracks(v):
    p = v.GetPosition(); r = v.GetWidth() / MM / 2 + 0.01; n = 0
    for s in segs:
        if s.GetNetCode() != v.GetNetCode(): continue
        for q in (s.GetStart(), s.GetEnd()):
            if math.hypot(q.x / MM - p.x / MM, q.y / MM - p.y / MM) <= r: n += 1; break
    return n
gone = set(); report = []
for x in d['violations']:
    if x['type'] == 'holes_co_located':
        pos = x['items'][0]['pos']; vs = [v for v in via_at(pos['x'], pos['y']) if v.m_Uuid.AsString() not in gone]
        nets = {v.GetNetCode() for v in vs}
        if len(vs) > 1 and len(nets) == 1:
            for v in vs[1:]: gone.add(v.m_Uuid.AsString())
            report.append('duplicate: %s (%.3f,%.3f) removed %d' % (vs[0].GetNetname().split('/')[-1], pos['x'], pos['y'], len(vs) - 1))
        elif len(nets) > 1: report.append('DIFFERENT NETS at one point (%.3f,%.3f): %s' % (pos['x'], pos['y'], [v.GetNetname() for v in vs]))
for x in d['violations']:
    if x['type'] != 'hole_to_hole': continue
    it = x['items']; kinds = [i['description'].split(' ')[0] for i in it]
    vs = []
    for i in it:
        if i['description'].startswith('Via'): vs += [v for v in via_at(i['pos']['x'], i['pos']['y']) if v.m_Uuid.AsString() not in gone]
    if len(vs) == 2 and all(k == 'Via' for k in kinds):
        if vs[0].GetNetCode() != vs[1].GetNetCode(): report.append('DIFFERENT NETS too close: %s' % [i['description'][:40] for i in it]); continue
        vict = min(vs, key=n_tracks); gone.add(vict.m_Uuid.AsString())
        report.append('too close: %s removed via (%.3f,%.3f) with %d tracks' % (vict.GetNetname().split('/')[-1], vict.GetPosition().x / MM, vict.GetPosition().y / MM, n_tracks(vict)))
    elif len(vs) == 1 and 'PTH' in ' '.join(kinds + [i['description'] for i in it]):
        v = vs[0]; pad = [i for i in it if 'pad' in i['description']][0]
        if v.GetNetname().split('/')[-1] in pad['description']:
            gone.add(v.m_Uuid.AsString()); report.append('next to a same-net PTH: removed via %s (%.3f,%.3f)' % (v.GetNetname().split('/')[-1], v.GetPosition().x / MM, v.GetPosition().y / MM))
        else: report.append('via next to a PTH of another net: %s' % [i['description'][:40] for i in it])
    elif len(vs) == 0: pass   # already removed (pair of duplicates)
    else: report.append('not handled: %s' % [i['description'][:40] for i in it])
for r in report: print('  ' + r)
print('vias to remove: %d' % len(gone))
if a.apply and gone:
    for v in vias:
        if v.m_Uuid.AsString() in gone: b.Remove(v)
    pcbnew.SaveBoard(a.board, b); print('saved', a.board)
