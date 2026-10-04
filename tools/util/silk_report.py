"""Triage of silkscreen DRC findings: what hinders assembly, what is cosmetic, what will not print.

Usage: python3 tools/util/silk_report.py BOARD.kicad_pcb DRC.json [--list]

The triage is geometric, not by description text. In KiCad 10 `silk_over_copper` comes with
the description "Silkscreen clipped by solder mask" and ONE item - the silkscreen object itself;
looking for a second item ("what is covered") finds nothing. So the tool finds the object by
uuid, takes its bbox and intersects it with mask openings (pads with the mask layer of their
side open, and untented vias such as test points).

What this class is NOT: ink does not land on pads when the silkscreen Gerber is exported with
`--subtract-soldermask` (as tools/make_fab.sh does), which subtracts mask openings from the
silkscreen. These findings do not affect soldering; the damage is that the text prints broken.

Distinctions that matter:
  * text clipped by ITS OWN pad - normal in dense layouts: the reference sits on its part,
    some letters are cut, but the addressee is obvious;
  * by ANOTHER part's pad - worse: cut more and placed over another part, so the text no
    longer points at its own part unambiguously;
  * `silk_overlap` within one part (reference over its own outline) - readability is fine,
    no reason to move the reference away from its part;
  * `text_height` and `silk_edge_clearance` - fab minimums, must be fixed.

Also printed is what DRC is silent about: texts ENTIRELY outside the board outline.
`silk_edge_clearance` catches crossings of the outline, but an object that moved fully outside
does not cross it, so a reference that will not be on the board falls into no finding class.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import json
import sys
from collections import Counter, defaultdict

import pcbnew

MM = 1e6
KIND = ('silk_over_copper', 'silk_overlap', 'silk_edge_clearance', 'text_height')
args = [a for a in sys.argv[1:] if not a.startswith('--')]
if len(args) < 2:
    raise SystemExit(__doc__)
BRD, DRC = args[0], args[1]
LIST = '--list' in sys.argv

b = pcbnew.LoadBoard(BRD)
d = json.load(open(DRC))
by = {k: [v for v in d['violations'] if v['type'] == k] for k in KIND}

# --- silkscreen objects of the board, indexed by uuid
silk = {}        # uuid -> (owner, object)
for fp in b.GetFootprints():
    items = [fp.Reference(), fp.Value()] + list(fp.GraphicalItems())
    for it in items:
        if 'Silkscreen' in b.GetLayerName(it.GetLayer()):
            silk[it.m_Uuid.AsString()] = (fp.GetReference(), it)
for g in b.GetDrawings():
    if 'Silkscreen' in b.GetLayerName(g.GetLayer()):
        silk[g.m_Uuid.AsString()] = (None, g)

# --- mask openings: pads with an open mask layer AND untented vias
# Test points are often made as vias with the mask removed on both sides; such a via is also
# an opening that clips ink. Without them findings end up as "mask opening not found".
# IsTented(layer) already applies the board rules, so FROM_RULES needs no manual handling.
mask_pads = []   # (ref, num, side, bbox)
for fp in b.GetFootprints():
    for pd in fp.Pads():
        ls = pd.GetLayerSet()
        for side, lay in (('F', pcbnew.F_Mask), ('B', pcbnew.B_Mask)):
            if ls.Contains(lay):
                mask_pads.append((fp.GetReference(), pd.GetNumber(), side, pd.GetBoundingBox()))
for t in b.GetTracks():
    if t.GetClass() != 'PCB_VIA':
        continue
    for side, lay in (('F', pcbnew.F_Mask), ('B', pcbnew.B_Mask)):
        if not t.IsTented(lay):
            mask_pads.append(('(via %s)' % t.GetNetname().split('/')[-1], 'via', side,
                              t.GetBoundingBox()))


def side_of(obj):
    return 'F' if b.GetLayerName(obj.GetLayer()).startswith('F.') else 'B'


def clippers(obj):
    """Mask openings intersecting the bbox of a silkscreen object."""
    bb, sd = obj.GetBoundingBox(), side_of(obj)
    return [(r, n) for r, n, s, pb in mask_pads if s == sd and bb.Intersects(pb)]


print('silkscreen findings: %d' % sum(len(x) for x in by.values()))
for k in KIND:
    print('  %-22s %d' % (k, len(by[k])))

# --- silk_over_copper: what clips the text
own, alien, probe, none_found, unknown = [], [], [], [], []
for v in by['silk_over_copper']:
    it = v['items'][0]
    hit = silk.get(it.get('uuid'))
    if not hit:
        unknown.append((it.get('description'), it.get('pos'))); continue
    owner, obj = hit
    cl = clippers(obj)
    mine = [c for c in cl if c[0] == owner]
    others = [c for c in cl if c[0] != owner]
    tp = [c for c in others if c[1] == 'via']
    pads_alien = [c for c in others if c[1] != 'via']
    if pads_alien:
        alien.append((owner, it.get('description'), pads_alien + tp, it.get('pos')))
    elif tp:
        probe.append((owner, it.get('description'), tp, it.get('pos')))
    elif mine:
        own.append((owner, it.get('description'), mine, it.get('pos')))
    else:
        none_found.append((owner, it.get('description'), it.get('pos')))

print('\nsilk_over_copper - what clips the ink (geometry, not the DRC description):')
print('  own pad (addressee obvious)                : %d' % len(own))
print('  ANOTHER part\'s pad (cut over another part) : %d  <- the real target' % len(alien))
print('  test-point via (cosmetic)                  : %d' % len(probe))
print('  mask opening not found (check manually)   : %d' % len(none_found))
if unknown:
    print('  object not found by uuid: %d' % len(unknown))

# --- silk_overlap: within one part or between parts
same, diff = [], []
for v in by['silk_overlap']:
    refs = []
    for it in v['items']:
        ds = it.get('description', '')
        refs.append(ds.split(' of ')[-1].split(' on ')[0].strip() if ' of ' in ds else '?')
    (same if len(set(refs)) == 1 else diff).append((refs, v['items'][0].get('pos')))
print('\nsilk_overlap - what the text overlaps:')
print('  within one part (do not move)           : %d' % len(same))
print('  between different parts (readability)  : %d' % len(diff))
if diff:
    print('     parts: %s' % ', '.join('%s/%s' % (r[0], r[1]) for r, _ in diff[:10]))

# --- what DRC is silent about: silkscreen outside the board outline
edge = b.GetBoardEdgesBoundingBox()
outside = []
for uid, (owner, obj) in silk.items():
    bb = obj.GetBoundingBox()
    if not edge.Intersects(bb):
        txt = obj.GetText() if hasattr(obj, 'GetText') else obj.GetClass()
        outside.append((owner, txt, bb.GetCenter().x / MM, bb.GetCenter().y / MM))
print('\noutside the board outline (not reported by DRC - will not print): %d' % len(outside))
for owner, txt, x, y in sorted(outside):
    print('  %-6s %-24r centre (%.2f,%.2f), outline X %.1f..%.1f Y %.1f..%.1f'
          % (owner, txt, x, y, edge.GetLeft() / MM, edge.GetRight() / MM,
             edge.GetTop() / MM, edge.GetBottom() / MM))

# --- fab minimums
print('\ntext_height (fab minimum, fix):')
for v in by['text_height']:
    for i in v['items']:
        print('  %-52s @ (%.2f,%.2f)' % (i.get('description', '')[:52],
                                         i['pos']['x'], i['pos']['y']))
        print('     %s' % v['description'])
print('\nsilk_edge_clearance (fab minimum, fix):')
for v in by['silk_edge_clearance']:
    print('  %s' % ' + '.join(i.get('description', '') for i in v['items']))

if LIST:
    print('\n--- texts clipped by ANOTHER part\'s pad ---')
    for owner, txt, cl, pos in alien:
        print('  %-6s %-28s clipped by: %s @ (%.2f,%.2f)'
              % (owner, txt[:28], ', '.join('%s.%s' % c for c in cl[:4]), pos['x'], pos['y']))
    if probe:
        print('\n--- texts clipped by a test-point via (do not move) ---')
        for owner, txt, cl, pos in probe:
            print('  %-6s %-28s %s' % (owner, txt[:28], ', '.join('%s' % c[0] for c in cl[:3])))
    print('\n--- texts clipped by their own pad ---')
    for owner, txt, cl, pos in own:
        print('  %-6s %-28s own: %s' % (owner, txt[:28], ', '.join('%s.%s' % c for c in cl[:4])))
    if none_found:
        print('\n--- mask opening not found ---')
        for owner, txt, pos in none_found:
            print('  %-6s %-28s @ (%.2f,%.2f)' % (owner, txt[:28], pos['x'], pos['y']))
