#!/usr/bin/env python3
"""Unconnected plane pads: a via into the plane on a stub or next to the pad; DRC has the last word.

Breaks of plane nets (project.plane_nets()) of the Pad<->Track / Pad<->Pad kind: the pad has a piece of copper
(or none), but it does not reach the plane; the router (`route --planes`) sees the end as trapped, and
`fix_plane_pads` only handles bare pads with an outward escape. Here, for each such break, candidates are tried:
a VIA/DRILL via at the ends and midpoints of the net's segments within 1.2 mm of the break end (stub), and, for a
pad without copper, a via on a 0.6-1.0 mm ring around the pad centre with a 0.15 mm stub from the centre. Each
candidate is placed in a trial copy of the board and judged by kicad-cli DRC: the first one with 0 violations and
fewer breaks is accepted. One LoadBoard per process; the trial copy is written with SaveBoard to a separate dir.

    python3 tools/plane_via_stub.py --board B --drc D [--apply]
"""
import argparse
import math
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import drcinfo as D

import pcbnew

MM = 1e6
VIA, DRILL = 0.5, 0.2
RING = (0.6, 0.8, 1.0)
DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--max-cands', type=int, default=8)
    a = ap.parse_args()
    board = pcbnew.LoadBoard(a.board)
    drc0 = D.load(a.drc)
    U0, E0, _ = D.score(drc0)
    trial = paths.work('pvs_trial')
    os.makedirs(trial, exist_ok=True)
    bdir = os.path.dirname(os.path.abspath(a.board))
    stem = os.path.splitext(os.path.basename(a.board))[0]
    for ext in ('.kicad_pro', '.kicad_dru'):
        if os.path.exists(os.path.join(bdir, stem + ext)):
            shutil.copy2(os.path.join(bdir, stem + ext), os.path.join(trial, stem + ext))
    tb = os.path.join(trial, stem + '.kicad_pcb')
    # foreign copper for a rough ranking of candidates:
    # pads (centre + half size), vias (centre + radius), segments (distance to segment + half width)
    pads_f, vias_f, segs_f = [], [], []
    for fp in board.GetFootprints():
        for p in fp.Pads():
            sz = p.GetSize(p.GetPrincipalLayer()) if hasattr(p, 'GetPrincipalLayer') else p.GetSize()
            pads_f.append((p.GetPosition().x / MM, p.GetPosition().y / MM, max(sz.x, sz.y) / 2 / MM, p.GetNetname()))
    for t in board.GetTracks():
        if t.GetClass() == 'PCB_VIA':
            vias_f.append((t.GetPosition().x / MM, t.GetPosition().y / MM, t.GetWidth() / 2 / MM, t.GetNetname()))
        elif t.GetClass() == 'PCB_TRACK':
            s_, e_ = t.GetStart(), t.GetEnd()
            segs_f.append((s_.x / MM, s_.y / MM, e_.x / MM, e_.y / MM, t.GetWidth() / 2 / MM, t.GetNetname()))
    def d_seg(px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1; L2 = dx * dx + dy * dy
        tt = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
        return math.hypot(px - (x1 + tt * dx), py - (y1 + tt * dy))
    def room(x, y, net):
        # clearance from the via edge (VIA/2) to the edge of foreign copper (rough, for ranking)
        r = 9.0
        for fx, fy, hr, fn in pads_f:
            if fn != net: r = min(r, math.hypot(fx - x, fy - y) - hr)
        for fx, fy, vr, fn in vias_f:
            if fn != net: r = min(r, math.hypot(fx - x, fy - y) - vr)
        for x1, y1, x2, y2, hw, fn in segs_f:
            if fn != net and abs(x1 - x) < 3 and abs(y1 - y) < 3: r = min(r, d_seg(x, y, x1, y1, x2, y2) - hw)
        return r - VIA / 2
    jobs = []
    for b in D.breaks(drc0):
        net, kinds, dist, refs, pos = b
        if net.split('/')[-1] not in D.PLANE_NETS or 'Zone' in kinds or len(pos) < 2:
            continue
        for k, (x, y) in zip(sorted(kinds), pos):
            jobs.append((net, k, x, y))
    # kinds are sorted while positions follow the items order: rebuild the mapping from the descriptions
    jobs = []
    for x in drc0['unconnected_items']:
        its = x.get('items', [])
        if len(its) != 2:
            continue
        net = D._net_of(its[0]) or ''
        if net.split('/')[-1] not in D.PLANE_NETS:
            continue
        if any('Zone' in it['description'] for it in its):
            continue
        for it in its:
            jobs.append((net, D._kind_of(it), it['pos']['x'], it['pos']['y'], it['description'][:60]))
    pairs = []
    for x in drc0['unconnected_items']:
        its = x.get('items', [])
        if len(its) != 2:
            continue
        net = D._net_of(its[0]) or ''
        if net.split('/')[-1] not in D.PLANE_NETS or any('Zone' in it['description'] for it in its):
            continue
        (x1, y1), (x2, y2) = (its[0]['pos']['x'], its[0]['pos']['y']), (its[1]['pos']['x'], its[1]['pos']['y'])
        if math.hypot(x2 - x1, y2 - y1) <= 3.0:
            pairs.append((net, (x1, y1), (x2, y2), its[0]['description'][:40] + ' <-> ' + its[1]['description'][:40]))
    print('plane breaks (pads/stubs): %d ends, short pairs %d; start %d / %d' % (len(jobs), len(pairs), U0, E0))
    U, E = U0, E0
    accepted = []
    done_nets = set()
    layer_f = board.GetLayerID('F.Cu')
    def try_items(mk, label):
        nonlocal U, E
        added = mk()
        pcbnew.SaveBoard(tb, board)
        d2 = D.run_drc(tb, os.path.join(trial, 'drc.json'))
        U2, E2, _ = D.score(d2)
        if E2 == 0 and U2 < U:
            print('    ACCEPTED %s: %d -> %d' % (label, U, U2), flush=True)
            U, E = U2, E2; accepted.append(label); return True
        for it in added:
            board.Remove(it)
        return False
    def seg(a_, b_, w=0.2):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(round(a_[0] * MM)), int(round(a_[1] * MM))))
        t.SetEnd(pcbnew.VECTOR2I(int(round(b_[0] * MM)), int(round(b_[1] * MM))))
        t.SetWidth(int(w * MM)); t.SetLayer(layer_f); return t
    # first: direct F.Cu jumpers between the two ends (for plane pads these are usually two pads of the same IC)
    for net, p1, p2, desc in pairs:
        ok = False
        for path in ((p1, p2), (p1, (p1[0], p2[1]), p2), (p1, (p2[0], p1[1]), p2)):
            for w in (0.2, 0.15):
                def mk(path=path, w=w):
                    out = []
                    for a_, b_ in zip(path, path[1:]):
                        t = seg(a_, b_, w); t.SetNet(board.FindNet(net)); board.Add(t); out.append(t)
                    return out
                if try_items(mk, 'jumper %s w%.2f %s' % (net.split('/')[-1], w, '->'.join('(%.2f,%.2f)' % q for q in path))):
                    ok = True; break
            if ok:
                break
        if not ok:
            print('  jumper rejected: %s' % desc)
        else:
            done_nets.add((net, round(p1[0], 1), round(p1[1], 1))); done_nets.add((net, round(p2[0], 1), round(p2[1], 1)))
    for net, kind, x, y, desc in jobs:
        key = (net, round(x, 1), round(y, 1))
        if key in done_nets:
            continue
        cands = []
        if kind == 'Track':
            for t in board.GetTracks():
                if t.GetClass() != 'PCB_TRACK' or t.GetNetname() != net:
                    continue
                s, e = t.GetStart(), t.GetEnd()
                pts = [(s.x / MM, s.y / MM), (e.x / MM, e.y / MM), ((s.x + e.x) / 2 / MM, (s.y + e.y) / 2 / MM)]
                if min(math.hypot(px - x, py - y) for px, py in pts) <= 1.2:
                    for px, py in pts:
                        cands.append((px, py, None, t.GetLayer()))
        else:  # Pad: via on a ring with a stub from the pad centre
            for r in RING:
                for dx, dy in DIRS:
                    n = math.hypot(dx, dy)
                    cands.append((x + dx / n * r, y + dy / n * r, (x, y), layer_f))
        # candidate grid in a +-1.5 mm window: via + stub from the break end (for a pad, from its centre)
        for gx in range(-15, 16):
            for gy in range(-15, 16):
                cx_, cy_ = x + gx * 0.1, y + gy * 0.1
                if 0.45 <= math.hypot(gx * 0.1, gy * 0.1) <= 1.5:
                    cands.append((cx_, cy_, (x, y), layer_f))
        cands = [(round(c[0], 3), round(c[1], 3), c[2], c[3]) for c in cands]
        cands = sorted({c for c in cands if room(c[0], c[1], net) >= 0.15}, key=lambda c: (-room(c[0], c[1], net), math.hypot(c[0] - x, c[1] - y)))[:a.max_cands]
        print('  %-28s %-5s (%.2f, %.2f): candidates %d' % (net.split('/')[-1], kind, x, y, len(cands)), flush=True)
        ok = False
        for cx, cy, stub, layer in cands:
            added = []
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(pcbnew.VECTOR2I(int(round(cx * MM)), int(round(cy * MM))))
            v.SetViaType(pcbnew.VIATYPE_THROUGH); v.SetWidth(int(VIA * MM)); v.SetDrill(int(DRILL * MM))
            v.SetNet(board.FindNet(net)); board.Add(v); added.append(v)
            if stub:
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(int(round(stub[0] * MM)), int(round(stub[1] * MM))))
                t.SetEnd(pcbnew.VECTOR2I(int(round(cx * MM)), int(round(cy * MM))))
                t.SetWidth(int(0.15 * MM)); t.SetLayer(layer); t.SetNet(board.FindNet(net)); board.Add(t); added.append(t)
            pcbnew.SaveBoard(tb, board)
            d2 = D.run_drc(tb, os.path.join(trial, 'drc.json'))
            U2, E2, _ = D.score(d2)
            if E2 == 0 and U2 < U:
                print('    ACCEPTED via (%.3f, %.3f)%s: %d -> %d' % (cx, cy, ' + stub' if stub else '', U, U2), flush=True)
                U, E = U2, E2; accepted.append((net, cx, cy)); ok = True
                break
            why = next((v['type'] + ': ' + ' ; '.join(i['description'][:45] for i in v['items']) for v in d2.get('violations', []) if v.get('severity') == 'error'), 'breaks %d' % U2)
            print('    - via (%.2f, %.2f): %s' % (cx, cy, why[:110]), flush=True)
            for it in added:
                board.Remove(it)
        if not ok:
            print('    no room found: %s' % desc)
        else:
            done_nets.add(key)
    print('edits accepted: %d; breaks %d -> %d' % (len(accepted), U0, U))
    if a.apply and accepted:
        shutil.copy2(a.board, a.board + '.bakPVS')
        pcbnew.SaveBoard(a.board, board)
        print('written', a.board)
    return 0


if __name__ == '__main__':
    sys.exit(main())
