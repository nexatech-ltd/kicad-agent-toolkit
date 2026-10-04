#!/usr/bin/env python3
"""Free a trapped plane pad: rip the copper of the "wall" nets around it, connect the pad first, reroute the walls.

For each cut-off pad (list from `island_pads.py`): wall nets = all nets (except planes and protected nets) that have
copper within R_WALL of the pad on any layer; their copper within R_RIP is removed (a pcbnew subprocess, like ripup
but without routing), then `route.py --planes --any-target` (planes as signals) routes the pad to the nearest copper
of its net, then a normal `route.py` puts the walls back. DRC is the judge: a step is accepted if there are 0
violations, the pad is connected and the total number of breaks did not grow; otherwise it rolls back to the copy
taken before the step. "Plane net first" is the key ordering: when everything is rerouted together, the walls
come back before it.

Protected nets (never ripped) come from the `protect_nets` config key (e.g. an RF feed line).

    python3 tools/free_pad.py --board B [--nets GND,+3V3] [--r-wall 1.0] [--r-rip 1.3] [--apply]
"""
import argparse, json, math, os, shutil, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, project, drcinfo as D
PROTECT_CLASSES = set(project.get('protect_nets', []))
RIP_SRC = r"""
import sys, json, math, pcbnew
MM = 1e6
bp, x, y, r_wall, r_rip, protect = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]), set(json.loads(sys.argv[6]))
b = pcbnew.LoadBoard(bp)
def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1; L2 = dx*dx + dy*dy; t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px-x1)*dx + (py-y1)*dy) / L2))
    return math.hypot(px - (x1 + t*dx), py - (y1 + t*dy))
def dist(t):
    if t.GetClass() == 'PCB_VIA':
        p = t.GetPosition(); return math.hypot(p.x/MM - x, p.y/MM - y)
    s, e = t.GetStart(), t.GetEnd(); return dseg(x, y, s.x/MM, s.y/MM, e.x/MM, e.y/MM)
walls = set()
for t in b.GetTracks():
    n = t.GetNetname()
    if not n or n in protect or n.split('/')[-1] in protect: continue
    if dist(t) <= r_wall: walls.add(n)
rm = 0
for t in list(b.GetTracks()):
    if t.GetNetname() in walls and dist(t) <= r_rip:
        b.Remove(t); rm += 1
pcbnew.SaveBoard(bp, b)
print(json.dumps({'walls': sorted(walls), 'removed': rm}))
"""


def drc(board, out):
    d = D.run_drc(board, out); u, e, _ = D.score(d); return d, u, e


def cut_pads(board, nets):
    r = subprocess.run([sys.executable, os.path.join(HERE, 'island_pads.py'), '--board', board, '--nets', nets, '-o', paths.work('fp_pads.json')],
                       capture_output=True, text=True, timeout=900)
    return json.load(open(paths.work('fp_pads.json')))['unconnected_items']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD); ap.add_argument('--nets', default=','.join(sorted(project.plane_nets())))
    ap.add_argument('--r-wall', type=float, default=1.0); ap.add_argument('--r-rip', type=float, default=1.3)
    ap.add_argument('--apply', action='store_true'); ap.add_argument('--max', type=int, default=40)
    ap.add_argument('--rip-plane-tracks', action='store_true', help='also rip tracks/vias of plane nets around the end (fills are untouched); their pads are then reconnected by an island_pads pass')
    ap.add_argument('--from-drc', default=None, help='take breaks from this DRC report (signal nets and plane pads) instead of island_pads')
    a = ap.parse_args()
    board = os.path.abspath(a.board); work = os.path.dirname(paths.work('x'))
    protect = json.dumps(sorted((set() if a.rip_plane_tracks else D.PLANE_NETS) | PROTECT_CLASSES))
    d0, U, E = drc(board, paths.work('fp_drc0.json'))
    SIG0 = len([b_ for b_ in D.signal_breaks(d0) if b_[0].split('/')[-1] not in D.PLANE_NETS])
    print('start %d / %d' % (U, E), flush=True)
    if E: print('REFUSED: the board has violations'); return 1
    if a.from_drc:
        src = D.load(a.from_drc); items = []
        for x in src.get('unconnected_items', []):
            its = x.get('items', [])
            if len(its) != 2 or any('Zone' in it['description'] for it in its): continue
            net = D._net_of(its[0]) or ''
            if net.split('/')[-1] in D.PLANE_NETS:
                for it in its:
                    if it['description'].startswith(('Pad ', 'Via ')):
                        items.append({'description': x['description'], 'severity': 'error', 'type': 'unconnected_items', 'items': [it, dict(it)]}); break
            else:
                items.append(x)
    else:
        items = cut_pads(board, a.nets)
    print('cut-off pads: %d' % len(items), flush=True)
    done = 0
    for it in items[:a.max]:
        pad = it['items'][0]; x, y = pad['pos']['x'], pad['pos']['y']; net = D._net_of(pad); short = net.split('/')[-1]
        plane = short in D.PLANE_NETS
        if not plane:
            # for a signal break the trapped end is the one with less room; take both ends if they are close (< 4 mm)
            bx, by = it['items'][1]['pos']['x'], it['items'][1]['pos']['y']
        bak = board + '.bakFreePad'; shutil.copy2(board, bak)
        prot = json.dumps(sorted(set(json.loads(protect)) | {net}))
        r = subprocess.run([sys.executable, '-c', RIP_SRC, board, str(x), str(y), str(a.r_wall), str(a.r_rip), prot], capture_output=True, text=True, timeout=600)
        try: info = json.loads([l for l in r.stdout.splitlines() if l.startswith('{')][-1])
        except Exception: print('  %s (%.2f,%.2f): rip-up failed: %s' % (pad['description'][:30], x, y, r.stderr[-200:])); shutil.copy2(bak, board); continue
        walls = [w.split('/')[-1] for w in info['walls']]
        if not plane and math.hypot(bx - x, by - y) < 4.0:
            r2 = subprocess.run([sys.executable, '-c', RIP_SRC, board, str(bx), str(by), str(a.r_wall), str(a.r_rip), prot], capture_output=True, text=True, timeout=600)
            try:
                info2 = json.loads([l for l in r2.stdout.splitlines() if l.startswith('{')][-1]); walls = sorted(set(walls) | {w.split('/')[-1] for w in info2['walls']}); info['removed'] += info2['removed']
            except Exception: pass
        print('  %s (%.2f, %.2f): walls %s, removed %d' % (pad['description'][:34], x, y, walls, info['removed']), flush=True)
        one = paths.work('fp_one.json'); json.dump({'unconnected_items': [it], 'violations': [], 'schematic_parity': []}, open(one, 'w'))
        only = paths.work('fp_only.txt'); open(only, 'w').write(short + '\n')
        env = dict(os.environ, KAT_PLANE_AS_SIGNAL='1', KAT_MULTI_R='10', KAT_ROOT=paths.KAT_ROOT)
        args = ['--planes', '--any-target'] if plane else []
        r1 = subprocess.run([sys.executable, os.path.join(HERE, 'gpuroute', 'route.py'), '--board', board, '--drc', one] + args + ['--only-net', only, '--limit', '4', '--iters', '8', '--apply'],
                            capture_output=True, text=True, env=env if plane else dict(os.environ, KAT_ROOT=paths.KAT_ROOT), timeout=900)
        # route.py summary line: 'to write: breaks <done> of <total>, ...'
        ok1 = 'breaks 1 of 1' in r1.stdout
        d1, U1, E1 = drc(board, paths.work('fp_drc1.json'))
        if walls:
            wonly = paths.work('fp_walls.txt'); open(wonly, 'w').write('\n'.join(walls) + '\n')
            subprocess.run([sys.executable, os.path.join(HERE, 'gpuroute', 'route.py'), '--board', board, '--drc', paths.work('fp_drc1.json'), '--only-net', wonly, '--limit', '20', '--iters', '8', '--apply'], capture_output=True, text=True, env=dict(os.environ, KAT_ROOT=paths.KAT_ROOT), timeout=900)
        if a.rip_plane_tracks and any(w in D.PLANE_NETS for w in walls):
            pw = [w for w in walls if w in D.PLANE_NETS]
            subprocess.run([sys.executable, os.path.join(HERE, 'island_pads.py'), '--board', board, '--nets', ','.join(pw), '-o', paths.work('fp_ip.json')], capture_output=True, text=True, timeout=900)
            ponly = paths.work('fp_ponly.txt'); open(ponly, 'w').write('\n'.join(pw) + '\n')
            subprocess.run([sys.executable, os.path.join(HERE, 'gpuroute', 'route.py'), '--board', board, '--drc', paths.work('fp_ip.json'), '--planes', '--any-target', '--only-net', ponly, '--limit', '20', '--iters', '8', '--apply'], capture_output=True, text=True, env=env, timeout=900)
        d2, U2, E2 = drc(board, paths.work('fp_drc2.json'))
        sig2 = len([b_ for b_ in D.signal_breaks(d2) if b_[0].split('/')[-1] not in D.PLANE_NETS])
        if E2 == 0 and U2 <= U and ok1 and sig2 <= SIG0:
            print('    ACCEPTED: %d -> %d (connected: %s)' % (U, U2, ok1), flush=True); U = U2; SIG0 = sig2; done += 1
        else:
            print('    rollback: breaks %d -> %d, errors %d, pad connected %s' % (U, U2, E2, ok1), flush=True); shutil.copy2(bak, board)
    print('pads freed: %d; breaks %d' % (done, U))
    return 0


if __name__ == '__main__':
    sys.exit(main())
