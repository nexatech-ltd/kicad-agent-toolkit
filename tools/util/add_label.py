"""Add a net label to a schematic sheet.

Usage: python3 tools/util/add_label.py SHEET.kicad_sch NAME X Y [ROT] [JUST] [--global] [--apply]

Without --apply it is a dry run that only reports the size change. --global creates a global
label; this is required when a net must match a net-class pattern (e.g. `RF_*`), because a
local label gets the sheet path as a prefix and the pattern no longer matches. Useful for
sheets wired with labels on the pins instead of wires.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import sys, os, uuid as _uuid
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import sexp
sh, name, x, y = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
rot = float(sys.argv[5]) if len(sys.argv) > 5 and not sys.argv[5].startswith('--') else 0
just = sys.argv[6] if len(sys.argv) > 6 and not sys.argv[6].startswith('--') else 'left'
s = open(sh).read(); n0 = len(s)
if '(label "%s"' % name in s and '(at %g %g' % (x, y) in s:
    sys.exit('label %s may already exist - check manually' % name)
if '--global' in sys.argv:
    lbl = ('\t(global_label "%s"\n\t\t(shape passive)\n\t\t(at %g %g %g)\n\t\t(effects\n\t\t\t(font\n'
           '\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify %s)\n\t\t)\n\t\t(uuid "%s")\n'
           '\t\t(property "Intersheetrefs" "${INTERSHEET_REFS}"\n\t\t\t(at %g %g 0)\n\t\t\t(hide yes)\n'
           '\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n\t\t\t(effects\n\t\t\t\t(font\n'
           '\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)'
           % (name, x, y, rot, just, _uuid.uuid4(), x, y))
else:
    lbl = ('\t(label "%s"\n\t\t(at %g %g %g)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n'
           '\t\t\t(justify %s)\n\t\t)\n\t\t(uuid "%s")\n\t)' % (name, x, y, rot, just, _uuid.uuid4()))
k = s.rstrip().rfind(')')
s = s.rstrip()[:k] + '\n' + lbl + '\n)\n'
sexp.parse(s)
if '--apply' in sys.argv:
    open(sh, 'w').write(s); print('label %s at (%g,%g) written to %s: %d -> %d bytes' % (name, x, y, sh, n0, len(s)))
else:
    print('label %s at (%g,%g) - dry run (%d -> %d bytes)' % (name, x, y, n0, len(s)))
