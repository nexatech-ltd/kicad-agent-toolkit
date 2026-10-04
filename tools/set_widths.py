#!/usr/bin/env python3
"""Set segment widths by uuid: python3 set_widths.py --board B --json {uuid: width_nm}. A separate process so
that SaveBoard is not mixed with text edits and LoadBoard is not called twice in one process."""
import argparse, json, pcbnew
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--json', required=True)
a = ap.parse_args(); want = json.load(open(a.json)); b = pcbnew.LoadBoard(a.board); n = 0
for t in b.GetTracks():
    u = t.m_Uuid.AsString()
    if u in want and t.GetWidth() != int(want[u]):
        t.SetWidth(int(want[u])); n += 1
pcbnew.SaveBoard(a.board, b); print('segments changed: %d' % n)
