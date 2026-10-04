#!/usr/bin/env python3
"""Compare S11 curves of several openEMS runs: resonances and per-band performance.

    python3 sim/ant_compare.py run1/s11.csv run2/s11.csv ...
    python3 sim/ant_compare.py --band 2400:2500:wifi24 --band 5150:5850:wifi5 run/s11.csv

CSV columns: f_MHz, S11_dB, Re(Z), Im(Z) (one header row).
Default bands are the common cellular LTE ranges (low / mid / high); pass --band LO:HI:NAME
(MHz, repeatable) for other antennas, e.g. Wi-Fi 2.4/5 GHz.
"""
import argparse
import numpy as np

BANDS = ((703, 960, 'low'), (1710, 2170, 'mid'), (2500, 2690, 'high'))   # LTE low/mid/high, MHz


def load(p):
    d = np.loadtxt(p, delimiter=',', skiprows=1)
    return d[:, 0], d[:, 1], d[:, 2] + 1j * d[:, 3]


def resonances(f, s):
    idx = [i for i in range(1, len(f) - 1) if s[i] < s[i - 1] and s[i] < s[i + 1] and s[i] < -5]
    return ['%.0f (%.1f dB, Z=%.0f%+.0fj)' % (f[i], s[i], z[i].real, z[i].imag) for i in idx]


ap = argparse.ArgumentParser()
ap.add_argument('csv', nargs='+')
ap.add_argument('--band', action='append', default=None,
                help='LO:HI:NAME in MHz, repeatable (default: LTE low/mid/high)')
args = ap.parse_args()
bands = BANDS
if args.band:
    bands = []
    for spec in args.band:
        lo, hi, name = spec.split(':', 2)
        bands.append((float(lo), float(hi), name))

for p in args.csv:
    f, s, z = load(p)
    print('== %s' % p)
    for lo, hi, name in bands:
        m = (f >= lo) & (f <= hi)
        print('   %-5s worst %6.1f  best %6.1f  |Z| at edges %.0f/%.0f Ohm' % (
            name, s[m].max(), s[m].min(), abs(z[m][0]), abs(z[m][-1])))
    print('   resonances: ' + ', '.join(resonances(f, s)))
