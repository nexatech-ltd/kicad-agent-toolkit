#!/usr/bin/env python3
"""Decisive test of the decoupling check in verify.py: does it catch a missing path through
the plane?

Why. The check's criterion accepts ANY capacitor on the same net that has its own via to
the plane closer than `VIA_NEAR`, not just the capacitor nearest to the pin over the
surface (the nearest one can be a 100 nF part sitting 10 mm from its own via, while bulk
22 uF capacitors are stitched to the plane 1.2 mm away). Such a widening easily turns the
check into an always-green one, and that cannot be noticed on a good board: it passes
anyway. So the check is tested by mutation: break the board where the criterion must fire,
and require that it fires.

Cases:
  base         board as is                                           -> decoupling PASSED
  no-cap-vias  remove the vias of CAP_NET next to its capacitors     -> FAILED
  no-pin-vias  remove the vias of PIN_NET next to the pins of REFS   -> FAILED

Targets are board-specific: pass them as arguments or in kicad-agent.json:
  "test_decouple": {"cap_net": "VSYS", "pin_net": "+3V3", "pin_refs": ["U5", "U6"]}
A case without a target is skipped (and reported as skipped).

Run:  python3 tools/test_decouple.py [--board BOARD] [--cap-net NET] [--pin-net NET]
                                     [--pin-refs U1,U2] [--keep]
Takes a few minutes: verify.py runs on board copies in parallel.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

MM = 1e6
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import project

_cfg = project.get('test_decouple', {}) or {}
ap = argparse.ArgumentParser()
ap.add_argument('--board', default=paths.BOARD)
ap.add_argument('--cap-net', default=_cfg.get('cap_net'),
                help='net whose vias next to capacitors are removed (case no-cap-vias)')
ap.add_argument('--pin-net', default=_cfg.get('pin_net'),
                help='net whose vias next to the pins of --pin-refs are removed (case no-pin-vias)')
ap.add_argument('--pin-refs', default=','.join(_cfg.get('pin_refs', []) or []),
                help='comma-separated references for the no-pin-vias case')
ap.add_argument('--keep', action='store_true', help='keep the temporary directories')
ARGS = ap.parse_args()

BOARD = os.path.abspath(ARGS.board)
NAME = os.path.basename(BOARD)
SRC = os.path.dirname(BOARD)
KEEP = ARGS.keep
short = lambda s: s.split('/')[-1]


def prepare(tag):
    """Directory with the board and everything verify.py looks for next to it: project,
    rules, schematic sheets, project config, libraries."""
    d = os.path.join(tempfile.gettempdir(), 'test_decouple_' + tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    for name in os.listdir(SRC):
        if name.endswith(('.kicad_pro', '.kicad_dru', '.kicad_sch')) or name == 'kicad-agent.json':
            shutil.copy(os.path.join(SRC, name), d)
    if os.path.exists(paths.CONFIG) and not os.path.exists(os.path.join(d, 'kicad-agent.json')):
        shutil.copy(paths.CONFIG, os.path.join(d, 'kicad-agent.json'))
    if os.path.isdir(os.path.join(SRC, 'lib')):
        shutil.copytree(os.path.join(SRC, 'lib'), os.path.join(d, 'lib'))
    return d


# The mutation runs in a SEPARATE process: a second LoadBoard in the same process after
# SaveBoard returns an untyped SwigPyObject ("object has no attribute GetFootprints"). The
# same KiCad SWIG quirk is why edit tools keep one LoadBoard per run.
MUT_CODE = r"""
import sys, pcbnew
MM = 1e6
src, kind, dst, net, refs = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
refs = set(r for r in refs.split(',') if r)
short = lambda s: s.split('/')[-1]
b = pcbnew.LoadBoard(src)
killed = 0
if kind == 'no-cap-vias':
    caps = [(pd.GetPosition().x / MM, pd.GetPosition().y / MM)
            for fp in b.GetFootprints() if fp.GetReference().startswith('C')
            for pd in fp.Pads() if short(pd.GetNetname()) == net]
    for t in list(b.GetTracks()):
        if t.GetClass() != 'PCB_VIA' or short(t.GetNetname()) != net:
            continue
        p = t.GetPosition()
        if any(abs(p.x / MM - cx) < 2.0 and abs(p.y / MM - cy) < 2.0 for cx, cy in caps):
            b.Remove(t); killed += 1
elif kind == 'no-pin-vias':
    pins = []
    for fp in b.GetFootprints():
        if fp.GetReference() not in refs:
            continue
        for pd in fp.Pads():
            if short(pd.GetNetname()) == net:
                pins.append((pd.GetPosition().x / MM, pd.GetPosition().y / MM))
    for t in list(b.GetTracks()):
        if t.GetClass() != 'PCB_VIA' or short(t.GetNetname()) != net:
            continue
        p = t.GetPosition()
        if any(abs(p.x / MM - px) < 2.0 and abs(p.y / MM - py) < 2.0 for px, py in pins):
            b.Remove(t); killed += 1
pcbnew.SaveBoard(dst, b)
print(killed)
"""


def mutate(dst, kind, net='', refs=''):
    r = subprocess.run([sys.executable, '-c', MUT_CODE, BOARD, kind,
                        os.path.join(dst, NAME), net or '', refs or ''],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit('ERROR in mutation %s: %s' % (kind, r.stderr[-400:]))
    nums = [l for l in r.stdout.split() if l.isdigit()]
    return int(nums[-1]) if nums else 0


CASES = [('base', True, '', '')]
if ARGS.cap_net:
    CASES.append(('no-cap-vias', False, ARGS.cap_net, ''))
else:
    print('no-cap-vias: skipped, no --cap-net (config test_decouple.cap_net)')
if ARGS.pin_net and ARGS.pin_refs:
    CASES.append(('no-pin-vias', False, ARGS.pin_net, ARGS.pin_refs))
else:
    print('no-pin-vias: skipped, no --pin-net/--pin-refs (config test_decouple.pin_net/pin_refs)')
if len(CASES) == 1:
    raise SystemExit('NOTHING TO TEST: no mutation target given; base alone proves nothing')

runs = []
for tag, _expect_ok, net, refs in CASES:
    d = prepare(tag)
    n = mutate(d, tag, net, refs)
    print('%-11s directory %s, vias removed: %d' % (tag, d, n))
    env = dict(os.environ, KAT_ROOT=d, BOARD=os.path.join(d, NAME), KAT_WORK=os.path.join(d, 'work'))
    env.pop('KAT_CONFIG', None)
    runs.append((tag, d, subprocess.Popen([sys.executable, os.path.join(HERE, 'verify.py'),
                                           os.path.join(d, NAME)],
                                          cwd=d, env=env, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True)))

# Matches verify.py's check line: "  PASSED  decoupling: ..." / "  FAILED  decoupling: ..."
LINE = re.compile(r'(PASSED|FAILED)\s+decoupling')
fails = 0
print()
for (tag, expect_ok, _n, _r), (_t, d, proc) in zip(CASES, runs):
    out = proc.communicate()[0]
    m = LINE.search(out)
    if not m:
        print('%-11s ERROR: no decoupling line in the output: did the check run?' % tag)
        fails += 1
        continue
    ok = m.group(1) == 'PASSED'
    verdict = 'correct' if ok == expect_ok else 'WRONG'
    if ok != expect_ok:
        fails += 1
    detail = [l for l in out.splitlines() if 'decoupling' in l]
    print('%-11s expected %-7s got %-7s: %s'
          % (tag, 'PASSED' if expect_ok else 'FAILED', m.group(1), verdict))
    if detail:
        print('            %s' % detail[0].strip()[:150])
    if not KEEP:
        shutil.rmtree(d, ignore_errors=True)

print()
if fails:
    raise SystemExit('TEST FAILED: %d mismatches' % fails)
print('TEST PASSED: the decoupling check sees both the good board and every breakage')
