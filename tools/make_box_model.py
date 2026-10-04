#!/usr/bin/env python3
"""Approximate part body as a box (VRML) for board views.

Exists because some custom footprints have no 3D model in the project or in
kicad-packages3d, while board views are used to judge enclosure fit and overhang past
the board outline. An empty spot on a render reads as "no part there", and a large module
(e.g. a 15.7 x 17.6 mm LGA) simply vanishes from the picture.

VRML is chosen on purpose, not for simplicity: KiCad does not carry .wrl into
`pcb export step`. So the box shows in views but does NOT end up in the board's
mechanical STEP and cannot be mistaken for real package geometry. The manufacturer's real
body, once available, goes in as .step and replaces the box.

The size is an argument and must be traceable to a datasheet in the component registry;
no made-up dimensions here.

  python3 tools/make_box_model.py --out lib/MyLib.3dshapes/MODULE_LGA.wrl \
      --size 15.7 17.6 2.4 --color 0.16 0.16 0.17
"""
import argparse

TPL = """#VRML V2.0 utf8
# %(note)s
# Size %(sx).2f x %(sy).2f x %(mz).2f mm. Approximation: a box body, no leads or marking.
# UNITS: KiCad reads VRML in units of 0.1 inch (1 unit = 2.54 mm), a KiCad 4 legacy kept for
# compatibility with old .wrl files. So the coordinates below are already divided by 2.54
# and the footprint uses (scale 1 1 1). A box written in millimetres renders 2.54 times too
# large: always verify the size by measuring the render.
Shape {
  appearance Appearance { material Material {
      diffuseColor %(cr).3f %(cg).3f %(cb).3f
      specularColor 0.08 0.08 0.08
      ambientIntensity 0.25
      shininess 0.15
  } }
  geometry IndexedFaceSet {
    solid TRUE
    creaseAngle 0.5
    coord Coordinate { point [
      %(hx).4f %(hy).4f 0, %(nx).4f %(hy).4f 0, %(nx).4f %(ny).4f 0, %(hx).4f %(ny).4f 0,
      %(hx).4f %(hy).4f %(sz).4f, %(nx).4f %(hy).4f %(sz).4f,
      %(nx).4f %(ny).4f %(sz).4f, %(hx).4f %(ny).4f %(sz).4f ] }
    coordIndex [
      3 2 1 0 -1
      4 5 6 7 -1
      0 1 5 4 -1
      1 2 6 5 -1
      2 3 7 6 -1
      3 0 4 7 -1 ]
  }
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--size', nargs=3, type=float, required=True, metavar=('X', 'Y', 'Z'))
    ap.add_argument('--color', nargs=3, type=float, default=[0.16, 0.16, 0.17])
    ap.add_argument('--note', default='approximate body, size taken from the component datasheet')
    a = ap.parse_args()
    sx, sy, sz = a.size
    U = 2.54                              # mm per VRML unit, see the comment in the template
    d = dict(sx=sx, sy=sy, sz=sz / U, hx=sx / 2 / U, hy=sy / 2 / U,
             nx=-sx / 2 / U, ny=-sy / 2 / U,
             mz=sz, cr=a.color[0], cg=a.color[1], cb=a.color[2], note=a.note)
    with open(a.out, 'w', encoding='utf-8') as fh:
        fh.write(TPL % d)
    print('written %s: box %.2f x %.2f x %.2f mm' % (a.out, sx, sy, sz))


if __name__ == '__main__':
    main()
