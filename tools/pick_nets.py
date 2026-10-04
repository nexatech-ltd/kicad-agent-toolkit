"""Pick nets for an incremental (per-batch) routing round from a DRC report.

The ONLY_NETS_FILE list must not be built from "every break": that would include the
plane nets, and the router then lays tracks over the pours - acceptance showed over a
hundred shorting_items and dozens of tracks_crossing. Plane breaks are fixed by
stitching vias (stitch_gnd.py), not by tracks.

  python3 tools/pick_nets.py <drc.json> [--max-len MM] [--limit N] [-o FILE]

Prints net names one per line - the format dsn_export.py reads.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drcinfo as D
import project

# RF copper is laid out by impedance calculation (e.g. a microstrip on F.Cu referenced
# to In1.Cu) and must not be rerouted. Config key `rf_net_prefixes`.
RF_PREFIX = tuple(project.get('rf_net_prefixes', ['Net-(ANT', 'RF_']))


def pick(drc, max_len=None, limit=None):
    seen, out = set(), []
    rows = sorted(D.signal_breaks(drc), key=lambda b: b[2])   # shortest first
    for net, kinds, dist, refs, pos in rows:
        if net in D.PLANE_NETS or net == '?':
            continue
        base = net.split('/')[-1]
        if net.startswith(RF_PREFIX) or base.startswith(RF_PREFIX):
            continue
        if base.startswith('unconnected-'):
            continue
        if max_len is not None and dist > max_len:
            continue
        if net in seen:
            continue
        seen.add(net)
        out.append((net, dist))
        if limit and len(out) >= limit:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('drc')
    ap.add_argument('--max-len', type=float, default=None, help='maximum break length, mm')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('-o', '--out', default=None)
    a = ap.parse_args()
    d = D.load(a.drc)
    rows = pick(d, a.max_len, a.limit)
    total = len({b[0] for b in D.signal_breaks(d)} - D.PLANE_NETS - {'?'})
    sys.stderr.write('picked %d nets of %d signal nets; breaks %.2f..%.2f mm\n'
                     % (len(rows), total,
                        rows[0][1] if rows else 0, rows[-1][1] if rows else 0))
    text = ''.join(n + '\n' for n, _ in rows)
    if a.out:
        with open(a.out, 'w') as f:
            f.write(text)
        sys.stderr.write('written: %s\n' % a.out)
    else:
        sys.stdout.write(text)


if __name__ == '__main__':
    main()
