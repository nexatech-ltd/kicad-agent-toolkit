#!/usr/bin/env python3
"""Close micro-gaps that the router's raster model cannot see.

Raster connectivity (`connectivity.components`, 26-connectivity at a 0.1 mm step) treats two copper
pieces of one net whose edges are closer than a cell as ONE component - while DRC sees a break. Such
breaks (e.g. a 0.2 mm gap between two track ends, or a track ending 0.18 mm short of a pad) survive
every routing campaign: the router does not even try them. Here, from the DRC report, for each break
between two objects on the same layer (track <-> track, track <-> via, track <-> pad) closer than
MAX_GAP a segment is laid between the nearest points (to a pad - into its centre), width = the smaller
of the two (for a pad - the track width). DRC judges: segments that end up in violations are removed
(`--undo` with the report).

    python3 tools/close_gaps.py --board B --drc D [--max-gap 0.8] [--apply]
    python3 tools/close_gaps.py --board B --undo D2 --added work/close_gaps_added.json --apply
"""
import argparse
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pcbnew

import paths

MM = 1e6


def seg_seg_nearest(a1, a2, b1, b2):
    """Nearest points of two segments (in the plane) -> (p, q, dist)."""
    def clamp(v):
        return max(0.0, min(1.0, v))
    ax, ay = a2[0] - a1[0], a2[1] - a1[1]
    bx, by = b2[0] - b1[0], b2[1] - b1[1]
    best = None
    # enumeration: endpoints of one against the other segment (good enough in practice)
    def pt_seg(p, s1, s2):
        dx, dy = s2[0] - s1[0], s2[1] - s1[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else clamp(((p[0] - s1[0]) * dx + (p[1] - s1[1]) * dy) / L2)
        q = (s1[0] + t * dx, s1[1] + t * dy)
        return q, math.hypot(p[0] - q[0], p[1] - q[1])
    for p in (a1, a2):
        q, d = pt_seg(p, b1, b2)
        if best is None or d < best[2]:
            best = (p, q, d)
    for q in (b1, b2):
        p, d = pt_seg(q, a1, a2)
        if d < best[2]:
            best = (p, q, d)
    return best


def geom(obj, board):
    """(kind, set of layers, data)."""
    cls = obj.GetClass()
    if cls == 'PCB_VIA':
        p = obj.GetPosition()
        return 'via', None, dict(pos=(p.x / MM, p.y / MM), w=obj.GetWidth() / MM)
    if cls in ('PCB_TRACK', 'PCB_ARC'):
        s, e = obj.GetStart(), obj.GetEnd()
        return 'track', {obj.GetLayer()}, dict(a=(s.x / MM, s.y / MM), b=(e.x / MM, e.y / MM), w=obj.GetWidth() / MM)
    if cls == 'PAD':
        p = obj.GetPosition()
        lays = {l for l in obj.GetLayerSet().Seq() if board.GetLayerName(l) and pcbnew.IsCopperLayer(l)}
        return 'pad', lays, dict(pos=(p.x / MM, p.y / MM))
    return None, None, None


def find_by_uuid(board, uuids):
    out = {}
    for t in board.GetTracks():
        u = t.m_Uuid.AsString()
        if u in uuids:
            out[u] = t
    for fp in board.GetFootprints():
        for p in fp.Pads():
            u = p.m_Uuid.AsString()
            if u in uuids:
                out[u] = p
    return out


def propose(board, drc, max_gap):
    want = set()
    for x in drc.get('unconnected_items', []):
        for it in x.get('items', []):
            want.add(it['uuid'])
    objs = find_by_uuid(board, want)
    props = []
    for x in drc.get('unconnected_items', []):
        its = x.get('items', [])
        if len(its) != 2:
            continue
        A, B = objs.get(its[0]['uuid']), objs.get(its[1]['uuid'])
        if A is None or B is None:
            continue
        ka, la, ga = geom(A, board)
        kb, lb, gb = geom(B, board)
        if ka is None or kb is None or ka == kb == 'pad' or ka == kb == 'via':
            continue
        # a track must be on at least one side
        if 'track' not in (ka, kb):
            continue
        if kb == 'track' and ka != 'track':
            (ka, la, ga), (kb, lb, gb), A, B = (kb, lb, gb), (ka, la, ga), B, A
        layer = next(iter(la))
        if kb == 'track':
            if layer not in lb:
                continue
            p, q, d = seg_seg_nearest(ga['a'], ga['b'], gb['a'], gb['b'])
            w = min(ga['w'], gb['w'])
        elif kb == 'via':
            q = gb['pos']
            p, d = seg_seg_nearest(ga['a'], ga['b'], q, q)[0], None
            d = math.hypot(p[0] - q[0], p[1] - q[1])
            w = ga['w']
        else:  # pad
            if lb and layer not in lb:
                continue
            q = gb['pos']
            p = seg_seg_nearest(ga['a'], ga['b'], q, q)[0]
            d = math.hypot(p[0] - q[0], p[1] - q[1])
            w = min(ga['w'], 0.15)
        if d <= 1e-6 or d > max_gap:
            continue
        net = A.GetNetname()
        props.append(dict(net=net, layer=board.GetLayerName(layer), lid=layer, p=p, q=q, d=d, w=w,
                          kinds=(ka, kb), desc='%s ↔ %s' % (its[0]['description'][:40], its[1]['description'][:40])))
    return props


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=None)
    ap.add_argument('--max-gap', type=float, default=0.8)
    ap.add_argument('--added', default=paths.work('close_gaps_added.json'))
    ap.add_argument('--undo', default=None, help='DRC report after closing: remove added segments that ended up in violations')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    board = pcbnew.LoadBoard(a.board)
    if a.undo:
        d2 = json.load(open(a.undo))
        added = json.load(open(a.added))
        bad = set()
        for v in d2.get('violations', []):
            if v.get('severity') != 'error':
                continue
            us = {i['uuid'] for i in v.get('items', [])}
            bad |= us & set(added)
        n = 0
        for t in list(board.GetTracks()):
            if t.m_Uuid.AsString() in bad:
                board.Remove(t); n += 1
        print('segments removed for violations: %d of %d added' % (n, len(added)))
        if a.apply and n:
            pcbnew.SaveBoard(a.board, board)
            json.dump([u for u in added if u not in bad], open(a.added, 'w'))
        return 0
    drc = json.load(open(a.drc))
    props = propose(board, drc, a.max_gap)
    print('micro-gaps <= %.2f mm: %d' % (a.max_gap, len(props)))
    added = []
    for pr in props:
        print('  %-28s %-5s %.3f mm w%.2f  (%.3f,%.3f)->(%.3f,%.3f)  %s' % (pr['net'].split('/')[-1], pr['layer'], pr['d'], pr['w'],
                                                                      *pr['p'], *pr['q'], pr['desc']))
        if a.apply:
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(int(round(pr['p'][0] * MM)), int(round(pr['p'][1] * MM))))
            t.SetEnd(pcbnew.VECTOR2I(int(round(pr['q'][0] * MM)), int(round(pr['q'][1] * MM))))
            t.SetWidth(int(round(pr['w'] * MM)))
            t.SetLayer(pr['lid'])
            t.SetNet(board.FindNet(pr['net']))
            board.Add(t)
            added.append(t.m_Uuid.AsString())
    if a.apply and added:
        pcbnew.SaveBoard(a.board, board)
        json.dump(added, open(a.added, 'w'))
        print('written %d segments, list %s' % (len(added), a.added))
    return 0


if __name__ == '__main__':
    sys.exit(main())
