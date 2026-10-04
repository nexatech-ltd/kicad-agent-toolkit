#!/usr/bin/env python3
"""Plane-net pads cut off from the main copper of their net -> synthetic Pad-to-Pad breaks for the router.

DRC reports cut-off fill fragments as "Zone <-> Zone", but the coordinates of those records are the zone
anchor, not the fragment location, so there is nothing to search by. Here the plane net connectivity is
computed with the router's raster model (`connectivity.components`: fill + pads + tracks + via columns,
26-connectivity); the largest component is the main one, and every pad of the net in another component
is cut off. A DRC-format report with Pad-to-Pad breaks (both positions = the pad itself) is written for
`route.py --planes --any-target` with `KAT_PLANE_AS_SIGNAL=1`: a track from the pad to the nearest copper
of the net (usually a via into the plane).

    python3 tools/island_pads.py --board B [--nets GND,+3V3] -o work/island_pads.json
    (default --nets: project.plane_nets())
"""
import argparse, collections, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, 'gpuroute'))
import paths, project, raster, maze_route as MR
import connectivity as CN
import pcbnew
MM = 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD); ap.add_argument('--nets', default=','.join(sorted(project.plane_nets())))
    ap.add_argument('-o', '--out', default=paths.work('island_pads.json'))
    a = ap.parse_args()
    b = pcbnew.LoadBoard(a.board); g = raster.Grid(b, step=MR.STEP)
    items = []
    for short in a.nets.split(','):
        net = next((p.GetNetname() for fp in b.GetFootprints() for p in fp.Pads() if p.GetNetname().split('/')[-1] == short), None)
        if not net:
            print('no such net', short); continue
        lab, n = CN.components(b, g, net)
        sizes = collections.Counter(lab[lab > 0].ravel().tolist())
        main_lab = sizes.most_common(1)[0][0]
        cut = []; comp_pads = {}; comp_vias = {}; comp_trk = {}
        for fp in b.GetFootprints():
            for p in fp.Pads():
                if p.GetNetname() != net: continue
                x, y = p.GetPosition().x / MM, p.GetPosition().y / MM
                k = CN.label_at(lab, g, x, y, reach=0.4)
                if k and k != main_lab:
                    cut.append((fp.GetReference(), p.GetNumber(), x, y, sizes[k])); comp_pads.setdefault(k, []).append((fp.GetReference(), p.GetNumber()))
        for t in b.GetTracks():
            if t.GetNetname() != net: continue
            if t.GetClass() == 'PCB_VIA':
                x, y = t.GetPosition().x / MM, t.GetPosition().y / MM; k = CN.label_at(lab, g, x, y, reach=0.4)
                if k and k != main_lab: comp_vias.setdefault(k, []).append((x, y))
            else:
                x, y = t.GetStart().x / MM, t.GetStart().y / MM; k = CN.label_at(lab, g, x, y, reach=0.2)
                if k and k != main_lab: comp_trk.setdefault(k, []).append((b.GetLayerName(t.GetLayer()), x, y))
        print('%-5s components %d, main %d cells; cut-off pads %d' % (short, n, sizes[main_lab], len(cut)))
        for r, num, x, y, sz in cut:
            print('   %s.%s (%.2f, %.2f) in a component of %d cells' % (r, num, x, y, sz))
            desc = 'Pad %s [%s] of %s on F.Cu' % (num, net, r)
            items.append({'description': 'Missing connection between items', 'severity': 'error', 'type': 'unconnected_items',
                          'items': [{'description': desc, 'pos': {'x': x, 'y': y}, 'uuid': ''}, {'description': desc, 'pos': {'x': x, 'y': y}, 'uuid': ''}]})
        # components without pads: with a via -> break from the via; tracks only -> dangling copper (remove); nothing -> fill island
        for k, sz in sizes.items():
            if k == main_lab or k in comp_pads: continue
            if comp_vias.get(k):
                x, y = comp_vias[k][0]; desc = 'Via [%s] on F.Cu - B.Cu' % net
                print('   component %d (%d cells) without pads: via (%.2f, %.2f) -> break from the via' % (k, sz, x, y))
                items.append({'description': 'Missing connection between items', 'severity': 'error', 'type': 'unconnected_items',
                              'items': [{'description': desc, 'pos': {'x': x, 'y': y}, 'uuid': ''}, {'description': desc, 'pos': {'x': x, 'y': y}, 'uuid': ''}]})
            elif comp_trk.get(k):
                print('   component %d (%d cells): tracks only, dangling copper: %s' % (k, sz, comp_trk[k][:4]))
            else:
                print('   component %d (%d cells): plain fill island' % (k, sz))
    json.dump({'unconnected_items': items, 'violations': [], 'schematic_parity': []}, open(a.out, 'w'), ensure_ascii=False)
    print('synthetic breaks: %d -> %s' % (len(items), a.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
