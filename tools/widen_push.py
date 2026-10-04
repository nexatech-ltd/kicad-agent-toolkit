#!/usr/bin/env python3
"""Bring the copper of the given net classes up to class width by clearing neighbours: widen everything possible,
then, guided by the DRC report, rip up blocking signal tracks, re-route them, and roll back unresolvable widenings.
  python3 tools/widen_push.py --board B --classes MOTOR [--rounds 4] [--apply]
One round: (1) class width for every narrow segment (except necks <= keep near their own pads/vias);
(2) DRC; (3) conflict "our widened <-> foreign signal track" -> remove the neighbour's segment, queue its net for
re-routing; conflict with a pad/drill/plane/no-touch class or "ours <-> ours" -> roll back our shorter segment;
(4) re-route the removed nets (route.py --any-target, KAT_ANY_SIGNAL=1); (5) DRC: errors -> roll back the culprits.
DRC is the judge: a round is accepted only with 0 errors and no more unconnected items than at the start."""
import os, sys, re, json, math, argparse, subprocess, collections, shutil
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import pcbnew, drcinfo as D, maze_route as MR
import fnmatch
import paths, project

MM = 1e6
ap = argparse.ArgumentParser()
ap.add_argument('--board', required=True); ap.add_argument('--classes', required=True, help='comma-separated net classes to widen')
ap.add_argument('--keep', type=float, default=3.0); ap.add_argument('--rounds', type=int, default=4)
ap.add_argument('--pro', default=None, help='.kicad_pro (default: next to the board)')
ap.add_argument('--apply', action='store_true')
a = ap.parse_args()
if not a.pro:
    a.pro = paths.sibling(a.board, '.kicad_pro')
WANT = set(a.classes.split(','))
ns = json.load(open(a.pro))['net_settings']
WCLS = {c['name']: float(c['track_width']) for c in ns['classes']}
PATS = [(x['netclass'], x['pattern']) for x in ns.get('netclass_patterns', [])]
PLANES = project.plane_nets()
NO_TOUCH_CLS = set(project.get('no_touch_classes', []))   # e.g. an RF feed class: never re-routed


def cls_of(net):
    base = net.split('/')[-1]
    for c, p in PATS:
        if fnmatch.fnmatch(base, p) or fnmatch.fnmatch(net, p): return c
    return 'Default'


def run_drc(board, out):
    subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones', '--severity-all', '--format', 'json', '-o', out, board],
                   capture_output=True)
    return D.load(out)


def dseg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1; L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def load(board):
    b = pcbnew.LoadBoard(board)
    segs = [t for t in b.GetTracks() if t.GetClass() == 'PCB_TRACK']
    return b, segs


def narrow_uuids(board):
    """{uuid: (old width, target)} - narrow segments of the wanted classes outside necks near their own pads."""
    b, segs = load(board)
    padc, viac = collections.defaultdict(list), collections.defaultdict(list)
    for fp in b.GetFootprints():
        for pd in fp.Pads():
            if pd.GetNetname(): padc[pd.GetNetname().split('/')[-1]].append((pd.GetPosition().x / MM, pd.GetPosition().y / MM))
    for t in b.GetTracks():
        if t.GetClass() == 'PCB_VIA' and t.GetNetname(): viac[t.GetNetname().split('/')[-1]].append((t.GetPosition().x / MM, t.GetPosition().y / MM))
    out = {}
    for t in segs:
        net = t.GetNetname()
        if not net: continue
        c = cls_of(net)
        if c not in WANT: continue
        need = WCLS.get(c, 0.2); w = t.GetWidth() / MM
        if w >= need - 0.005: continue
        base = net.split('/')[-1]
        s, e = t.GetStart(), t.GetEnd(); L = math.hypot((e.x - s.x) / MM, (e.y - s.y) / MM)
        pts = padc.get(base, []) + viac.get(base, [])
        if pts and L <= a.keep:
            d1 = min(math.hypot(px - s.x / MM, py - s.y / MM) for px, py in pts)
            d2 = min(math.hypot(px - e.x / MM, py - e.y / MM) for px, py in pts)
            if max(d1, d2) < a.keep: continue
        out[t.m_Uuid.AsString()] = (round(w, 3), need)
    return out


def set_widths(board, spec):
    if not spec: return
    tmp = board + '.push.json'; json.dump(spec, open(tmp, 'w'))
    subprocess.run([sys.executable, os.path.join(HERE, 'set_widths.py'), '--board', board, '--json', tmp], capture_output=True)


def conflicts(drc):
    """[(violation, [(type, net, position)...])], errors only."""
    out = []
    for x in drc['violations']:
        if x['severity'] != 'error': continue
        items = []
        for i in x['items']:
            kind = i['description'].split()[0]
            m = re.search(r'\[([^\]]+)\]', i['description'])
            items.append((kind, (m.group(1) if m else ''), (i['pos']['x'], i['pos']['y'])))
        out.append((x['type'], items))
    return out


base_drc = run_drc(a.board, a.board + '.push0.json')
U0, E0, _ = D.score(base_drc)
print('initial: unconnected %d, errors %d' % (U0, E0))
if E0:
    sys.exit('the input already has errors - bring the board to zero first')

def nets_of(cand):
    b, segs = load(a.board); byu = {t.m_Uuid.AsString(): t for t in segs}
    out = collections.defaultdict(list)
    for u_ in cand:
        if u_ in byu: out[byu[u_].GetNetname()].append(u_)
    return out

cand0 = narrow_uuids(a.board)
groups = nets_of(cand0)
print('nets with narrow copper: %d (segments %d)' % (len(groups), len(cand0)))
BAK = a.board + '.pushbak'
taken, failed = [], []
for net in sorted(groups, key=lambda n: len(groups[n])):   # simple nets first: early gains are locked in
    shutil.copy(a.board, BAK)
    cand = {u_: v for u_, v in narrow_uuids(a.board).items() if u_ in set(groups[net])}
    if not cand: continue
    set_widths(a.board, {u_: int(v[1] * MM) for u_, v in cand.items()})
    ok = False
    for it in range(1, 4):
        d = run_drc(a.board, a.board + '.pn.json')
        u, e, _ = D.score(d)
        if e == 0 and u <= U0:
            ok = True; break
        b, segs = load(a.board); byu = {t.m_Uuid.AsString(): t for t in segs}
        ours = {u_: byu[u_] for u_ in cand if u_ in byu}
        rollback, strip_segs, strip_nets = set(), [], set()
        for typ, items in conflicts(d):
            our_here, foreign = [], []
            for kind, n, pos in items:
                hit = None
                for u_, t in ours.items():
                    s_, e_ = t.GetStart(), t.GetEnd()
                    if t.GetNetname() == n and dseg(pos[0], pos[1], s_.x / MM, s_.y / MM, e_.x / MM, e_.y / MM) < t.GetWidth() / MM / 2 + 0.05:
                        hit = u_; break
                (our_here if hit else foreign).append((kind, n, pos, hit))
            if not our_here:
                rollback |= set(cand)          # the error is not ours - this pass breaks something else
                continue
            hard = any(k != 'Track' or n.split('/')[-1] in PLANES or cls_of(n) in NO_TOUCH_CLS for k, n, p, h in foreign)
            if hard or not foreign:
                for k, n, p, h in our_here: rollback.add(h)
                continue
            for k, n, p, h in foreign:
                strip_nets.add(n.split('/')[-1])   # remove the neighbour's WHOLE run: a stub cut from the middle cannot be re-routed
        if rollback:
            set_widths(a.board, {u_: int(cand[u_][0] * MM) for u_ in rollback if u_ in cand})
            for u_ in rollback: cand.pop(u_, None)
            if not cand: break
        if strip_nets:
            ops = [{'op': 'del_net_run', 'net': n, 'keep': 3.0} for n in sorted(strip_nets)]
            json.dump(ops, open(a.board + '.strip.json', 'w'))
            subprocess.run([sys.executable, os.path.join(HERE, 'util', 'edit_ops.py'), a.board, a.board + '.strip.json', '--apply'], capture_output=True)
            dd = run_drc(a.board, a.board + '.pnb.json')
            only = a.board + '.nets.txt'; open(only, 'w').write('\n'.join(sorted(strip_nets)) + '\n')
            env = dict(os.environ, KAT_ANY_SIGNAL='1', KAT_MULTI_R='16', KAT_ROOT=paths.KAT_ROOT)
            for k in range(3):
                subprocess.run([sys.executable, os.path.join(HERE, 'gpuroute', 'route.py'), '--board', a.board,
                                '--drc', a.board + '.pnb.json', '--any-target', '--only-net', only,
                                '--limit', '40', '--iters', '30', '--apply'], capture_output=True, env=env)
                dd = run_drc(a.board, a.board + '.pnb.json')
                if D.score(dd)[0] <= U0 and D.score(dd)[1] == 0: break
    d = run_drc(a.board, a.board + '.pn.json'); u, e, _ = D.score(d)
    if e == 0 and u <= U0 and cand:
        taken.append((net.split('/')[-1], len(cand))); print('  ACCEPTED %-12s segments %d (errors %d, unconnected %d)' % (net.split('/')[-1], len(cand), e, u), flush=True)
    else:
        failed.append(net.split('/')[-1]); shutil.copy(BAK, a.board); print('  rollback %-12s (errors %d, unconnected %d)' % (net.split('/')[-1], e, u), flush=True)

d = run_drc(a.board, a.board + '.pushfinal.json'); u, e, w = D.score(d)
print('result: unconnected %d, errors %d, warnings %d' % (u, e, w))
left = narrow_uuids(a.board)
print('nets accepted %d, rolled back %d; narrow segments left: %d' % (len(taken), len(failed), len(left)))
if not a.apply:
    print('(--apply not given: edits are already written to the given copy; the live board is not touched)')
