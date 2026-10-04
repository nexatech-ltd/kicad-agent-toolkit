"""Reading a KiCad DRC report: breaks per net and violations by severity.

It exists because accepting a routing round is decided by three numbers (unconnected,
significant-severity violations, warnings), and every pipeline script used to count them
its own way. One home-made count already caused a miss: a custom list of
"manufacturing-relevant types" left out via_diameter, and three 0.45 mm vias on a board
with a 0.6 mm minimum passed as a non-issue.

The rule built in here: severity comes from KiCad; there is no custom type classification.
"""
import collections
import json
import math
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project

# Plane (poured) nets: their breaks are pour fragments, fixed by stitching vias
# (stitch_gnd.py), not by tracks. Never hand them to the router (verified: over a
# hundred shorting_items). Config key `plane_nets`.
PLANE_NETS = set(project.plane_nets())

_NET_RE = re.compile(r'\[([^\]]*)\]')
_REF_RE = re.compile(r'\bof (\S+)')


def run_drc(board, out, kicad_cli=None, refill=True, save_board=False, parity=False):
    """Full DRC report - all severities, not a filtered one.

    A --severity-error filter once hid 20 hole_to_hole items, 19 of them below the fab minimum.
    """
    cli = kicad_cli or paths.KICAD_CLI
    cmd = [cli, 'pcb', 'drc', '--severity-all', '--format', 'json', '-o', out]
    if refill:
        cmd.append('--refill-zones')
    if save_board:
        cmd.append('--save-board')
    if parity:
        # Without this flag the schematic_parity section exists but is EMPTY, and
        # "0 mismatches" reads as "checked" although the check never ran.
        cmd.append('--schematic-parity')
    cmd.append(board)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 5):        # 5 = DRC found violations, not a launch failure
        raise RuntimeError('kicad-cli pcb drc: rc=%d\n%s%s' % (r.returncode, r.stdout, r.stderr))
    return load(out)


def load(path):
    with open(path) as f:
        d = json.load(f)
    assert 'unconnected_items' in d and 'violations' in d, 'not a DRC report: %s' % path
    return d


def _net_of(item):
    m = _NET_RE.search(item.get('description', ''))
    return m.group(1) if m else None


def _ref_of(item):
    m = _REF_RE.search(item.get('description', ''))
    return m.group(1) if m else None


def _kind_of(item):
    d = item.get('description', '')
    for k in ('Zone', 'Pad', 'Track', 'Via', 'PTH pad', 'Footprint'):
        if d.startswith(k):
            return 'Pad' if k == 'PTH pad' else k
    return d.split()[0] if d else '?'


def breaks(drc):
    """List of breaks: (net, kind+kind, length mm, (ref, ref), positions)."""
    out = []
    for x in drc['unconnected_items']:
        items = x.get('items', [])
        net = next((n for n in map(_net_of, items) if n), '?')
        kinds = tuple(sorted(_kind_of(i) for i in items))
        refs = tuple(sorted({r for r in map(_ref_of, items) if r}))
        if len(items) >= 2:
            a, b = items[0]['pos'], items[1]['pos']
            dist = math.hypot(a['x'] - b['x'], a['y'] - b['y'])
            pos = ((a['x'], a['y']), (b['x'], b['y']))
        else:
            dist, pos = 0.0, ()
        out.append((net, kinds, dist, refs, pos))
    assert len(out) == len(drc['unconnected_items'])
    return out


def by_net(drc):
    """Break count per net."""
    return collections.Counter(b[0] for b in breaks(drc))


def signal_breaks(drc):
    """Breaks that tracks can close - without pour fragments of plane nets."""
    return [b for b in breaks(drc) if not (b[0] in PLANE_NETS and 'Zone' in b[1])]


def severity(drc):
    """{severity: count} over violations - KiCad's classification, not a custom one."""
    return collections.Counter(v.get('severity', '?') for v in drc['violations'])


def types(drc):
    """{(severity, type): count}."""
    return collections.Counter((v.get('severity', '?'), v.get('type', '?'))
                               for v in drc['violations'])


def score(drc):
    """The triple a round is judged by: (unconnected, errors, warnings)."""
    s = severity(drc)
    return len(drc['unconnected_items']), s.get('error', 0), s.get('warning', 0)


def fmt(drc, label=''):
    u, e, w = score(drc)
    head = '%s: ' % label if label else ''
    return '%sunconnected %d | errors %d | warnings %d' % (head, u, e, w)


def clearance_min(drc):
    """Smallest actual clearance quoted in violation texts (a typical fab minimum is 0.0889 mm)."""
    vals = [float(m.group(1)) for v in drc['violations']
            for m in [re.search(r'actual ([0-9.]+) mm', v.get('description', ''))] if m]
    return min(vals) if vals else None
