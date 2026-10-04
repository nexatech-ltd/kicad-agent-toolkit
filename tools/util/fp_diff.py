"""How a footprint on the board differs from its library copy - decoding `lib_footprint_mismatch`.

Usage: python3 tools/util/fp_diff.py BOARD.kicad_pcb [U1 ...] [--all] [--summary]

DRC only states the fact ("Footprint 'C_0402_1005Metric' does not match copy in library"),
not what differs, and dozens of such items become an unreadable heap - yet the answer decides
whether footprints may be updated from the library, and the reason should be named per part.
Comparison is by substance, in footprint-local coordinates: pads (number, position, size,
drill, shape, layers), graphics per layer (item counts), texts (which, height, layer) and
attributes. Purchase fields (LCSC/MPN) and text positions are reported separately: they are
harmless, unlike pad and graphics geometry. Libraries are searched in $KICAD_FOOTPRINT_DIR
(default /usr/share/kicad/footprints), then lib/ next to the board, then lib/ in $KAT_ROOT.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import math
import os
import re
import sys
from collections import Counter

import pcbnew

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import paths

MM = 1e6
SYS_LIB = os.environ.get('KICAD_FOOTPRINT_DIR', '/usr/share/kicad/footprints')
IGNORE_FIELDS = {'Reference', 'Value', 'Datasheet', 'Description', 'ki_fp_filters'}

bp = sys.argv[1]
want = [a for a in sys.argv[2:] if not a.startswith('--')]
ALL = '--all' in sys.argv
SUMMARY = '--summary' in sys.argv
b = pcbnew.LoadBoard(bp)

import os
proj_lib = os.path.join(os.path.dirname(os.path.abspath(bp)), 'lib')
if not os.path.isdir(proj_lib):
    proj_lib = os.path.join(paths.KAT_ROOT, 'lib')   # board is a trial copy outside the project


def lib_path(libname):
    for root in (SYS_LIB, proj_lib):
        p = os.path.join(root, libname + '.pretty')
        if os.path.isdir(p):
            return p
    return None


def local(fp, pos):
    """Item coordinates in the footprint frame (offset, rotation and flip removed)."""
    dx = (pos.x - fp.GetPosition().x) / MM
    dy = (pos.y - fp.GetPosition().y) / MM
    a = math.radians(fp.GetOrientationDegrees())
    x = dx * math.cos(a) - dy * math.sin(a)
    y = dx * math.sin(a) + dy * math.cos(a)
    if fp.IsFlipped():
        x = -x
    return round(x, 3), round(y, 3)


def pad_sig(fp, pd):
    return (pd.GetNumber(), local(fp, pd.GetPosition()),
            (round(pd.GetSize().x / MM, 3), round(pd.GetSize().y / MM, 3)),
            round(pd.GetDrillSize().x / MM, 3), pd.GetShape(), pd.GetAttribute(),
            tuple(sorted(b.GetLayerName(l) for l in pd.GetLayerSet().Seq())))


def gfx_sig(fp):
    c = Counter()
    for g in fp.GraphicalItems():
        if g.GetClass() == 'PCB_TEXT':
            continue
        c[(b.GetLayerName(g.GetLayer()), g.GetClass(), round(g.GetWidth() / MM, 3))] += 1
    return c


def texts(fp):
    """Footprint texts. Reference and Value contents must not be compared: on the board they
    are e.g. "C1" and "100nF", in the library "REF**" and the footprint name - a substitution
    at placement, not a difference. So their key is the role, not the text; other texts are
    compared as they are."""
    out = {}
    named = [('REF', fp.Reference()), ('VAL', fp.Value())]
    named += [(g.GetText(), g) for g in fp.GraphicalItems() if g.GetClass() == 'PCB_TEXT']
    for key, t in named:
        out.setdefault((key, b.GetLayerName(t.GetLayer())), []).append(
            (round(t.GetTextHeight() / MM, 3), local(fp, t.GetPosition())))
    return out


def fields(fp):
    try:
        return {f.GetName(): f.GetText() for f in fp.GetFields()
                if f.GetName() not in IGNORE_FIELDS}
    except Exception:
        return {}


rows = []
for fp in b.GetFootprints():
    ref = fp.GetReference()
    if want and ref not in want:
        continue
    fpid = fp.GetFPIDAsString()
    libname, name = (fpid.split(':', 1) if ':' in fpid else ('', fpid))
    lp = lib_path(libname)
    if not lp:
        rows.append((ref, fpid, ['library_not_found: ' + libname])); continue
    try:
        ref_fp = pcbnew.FootprintLoad(lp, name)
    except Exception as e:
        rows.append((ref, fpid, ['load_error: %s' % e])); continue
    if ref_fp is None:
        rows.append((ref, fpid, ['missing_in_library: ' + name])); continue

    diffs = []
    a_pads = sorted(pad_sig(fp, p) for p in fp.Pads())
    b_pads = sorted(pad_sig(ref_fp, p) for p in ref_fp.Pads())
    if len(a_pads) != len(b_pads):
        diffs.append('pads: %d instead of %d' % (len(a_pads), len(b_pads)))
    elif a_pads != b_pads:
        n = sum(1 for x, y in zip(a_pads, b_pads) if x != y)
        diffs.append('pad_geometry: %d of %d' % (n, len(a_pads)))
    ga, gb = gfx_sig(fp), gfx_sig(ref_fp)
    if ga != gb:
        add = sum((ga - gb).values()); rem = sum((gb - ga).values())
        lays = sorted({k[0] for k in (ga - gb) | (gb - ga)})
        diffs.append('graphics: +%d/-%d on %s' % (add, rem, ','.join(lays)))
    ta, tb = texts(fp), texts(ref_fp)
    if set(ta) - set(tb) or set(tb) - set(ta):
        diffs.append('text_set: +%d/-%d' % (len(set(ta) - set(tb)), len(set(tb) - set(ta))))
    else:
        moved = [k for k in ta if ta[k] != tb.get(k)]
        if moved:
            hdiff = [k for k in moved if {h for h, _ in ta[k]} != {h for h, _ in tb[k]}]
            diffs.append('texts_moved: %d%s' % (len(moved),
                         ', height changed: %d' % len(hdiff) if hdiff else ''))
    fa, fb = fields(fp), fields(ref_fp)
    if fa != fb:
        extra = sorted(set(fa) ^ set(fb))
        buy = {'LCSC', 'MPN', 'KiLib_Generator'}
        if extra and set(extra) <= buy:
            diffs.append('purchase_fields: %s' % ', '.join(extra))   # harmless, intended
        else:
            diffs.append('fields: %s' % ', '.join(extra or ['values']))
    if fp.GetAttributes() != ref_fp.GetAttributes():
        diffs.append('attributes: %d instead of %d' % (fp.GetAttributes(), ref_fp.GetAttributes()))
    if diffs or ALL:
        rows.append((ref, fpid, diffs or ['match']))

print('compared with library: %d footprints differ' % len([r for r in rows if r[2] != ['match']]))
kinds = Counter()
for ref, fpid, diffs in rows:
    for d in diffs:
        kinds[re.sub(r'[:\s].*', '', d)] += 1
print('\nby kind of difference:')
for k, n in kinds.most_common():
    print('  %-22s %d' % (k, n))
if not SUMMARY:
    print()
    for ref, fpid, diffs in sorted(rows):
        print('  %-7s %-52s %s' % (ref, fpid[:52], '; '.join(diffs)))
