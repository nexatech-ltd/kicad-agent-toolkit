#!/usr/bin/env python3
"""6 -> 8 layers: a text transformation of a .kicad_pcb (fallback when routing plateaus on 6 layers).

8L stack (a common fab standard stack-up); the top prepreg F.Cu-GND1 is unchanged (keeps the impedance of
controlled-impedance/RF lines on F.Cu referenced to GND1):
    F.Cu | GND1 (In1) | SIG1 (In2, new) | PWR1 (In3) | PWR2 (In4) | SIG2 (In5, new) | GND2 (In6) | B.Cu
Existing copper moves: In4.Cu -> In6.Cu, In3.Cu -> In4.Cu, In2.Cu -> In3.Cu (user names GND2/PWR2/PWR1 are
kept). Through vias and *.Cu pads are unchanged. Writes a new file and leaves the input untouched.
Assumes the 6-layer input uses the stack F.Cu | GND1 | PWR1 | PWR2 | GND2 | B.Cu; adjust NEW_LAYERS / STACK
for another stack-up.

    python3 tools/layers8.py --in board.kicad_pcb --out work/l8/board.kicad_pcb
"""
import argparse
import re
import sys

NEW_LAYERS = '''	(layers
		(0 "F.Cu" signal)
		(4 "In1.Cu" power "GND1")
		(6 "In2.Cu" signal "SIG1")
		(8 "In3.Cu" power "PWR1")
		(10 "In4.Cu" mixed "PWR2")
		(12 "In5.Cu" signal "SIG2")
		(14 "In6.Cu" power "GND2")
		(2 "B.Cu" signal)
'''

# (name, type, thickness, material, epsilon_r, loss_tangent) - copper: (name, 'copper', thickness)
STACK = [
    ('F.Cu', 'copper', 0.035),
    ('dielectric 1', 'prepreg', 0.0994, 'FR4', 4.05, 0.02),     # unchanged: impedance-controlled lines over GND1
    ('In1.Cu', 'copper', 0.0152),
    ('dielectric 2', 'core', 0.2, 'FR4', 4.6, 0.02),
    ('In2.Cu', 'copper', 0.0152),
    ('dielectric 3', 'prepreg', 0.15, 'FR4', 4.05, 0.02),
    ('In3.Cu', 'copper', 0.0152),
    ('dielectric 4', 'core', 0.5, 'FR4', 4.6, 0.02),
    ('In4.Cu', 'copper', 0.0152),
    ('dielectric 5', 'prepreg', 0.15, 'FR4', 4.05, 0.02),
    ('In5.Cu', 'copper', 0.0152),
    ('dielectric 6', 'core', 0.2, 'FR4', 4.6, 0.02),
    ('In6.Cu', 'copper', 0.0152),
    ('dielectric 7', 'prepreg', 0.0994, 'FR4', 4.05, 0.02),
    ('B.Cu', 'copper', 0.035),
]


def stack_text(indent='\t\t\t'):
    out = []
    for e in STACK:
        if e[1] == 'copper':
            out.append('%s(layer "%s"\n%s\t(type "copper")\n%s\t(thickness %g)\n%s)\n' % (indent, e[0], indent, indent, e[2], indent))
        else:
            out.append('%s(layer "%s"\n%s\t(type "%s")\n%s\t(thickness %g)\n%s\t(material "%s")\n%s\t(epsilon_r %g)\n%s\t(loss_tangent %g)\n%s)\n'
                       % (indent, e[0], indent, e[1], indent, e[2], indent, e[3], indent, e[4], indent, e[5], indent))
    return ''.join(out)


def convert(txt):
    # 1. layer header: replace the copper lines
    m = re.search(r'\t\(layers\n((?:\t\t\(\d+ "[^"]+" (?:signal|power|mixed|jumper)(?: "[^"]+")?\)\n)+)', txt)
    assert m, 'no (layers) block with copper layers found'
    cu_block = m.group(0)
    assert '"In4.Cu"' in cu_block and '"In5.Cu"' not in cu_block, 'the board is not 6-layer'
    head_end = m.end()
    head, body = txt[:head_end], txt[head_end:]
    head = head.replace(cu_block, NEW_LAYERS)
    # 2. stack-up: replace copper and dielectric layers between F.Mask and B.Mask
    sm = re.search(r'(\t\t\t\(layer "F\.Cu"\n.*?)(\t\t\t\(layer "B\.Mask")', body, re.S)
    assert sm, 'stack-up not found'
    body = body[:sm.start(1)] + stack_text() + body[sm.start(2):]
    # 3. move existing copper (order matters: In4 -> In6 first)
    n4 = body.count('"In4.Cu"'); n3 = body.count('"In3.Cu"'); n2 = body.count('"In2.Cu"')
    body = body.replace('"In4.Cu"', '"In6.Cu"').replace('"In3.Cu"', '"In4.Cu"').replace('"In2.Cu"', '"In3.Cu"')
    # 4. total thickness
    total = sum(e[2] for e in STACK)
    head = re.sub(r'\(thickness [\d.]+\)', '(thickness %.4f)' % total, head, count=1)
    return head + body, dict(In4_to_In6=n4, In3_to_In4=n3, In2_to_In3=n2, thickness=round(total, 4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='inp', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    txt = open(a.inp).read()
    out, stats = convert(txt)
    open(a.out, 'w').write(out)
    print('written %s: %s' % (a.out, stats))
    return 0


if __name__ == '__main__':
    sys.exit(main())
