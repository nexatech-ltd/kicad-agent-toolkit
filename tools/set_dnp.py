#!/usr/bin/env python3
"""Set the (dnp yes) attribute on parts that must not be assembled.

Why. Marking DNP only as text in the value field ("XYZ123 (DNP)") leaves the KiCad
attribute at `no`. The exporters do not read the value text, so such parts end up in both
the placement file and the BOM of the fabrication kit, and the assembler fits them: optional
drivers and connectors cost money, and a populated antenna-matching placeholder detunes the
antenna.

Done as a text edit: the attribute is a single word inside the symbol block.

    python3 tools/set_dnp.py <schematic dir> [REF[=reason] ...] [--apply]

Without REF arguments the list is read from the `dnp_refs` key of kicad-agent.json
({"REF": "reason", ...}).
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sexp import blocks
import project


def main():
    pos = [x for x in sys.argv[1:] if not x.startswith('--')]
    root = pos[0] if pos else '.'
    apply = '--apply' in sys.argv
    # Parts that are not assembled: REF[=reason] on the command line, else the config.
    TARGETS = {}
    for x in pos[1:]:
        ref, _, why = x.partition('=')
        TARGETS[ref] = why
    if not TARGETS:
        TARGETS = dict(project.get('dnp_refs', {}))
    if not TARGETS:
        raise SystemExit('no parts given: pass REF arguments or set "dnp_refs" in kicad-agent.json')
    found, changed = {}, 0
    for path in sorted(glob.glob(os.path.join(root, '*.kicad_sch'))):
        s = open(path).read()
        out, prev, hits = [], 0, 0
        for a, b in blocks(s, 'symbol'):
            blk = s[a:b]
            rm = re.search(r'\(property "Reference" "([^"]+)"', blk)
            if not rm or rm.group(1) not in TARGETS:
                continue
            ref = rm.group(1)
            found[ref] = os.path.basename(path)
            m = re.search(r'\(dnp (\w+)\)', blk)
            if not m:
                print('  %-6s in %s: no (dnp ...) node, skipped'
                      % (ref, os.path.basename(path)))
                continue
            if m.group(1) == 'yes':
                continue
            nb = blk[:m.start(1)] + 'yes' + blk[m.end(1):]
            # also append (DNP) to the value if it is not there yet, so the drawing shows it
            vm = re.search(r'\(property "Value" "([^"]*)"', nb)
            if vm and 'DNP' not in vm.group(1).upper():
                nb = nb[:vm.start(1)] + vm.group(1) + ' (DNP)' + nb[vm.end(1):]
            out.append(s[prev:a]); out.append(nb); prev = b
            hits += 1
        if hits:
            changed += hits
            if apply:
                open(path + '.bakDnp', 'w').write(s)
                open(path, 'w').write(''.join(out) + s[prev:])
            print('  %-24s marked %d' % (os.path.basename(path), hits))
    missing = set(TARGETS) - set(found)
    print('\ntotal marked: %d' % changed)
    if missing:
        print('NOT FOUND in the schematic: %s' % ', '.join(sorted(missing)))
        return 1
    if not apply:
        print('dry run, use --apply to write')
    return 0


if __name__ == '__main__':
    sys.exit(main())
