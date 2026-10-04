#!/usr/bin/env python3
"""Nudge a small part through a list of offsets so that its locked pad gets connected; DRC is the judge.

For each offset: board copy -> footprint to the new position (pcbnew) -> remove stubs of its nets near the old
pads (radius 1.2 mm) -> route its nets (`route.py --planes --any-target`, planes routed as signals) -> DRC.
The first offset with zero violations, no breaks on the part's nets and no growth of total breaks is accepted.

    python3 tools/nudge_part.py --board B --drc D --ref C1 [--offsets "0,0.5 0,-0.5 0.5,0 ..."] [--apply]
"""
import argparse, json, math, os, shutil, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, drcinfo as D
import pcbnew
MM = 1e6
DEFAULT_OFFSETS = "0,0.5 0,-0.5 0.5,0 -0.5,0 0.5,0.5 -0.5,0.5 0.5,-0.5 -0.5,-0.5 0,1.0 0,-1.0 1.0,0 -1.0,0"


def part_info(board_path, ref):
    b = pcbnew.LoadBoard(board_path)
    fp = next(f for f in b.GetFootprints() if f.GetReference() == ref)
    pads = [(p.GetNetname(), p.GetPosition().x / MM, p.GetPosition().y / MM) for p in fp.Pads() if p.GetNetname()]
    return (fp.GetPosition().x / MM, fp.GetPosition().y / MM), pads


MOVE_SRC = r"""
import sys, json, math, pcbnew
MM = 1e6
board_path, ref, dx, dy, out_path, pads = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), sys.argv[5], json.loads(sys.argv[6])
b = pcbnew.LoadBoard(board_path)
fp = next(f for f in b.GetFootprints() if f.GetReference() == ref)
p = fp.GetPosition(); fp.SetPosition(pcbnew.VECTOR2I(int(p.x + dx * MM), int(p.y + dy * MM)))
nets = {n for n, _, _ in pads}; n_rm = 0
for t in list(b.GetTracks()):
    if t.GetNetname() not in nets: continue
    if t.GetClass() == 'PCB_VIA':
        q = t.GetPosition(); pos = [(q.x / MM, q.y / MM)]
    else:
        s, e = t.GetStart(), t.GetEnd(); pos = [(s.x / MM, s.y / MM), (e.x / MM, e.y / MM)]
    if any(math.hypot(x - px, y - py) <= 1.2 for (x, y) in pos for _, px, py in pads):
        b.Remove(t); n_rm += 1
pcbnew.SaveBoard(out_path, b); print(n_rm)
"""


def move_and_strip(board_path, ref, dx, dy, pads, out_path):
    # separate process: a second LoadBoard in the same process (KiCad 10 SWIG) returns an unusable object
    r = subprocess.run([sys.executable, '-c', MOVE_SRC, board_path, ref, str(dx), str(dy), out_path, json.dumps(pads)],
                       capture_output=True, text=True, timeout=300)
    lines = [l for l in r.stdout.splitlines() if l.strip().isdigit()]
    return int(lines[-1]) if lines else -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD); ap.add_argument('--drc', default=paths.work('cur_drc.json'))
    ap.add_argument('--ref', required=True); ap.add_argument('--offsets', default=DEFAULT_OFFSETS)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    (cx, cy), pads = part_info(a.board, a.ref)
    nets = sorted({n for n, _, _ in pads}); short = [n.split('/')[-1] for n in nets]
    base = D.load(a.drc); U0, E0, _ = D.score(base)
    trial = paths.work('nudge_' + a.ref); os.makedirs(trial, exist_ok=True)
    stem = os.path.splitext(os.path.basename(a.board))[0]
    for ext in ('.kicad_pro', '.kicad_dru'):
        if os.path.exists(paths.sibling(a.board, ext)):
            shutil.copy2(paths.sibling(a.board, ext), os.path.join(trial, stem + ext))
    tb = os.path.join(trial, stem + '.kicad_pcb'); only = os.path.join(trial, 'only.txt'); open(only, 'w').write('\n'.join(short) + '\n')
    print('%s at (%.2f, %.2f), nets %s; start %d / %d' % (a.ref, cx, cy, short, U0, E0), flush=True)
    for off in a.offsets.split():
        dx, dy = (float(v) for v in off.split(','))
        n_rm = move_and_strip(a.board, a.ref, dx, dy, pads, tb)
        d1 = D.run_drc(tb, os.path.join(trial, 'drc1.json')); u1, e1, _ = D.score(d1)
        env = dict(os.environ, KAT_PLANE_AS_SIGNAL='1', KAT_ROOT=paths.KAT_ROOT)
        r = subprocess.run([sys.executable, os.path.join(HERE, 'gpuroute', 'route.py'), '--board', tb, '--drc', os.path.join(trial, 'drc1.json'),
                            '--planes', '--any-target', '--only-net', only, '--limit', '8', '--iters', '8', '--apply'],
                           capture_output=True, text=True, env=env, timeout=900)
        d2 = D.run_drc(tb, os.path.join(trial, 'drc2.json')); u2, e2, _ = D.score(d2)
        left = [b_[0].split('/')[-1] for b_ in D.signal_breaks(d2) if b_[0] in nets or b_[0].split('/')[-1] in short]
        errs = [v['type'] for v in d2.get('violations', []) if v.get('severity') == 'error']
        ok = e2 == 0 and not left and u2 <= U0
        print('  offset (%+.1f, %+.1f): stubs removed %d, after routing %d / %d, part breaks %d %s%s' % (dx, dy, n_rm, u2, e2, len(left), errs[:2], '  <- ACCEPTED' if ok else ''), flush=True)
        if ok:
            if a.apply:
                shutil.copy2(a.board, a.board + '.bakNudge'); shutil.copy2(tb, a.board); print('written', a.board)
            return 0
    print('no suitable offset'); return 1


if __name__ == '__main__':
    sys.exit(main())
