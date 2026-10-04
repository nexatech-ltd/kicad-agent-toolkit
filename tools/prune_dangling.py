#!/usr/bin/env python3
"""Iteratively remove dangling segments/vias of the given nets inside a window -
by KiCad DRC's verdict (track_dangling, via_dangling), until DRC stops finding them.

    python3 tools/prune_dangling.py --nets +3V3 --box 198 106 232 134
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

ap = argparse.ArgumentParser()
ap.add_argument('--board', default=paths.BOARD)
ap.add_argument('--nets', required=True, help='comma-separated; * for all nets')
ap.add_argument('--box', nargs=4, type=float, required=True, metavar=('X1', 'Y1', 'X2', 'Y2'))
ap.add_argument('--max-iter', type=int, default=5)
a = ap.parse_args()
nets = set(a.nets.split(','))
x1, y1, x2, y2 = a.box
rep = paths.work('prune_dangling.json')
shutil.copy2(a.board, a.board + '.bakDangling')
total = 0
for it in range(a.max_iter):
    subprocess.run([paths.KICAD_CLI, 'pcb', 'drc', '--refill-zones',
                    '--severity-all', '--format', 'json', '-o', rep, a.board],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = json.load(open(rep))
    hits = []
    for v in d['violations']:
        if v['type'] not in ('track_dangling', 'via_dangling'):
            continue
        for i in v['items']:
            p = i['pos']
            desc = i['description']
            if x1 <= p['x'] <= x2 and y1 <= p['y'] <= y2 and ('*' in nets or any('[%s]' % n in desc for n in nets)):
                hits.append((i['uuid'], desc))
    print('iteration %d: DRC found %d in the window' % (it + 1, len(hits)))
    if not hits:
        break
    b = pcbnew.LoadBoard(a.board)
    byid = {t.m_Uuid.AsString(): t for t in b.GetTracks()}
    n = 0
    for uuid, desc in hits:
        item = byid.get(uuid)
        if item is None:
            print('   skipped: %s' % desc[:70])
            continue
        print('   removed: %s' % desc[:70])
        b.Remove(item)
        n += 1
    pcbnew.SaveBoard(a.board, b)
    total += n
    if n == 0:
        break
print('removed in total: %d (backup .bakDangling)' % total)
