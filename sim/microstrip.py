#!/usr/bin/env python3
"""Characteristic impedance of a microstrip (Hammerstad-Jensen with copper-thickness correction),
e.g. an RF feed on F.Cu referenced to In1.Cu over a thin prepreg.

    python3 sim/microstrip.py                          # table for W = 0.15...0.30 mm
    python3 sim/microstrip.py --h 0.0994 --er 4.05     # dielectric height (mm) and permittivity
"""
import argparse
import math


def z0_microstrip(w, h, er, t=0.035):
    # effective width corrected for copper thickness (Hammerstad)
    if w / h >= 1 / (2 * math.pi):
        weff = w + t / math.pi * (1 + math.log(2 * h / t))
    else:
        weff = w + t / math.pi * (1 + math.log(4 * math.pi * w / t))
    u = weff / h
    a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    f = 6 + (2 * math.pi - 6) * math.exp(-(30.666 / u) ** 0.7528)
    z = 376.73 / (2 * math.pi * math.sqrt(eeff)) * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))
    return z, eeff


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--h', type=float, default=0.0994, help='dielectric height to the reference plane, mm '
                    '(default: a typical ~0.1 mm prepreg of a 6-layer stack)')
    ap.add_argument('--er', type=float, default=4.05, help='relative permittivity of the dielectric')
    ap.add_argument('--t', type=float, default=0.035, help='copper thickness, mm')
    ap.add_argument('--widths', default='0.15,0.152,0.18,0.20,0.22,0.25,0.30', help='track widths, mm')
    a = ap.parse_args()
    h, er = a.h, a.er
    print('microstrip, h = %.4f mm, er = %.2f, copper %.0f um' % (h, er, a.t * 1000))
    for w in (float(x) for x in a.widths.split(',')):
        z, e = z0_microstrip(w, h, er, a.t)
        print('  W = %.3f mm: Z0 = %.1f Ohm, eeff = %.2f' % (w, z, e))
