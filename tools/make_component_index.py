#!/usr/bin/env python3
"""Coverage map of the component registry: ref -> what it is verified by.

The registry (`<component_registry>/`, default doc/components) records the norm (numbers
from the datasheet) per part, not per designator: hundreds of placements typically map to
a few dozen unique MPNs. So an index is needed, a map "which designator is covered by
what", and it is COMPUTED from the netlist, not written by hand: a hand-written list of
hundreds of lines diverges from the schematic at the first part swap (e.g. a connector
whose footprint was updated while the purchasing fields stayed from the previous part).

Categories (by fact, not by prefix; the prefix lies: an `L` may be an inductor with an
MPN or a ferrite bead without one):
  ic        - has an MPN: the norm lives in its own file <component_registry>/<slug>.yaml
  passive   - R/C/L/FB without MPN: the norm lives as a group in passives.yaml (value + package)
  nofield   - purchased part without MPN and not a passive: a defect in itself, fix the schematic
  nobuy     - by footprint (test point, mounting hole, printed antenna): not ordered,
              the norm lives in not-purchased.yaml

    python3 tools/make_component_index.py --net work/board.net              # show the summary
    python3 tools/make_component_index.py --net work/board.net --write      # index.yaml
    python3 tools/make_component_index.py --net work/board.net --passives   # + passives.yaml

Key property of --passives: filled fields are NOT OVERWRITTEN. Passive groups are created
with the value `TBD`, then the LCSC code, voltage rating and source are filled in by hand;
a re-run only adds new groups and drops vanished ones, otherwise the work of fixing codes
would be wiped by every index rebuild.
"""
import argparse
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import project
from check_stock import comps

PROJ = paths.KAT_ROOT
REG = os.path.join(PROJ, project.get('component_registry', os.path.join('doc', 'components')))
PASSIVE_PREFIX = {'R', 'C', 'L', 'FB'}
# "Not ordered" is decided by FOOTPRINT, not by the designator prefix. The prefix lies: one
# prefix such as `ANT_*` may hold both a printed antenna (really not purchased) and real parts
# (a matching inductor with an order code, 0R resistors of the matching network). Treating
# them as non-purchased would drop real BOM lines from the check. Config key
# "not_purchased_footprints" (footprint name substrings; add e.g. a printed-antenna footprint).
NOBUY_FP = tuple(project.get('not_purchased_footprints', ['TestPoint_', 'MountingHole_']))


def slug(mpn):
    """Registry file name from a part number: ABC123RGER -> abc123rger, ABC123-Q1 -> abc123-q1."""
    return re.sub(r'[^a-z0-9.+-]+', '_', (mpn or '').lower()).strip('_')


def prefix(ref):
    return re.match(r'[A-Za-z]+', ref).group(0)


def group_key(c):
    """Passive group: value + package. A part without an MPN is ordered by exactly this pair."""
    return '%s | %s' % (c['value'], c['fp'].split(':')[-1])


def classify(net):
    cs = [c for c in comps(net) if not c['ref'].startswith('#')]
    for c in cs:
        p = prefix(c['ref'])
        if c['mpn']:
            c['cat'], c['key'] = 'ic', slug(c['mpn'])
        elif any(x in c['fp'] for x in NOBUY_FP):
            c['cat'], c['key'] = 'nobuy', c['fp'].split(':')[-1]
        elif p in PASSIVE_PREFIX or re.match(r'(R|C|L|FB)_\d{4}_', c['fp'].split(':')[-1]):
            # A passive is also recognised by footprint: an antenna matching network may use
            # designators like `ANT_M1`, yet these are plain 0402 resistors (some DNP), and by
            # prefix they would land in "no MPN field and not a passive".
            c['cat'], c['key'] = 'passive', group_key(c)
        else:
            c['cat'], c['key'] = 'nofield', c['ref']
    return cs


PASSIVE_FIELDS = ('lcsc', 'voltage_rating', 'rail', 'dielectric', 'tolerance', 'source')


def passives(cs):
    """Create/update passive groups in passives.yaml without losing filled fields."""
    import yaml
    p = os.path.join(REG, 'passives.yaml')
    old = {}
    if os.path.exists(p):
        old = (yaml.safe_load(open(p)) or {}).get('groups', {}) or {}
    now = collections.defaultdict(list)
    for c in cs:
        if c['cat'] == 'passive':
            now[c['key']].append(c['ref'])

    data, new, gone = {}, [], [k for k in old if k not in now]
    for key in sorted(now):
        refs = sorted(now[key], key=lambda r: (prefix(r), int(re.sub(r'\D', '', r) or 0)))
        e = dict(old.get(key) or {})
        if key not in old:
            new.append(key)
            for f in PASSIVE_FIELDS:
                e.setdefault(f, 'TBD')
        # designators are always recomputed: they are a schematic fact, not a part decision
        e['refs'] = refs
        e['count'] = len(refs)
        data[key] = e

    os.makedirs(REG, exist_ok=True)
    with open(p, 'w') as f:
        yaml.safe_dump({'group_count': len(data), 'groups': data}, f,
                       allow_unicode=True, sort_keys=False, default_flow_style=False)
    done = sum(1 for e in data.values() if e.get('lcsc') not in (None, '', 'TBD'))
    print('\n%s: groups %d, with a code %d, new %d, vanished %d'
          % (os.path.relpath(p, PROJ), len(data), done, len(new), len(gone)))
    for k in gone:
        print('  vanished group: %s' % k)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--net', default=paths.NETLIST)
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--passives', action='store_true',
                    help='add new groups to passives.yaml (filled fields are kept)')
    a = ap.parse_args()

    cs = classify(a.net)
    cat = collections.Counter(c['cat'] for c in cs)
    print('parts in the netlist: %d' % len(cs))
    for k in ('ic', 'passive', 'nobuy', 'nofield'):
        keys = {c['key'] for c in cs if c['cat'] == k}
        print('  %-8s parts %3d, distinct norms %3d' % (k, cat[k], len(keys)))
    bad = [c['ref'] for c in cs if c['cat'] == 'nofield']
    if bad:
        print('  NO MPN FIELD and not a passive: %s' % ', '.join(sorted(bad)))

    if a.passives:
        return passives(cs)

    if not a.write:
        print('\nsummary only. Add --write to write index.yaml')
        return 0

    import yaml
    idx = {'source': os.path.relpath(os.path.abspath(a.net), PROJ),
           'count': len(cs), 'components': {}}
    for c in sorted(cs, key=lambda c: (prefix(c['ref']), int(re.sub(r'\D', '', c['ref']) or 0))):
        e = {'category': c['cat'], 'norm': c['key'], 'value': c['value'],
             'footprint': c['fp']}
        if c['mpn']:
            e['mpn'] = c['mpn']
        if c['lcsc']:
            e['lcsc'] = c['lcsc']
        if c['dnp']:
            e['dnp'] = True
        idx['components'][c['ref']] = e
    os.makedirs(REG, exist_ok=True)
    p = os.path.join(REG, 'index.yaml')
    with open(p, 'w') as f:
        yaml.safe_dump(idx, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print('\nwritten %s (%d designators)' % (os.path.relpath(p, PROJ), len(cs)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
