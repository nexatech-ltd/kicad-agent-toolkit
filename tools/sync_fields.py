#!/usr/bin/env python3
"""Copy symbol fields (MPN, LCSC, Datasheet, ...) from the netlist into footprint fields. Clears the parity
findings "Missing symbol field 'MPN' in footprint" / "Field 'Datasheet' differs". Fields are created hidden on F.Fab.
  python3 tools/sync_fields.py --board B.kicad_pcb --netlist work/B.net [--fields MPN,LCSC,Datasheet] [--apply]"""
import sys, re, argparse, pcbnew
ap = argparse.ArgumentParser(); ap.add_argument('--board', required=True); ap.add_argument('--netlist', required=True)
ap.add_argument('--fields', default='MPN,LCSC,Datasheet'); ap.add_argument('--apply', action='store_true')
a = ap.parse_args(); want = a.fields.split(',')
s = open(a.netlist).read()
comps = {}
body = s.split('(libparts')[0]                      # only the (components ...) section
for part in re.split(r'\(comp\s*\(ref "', body)[1:]:   # "(comp" may sit on its own line
    ref = part.split('"', 1)[0]; f = {}
    for fm in re.finditer(r'\(field\s*\(name "([^"]+)"\)\s*(?:"((?:[^"\\]|\\.)*)")?\s*\)', part):
        f[fm.group(1)] = (fm.group(2) or '').replace('\\"', '"')
    comps[ref] = f
print('netlist components with fields: %d' % len(comps))
b = pcbnew.LoadBoard(a.board); MM = 1e6
FAB = b.GetLayerID('F.Fab'); changed = 0; created = 0; missing = 0
for fp in b.GetFootprints():
    ref = fp.GetReference(); f = comps.get(ref)
    if f is None: missing += 1; continue
    for name in want:
        if name not in f: continue
        val = f[name]
        cur = fp.GetFieldText(name) if fp.HasField(name) else None
        if cur == val: continue
        had = fp.HasField(name)
        fp.SetField(name, val)
        fld = fp.GetField(name)
        fld.SetVisible(False); fld.SetLayer(FAB)
        changed += 1; created += 0 if had else 1
print('footprints without a netlist component: %d; fields written: %d (new %d)' % (missing, changed, created))
if a.apply:
    pcbnew.SaveBoard(a.board, b); print('saved', a.board)
