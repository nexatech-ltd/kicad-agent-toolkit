#!/usr/bin/env python3
"""List what lies inside a window of a schematic sheet: symbols, labels, wires, NC flags, junctions.

    python3 tools/sch_window.py sheet.kicad_sch 85 55 140 105
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import re
import sys

f, x1, y1, x2, y2 = sys.argv[1], *map(float, sys.argv[2:6])
s = open(f).read()
inb = lambda x, y: x1 <= x <= x2 and y1 <= y <= y2

print('== symbols')
for m in re.finditer(r'\(symbol\s*\(lib_id "([^"]*)"\)\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)(.*?)\n\t\)\n', s, re.S):
    lib, x, y, r, body = m.group(1), float(m.group(2)), float(m.group(3)), m.group(4), m.group(5)
    if not inb(x, y):
        continue
    ref = re.search(r'\(property "Reference" "([^"]*)"', body)
    val = re.search(r'\(property "Value" "([^"]*)"', body)
    mir = 'mirror' if '(mirror' in body[:200] else ''
    unit = re.search(r'\(unit (\d+)\)', body)
    print('  %-8s %-24s at (%.2f, %.2f) rot %s %s unit %s  [%s]'
          % (ref.group(1) if ref else '?', val.group(1)[:24] if val else '', x, y, r, mir,
             unit.group(1) if unit else '1', lib))
print('== labels')
for m in re.finditer(r'\((global_label|hierarchical_label|label) "([^"]*)"\s*(?:\(shape \w+\)\s*)?\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', s):
    x, y = float(m.group(3)), float(m.group(4))
    if inb(x, y):
        print('  %-18s %-22s at (%.2f, %.2f) rot %s' % (m.group(1), m.group(2), x, y, m.group(5)))
print('== wires')
for m in re.finditer(r'\(wire\s*\(pts\s*\(xy ([-\d.]+) ([-\d.]+)\)\s*\(xy ([-\d.]+) ([-\d.]+)\)', s):
    a, b, c, d = map(float, m.groups())
    if inb(a, b) or inb(c, d):
        print('  (%.2f, %.2f) - (%.2f, %.2f)' % (a, b, c, d))
print('== NC / junctions')
for m in re.finditer(r'\((no_connect|junction)\s*\(at ([-\d.]+) ([-\d.]+)\)', s):
    x, y = float(m.group(2)), float(m.group(3))
    if inb(x, y):
        print('  %-10s at (%.2f, %.2f)' % (m.group(1), x, y))
