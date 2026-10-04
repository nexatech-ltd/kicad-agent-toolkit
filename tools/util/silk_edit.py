"""Point edits of silkscreen and fabrication texts from an operation list.

Usage: python3 tools/util/silk_edit.py BOARD.kicad_pcb OPS.json [--apply]

Operations (pcbnew, one LoadBoard, SaveBoard at the end):
  {"op":"text_size","ref":"U1","match":"TEXT","h":0.8,"w":0.8}     height/width of a silkscreen text
  {"op":"text_place","ref":"U1","field":"reference","near":[x,y],  move the text to the nearest free
   "max_r":2.0,"optional":true}                                    spot around `near`; max_r limits the
                                                                   distance, optional = skip if no room
  {"op":"shape_trim","ref":"U1","ymax":134.8}                      clip the part's silkscreen lines
                                                                   (likewise xmin/xmax/ymin)
  {"op":"fab_ref","ref":"U1","h":1.0,"replace":true}               reference on the assembly drawing
                                                                   (F.Fab/B.Fab), position and height
                                                                   chosen to avoid other Fab texts

Why separate from fix_silk.py: that tool fixes only what DRC reports, and only by moving the
reference with its own heuristic. It does not change text height (the fab's minimum
`text_height`), does not clip a part's silkscreen at the board edge (`silk_edge_clearance` for
modules overhanging the edge), and does not see texts that moved ENTIRELY outside the outline -
DRC is silent about those because they do not cross the outline.

A spot is free when the text bbox lies inside the board outline (with EDGE_KEEP margin) and
touches neither mask openings nor silkscreen of other parts. The part's own pad is not an
obstacle: a reference over its own part is still readable, and moving it away is worse - a
reference far from its part is useless. Without --apply only the plan is shown.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import json
import math
import sys

import pcbnew

MM = 1e6
EDGE_KEEP = 0.25      # mm - text bbox margin from the board outline
STEP = 0.25           # mm - position search step
MAX_R = 10.0          # mm - maximum distance from `near`

bp, ops = sys.argv[1], json.load(open(sys.argv[2]))
apply = '--apply' in sys.argv
b = pcbnew.LoadBoard(bp)

outline = pcbnew.SHAPE_POLY_SET()
b.GetBoardPolygonOutlines(outline, True)

FP = {fp.GetReference(): fp for fp in b.GetFootprints()}


def texts(fp):
    return [('reference', fp.Reference()), ('value', fp.Value())] + \
           [('graphic', g) for g in fp.GraphicalItems() if g.GetClass() == 'PCB_TEXT']


def side(obj):
    return 'F' if b.GetLayerName(obj.GetLayer()).startswith('F.') else 'B'


# Obstacles: mask openings and silkscreen, with their owner - own items do not count.
# Pad bboxes are cached (pads do not move), but for silkscreen items the OBJECT itself is stored
# and its bbox is taken at check time: caching bboxes before the moves makes every later move
# check against the old positions of already moved texts, and a "successful" pass then
# increases silk_overlap. Board-level silkscreen (b.GetDrawings()) is included too; without it
# texts outside footprints would not be obstacles at all.
mask_pads, silk_items = [], []
for fp in b.GetFootprints():
    r = fp.GetReference()
    for pd in fp.Pads():
        ls = pd.GetLayerSet()
        for sd, lay in (('F', pcbnew.F_Mask), ('B', pcbnew.B_Mask)):
            if ls.Contains(lay):
                mask_pads.append((r, sd, pd.GetBoundingBox()))
    for _k, t in texts(fp):
        if 'Silkscreen' in b.GetLayerName(t.GetLayer()):
            silk_items.append((r, side(t), t))
    for g in fp.GraphicalItems():
        if g.GetClass() != 'PCB_TEXT' and 'Silkscreen' in b.GetLayerName(g.GetLayer()):
            silk_items.append((r, side(g), g))
# Untented vias (test points) are mask openings as well: ink breaks on them.
# They have no owner, so they block any text.
for t in b.GetTracks():
    if t.GetClass() == 'PCB_VIA':
        for sd, lay in (('F', pcbnew.F_Mask), ('B', pcbnew.B_Mask)):
            if not t.IsTented(lay):
                mask_pads.append((None, sd, t.GetBoundingBox()))
for g in b.GetDrawings():
    if 'Silkscreen' in b.GetLayerName(g.GetLayer()):
        silk_items.append((None, side(g), g))


# Part bboxes, so that a moved reference does not end up closer to another part than to its
# own. This is the rule "a displaced reference is worse than an overlapped one" in measurable
# form: with neighbours about 1 mm apart, even a 1.5 mm move can re-address the label.
FPBB = {}
for _r, _fp in FP.items():
    _bb = _fp.GetBoundingBox(False, False)
    FPBB[_r] = (_bb.GetLeft() / MM, _bb.GetTop() / MM, _bb.GetRight() / MM, _bb.GetBottom() / MM)


def dist_to_box(x, y, box):
    x0, y0, x1, y1 = box
    return math.hypot(max(x0 - x, 0, x - x1), max(y0 - y, 0, y - y1))


def owner_is_nearest(bb, owner):
    if owner not in FPBB: return True
    c = bb.GetCenter(); x, y = c.x / MM, c.y / MM
    mine = dist_to_box(x, y, FPBB[owner])
    for r, box in FPBB.items():
        if r == owner: continue
        if dist_to_box(x, y, box) < mine - 1e-9:
            return False, r
    return True, None


def inside_board(bb):
    keep = int(EDGE_KEEP * MM)
    for x, y in ((bb.GetLeft(), bb.GetTop()), (bb.GetRight(), bb.GetTop()),
                 (bb.GetLeft(), bb.GetBottom()), (bb.GetRight(), bb.GetBottom())):
        if not outline.Contains(pcbnew.VECTOR2I(x, y)):
            return False
    grown = pcbnew.BOX2I(bb.GetOrigin(), bb.GetSize()); grown.Inflate(keep)
    for x, y in ((grown.GetLeft(), grown.GetTop()), (grown.GetRight(), grown.GetTop()),
                 (grown.GetLeft(), grown.GetBottom()), (grown.GetRight(), grown.GetBottom())):
        if not outline.Contains(pcbnew.VECTOR2I(x, y)):
            return False
    return True


def free(obj, owner):
    bb, sd = obj.GetBoundingBox(), side(obj)
    if not inside_board(bb):
        return False, 'outside the board outline'
    for r, s, pb in mask_pads:
        if s == sd and r != owner and bb.Intersects(pb):
            return False, 'mask opening %s' % r
    for r, s, it in silk_items:
        if s != sd or r == owner or it is obj: continue
        if bb.Intersects(it.GetBoundingBox()):
            return False, 'silkscreen %s' % (r or 'of the board')
    ok, other = owner_is_nearest(bb, owner)
    if not ok:
        return False, 'closer to another part %s' % other
    return True, ''


changed = 0
for o in ops:
    fp = FP.get(o['ref'])
    if fp is None:
        sys.exit('ERROR: no part %s on the board' % o['ref'])

    if o['op'] == 'text_size':
        # Silkscreen only: the text_height minimum applies to ink, while an assembly-drawing
        # text (F.Fab) easily matches the same `match` string and would be resized wrongly.
        hit = [t for _k, t in texts(fp)
               if o.get('match', '') in t.GetText() and 'Silkscreen' in b.GetLayerName(t.GetLayer())]
        if not hit:
            sys.exit('ERROR: %s has no silkscreen text containing %r' % (o['ref'], o.get('match')))
        for t in hit:
            was = (t.GetTextHeight() / MM, t.GetTextWidth() / MM)
            t.SetTextHeight(int(round(o['h'] * MM))); t.SetTextWidth(int(round(o['w'] * MM)))
            changed += 1
            print('  %s %r: height %.2f -> %.2f, width %.2f -> %.2f'
                  % (o['ref'], t.GetText(), was[0], o['h'], was[1], o['w']))

    elif o['op'] == 'text_place':
        t = dict(texts(fp))[o.get('field', 'reference')]
        p0 = t.GetPosition(); nx, ny = o.get('near', [p0.x / MM, p0.y / MM])
        # The distance limit comes from the operation: a reference far from its part is less useful
        # than an overlapped one, so dense areas use 1.5-2 mm rather than the full MAX_R.
        lim = float(o.get('max_r', MAX_R))
        best = None
        r = 0.0
        while r <= lim and best is None:
            cand = []
            n = max(1, int(2 * math.pi * r / STEP)) if r > 0 else 1
            for i in range(n):
                a = 2 * math.pi * i / n
                cand.append((nx + r * math.cos(a), ny + r * math.sin(a)))
            for x, y in cand:
                t.SetPosition(pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM))))
                ok, _why = free(t, o['ref'])
                if ok:
                    best = (x, y); break
            r += STEP
        if best is None:
            t.SetPosition(p0)
            msg = ('no free spot for %s.%s within %.1f mm of (%.2f,%.2f)'
                   % (o['ref'], o.get('field', 'reference'), lim, nx, ny))
            if o.get('optional'):
                print('  SKIPPED: ' + msg); continue
            sys.exit('ERROR: ' + msg)
        t.SetPosition(pcbnew.VECTOR2I(int(round(best[0] * MM)), int(round(best[1] * MM))))
        changed += 1
        print('  %s.%s: (%.2f,%.2f) -> (%.2f,%.2f), %.2f mm from target'
              % (o['ref'], o.get('field', 'reference'), p0.x / MM, p0.y / MM, best[0], best[1],
                 math.hypot(best[0] - nx, best[1] - ny)))

    elif o['op'] == 'shape_trim':
        lim = {k: o[k] for k in ('xmin', 'xmax', 'ymin', 'ymax') if k in o}
        if not lim:
            sys.exit('ERROR: shape_trim without limits')
        n = 0
        for g in fp.GraphicalItems():
            if g.GetClass() == 'PCB_TEXT' or 'Silkscreen' not in b.GetLayerName(g.GetLayer()):
                continue
            for get, st in ((g.GetStart, g.SetStart), (g.GetEnd, g.SetEnd)):
                p = get(); x, y = p.x / MM, p.y / MM
                nx2 = min(max(x, lim.get('xmin', -1e9)), lim.get('xmax', 1e9))
                ny2 = min(max(y, lim.get('ymin', -1e9)), lim.get('ymax', 1e9))
                if abs(nx2 - x) > 1e-6 or abs(ny2 - y) > 1e-6:
                    st(pcbnew.VECTOR2I(int(round(nx2 * MM)), int(round(ny2 * MM))))
                    n += 1
                    print('  %s: point (%.3f,%.3f) -> (%.3f,%.3f)' % (o['ref'], x, y, nx2, ny2))
        if n == 0:
            sys.exit('ERROR: nothing to clip on %s - limits already met' % o['ref'])
        changed += n
    elif o['op'] == 'fab_ref':
        # The reference on the assembly drawing is what assembly uses to place the part. Some
        # silkscreen findings in dense areas cannot be fixed by moving (there is no room), and then
        # readability relies on F.Fab, so the drawing must be complete. Simply centring the text
        # in the part bbox is not enough: on a small part it can overlap a neighbour's Fab value
        # text, and there are no DRC rules on F.Fab to catch that. So position and height are
        # searched: first the requested height at the centre, then offsets inside the part bbox,
        # then smaller heights.
        lay = 'B.Fab' if b.GetLayerName(fp.GetLayer()).startswith('B.') else 'F.Fab'
        have = [g for g in fp.GraphicalItems()
                if g.GetClass() == 'PCB_TEXT' and 'Fab' in b.GetLayerName(g.GetLayer())
                and ('${REFERENCE}' in g.GetText() or g.GetText() == o['ref'])]
        if have and not o.get('replace'):
            sys.exit('ERROR: %s already has a reference on %s (use "replace": true)' % (o['ref'], lay))
        # Obstacles are only other parts' Fab TEXTS. Part outlines on F.Fab must not count: they
        # cover almost the whole board, and with them no densely placed part finds a free spot.
        # A thin outline line does not hinder reading a reference - another text does.
        fab_items = []
        for fp2 in b.GetFootprints():
            if fp2.GetReference() == o['ref']: continue
            for g2 in [fp2.Reference(), fp2.Value()] + list(fp2.GraphicalItems()):
                if g2.GetClass() == 'PCB_TEXT' and 'Fab' in b.GetLayerName(g2.GetLayer()):
                    fab_items.append(g2)
        own_fab = [g for g in fp.GraphicalItems()
                   if 'Fab' in b.GetLayerName(g.GetLayer()) and g.GetClass() == 'PCB_TEXT'
                   and g not in have]
        bb = fp.GetBoundingBox(False, False)
        c = bb.GetCenter()
        t = have[0] if have else pcbnew.PCB_TEXT(fp)
        if not have:
            t.SetText('${REFERENCE}')
            t.SetLayer(b.GetLayerID(lay))
            t.SetTextThickness(int(round(0.15 * MM)))
            fp.Add(t)
        placed = None
        for hmm in [o.get('h', 1.0), 0.8, 0.6, 0.5]:
            t.SetTextSize(pcbnew.VECTOR2I(int(round(hmm * MM)), int(round(hmm * MM))))
            step = int(round(0.25 * MM))
            for dy in range(0, int(bb.GetHeight() / 2) + step, step):
                for sy in ((0,) if dy == 0 else (-dy, dy)):
                    for dx in range(0, int(bb.GetWidth() / 2) + step, step):
                        for sx in ((0,) if dx == 0 else (-dx, dx)):
                            t.SetPosition(pcbnew.VECTOR2I(c.x + sx, c.y + sy))
                            tb = t.GetBoundingBox()
                            if any(tb.Intersects(g2.GetBoundingBox()) for g2 in fab_items): continue
                            if any(tb.Intersects(g2.GetBoundingBox()) for g2 in own_fab): continue
                            ok_near, _other = owner_is_nearest(tb, o['ref'])
                            if not ok_near: continue      # the reference must not point at a neighbour
                            placed = (hmm, t.GetPosition()); break
                        if placed: break
                    if placed: break
                if placed: break
            if placed: break
        if placed is None:
            t.SetTextSize(pcbnew.VECTOR2I(int(round(0.5 * MM)), int(round(0.5 * MM))))
            t.SetPosition(c)
            print('  WARNING: %s - no free spot on %s, reference placed at the centre '
                  'at 0.50 mm height, overlapping' % (o['ref'], lay))
            placed = (0.5, c)
        changed += 1
        print('  %s: reference on %s at (%.2f,%.2f), height %.2f mm%s'
              % (o['ref'], lay, placed[1].x / MM, placed[1].y / MM, placed[0],
                 ' (moved)' if have else ''))
    else:
        sys.exit('ERROR: unknown operation %r' % o['op'])

print('items changed: %d' % changed)
if apply:
    pcbnew.SaveBoard(bp, b)
    print('saved', bp)
else:
    print('plan shown. Add --apply to write')
