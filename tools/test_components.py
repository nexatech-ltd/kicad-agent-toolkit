#!/usr/bin/env python3
"""Mutation test of check_components.py: the check must fail when a fixed defect is put back.

Why. A check that catches nothing reports the same way as a check with nothing to catch
("a check that did not run reports zero"). So every defect found against the official
documentation is reproduced here as a mutation on a COPY of the netlist and the registry,
and the test requires the check to see it and name it with the right code.

What is checked (mutation -> expected code):
  POL-1   swap pins 1 and 2 of a polarized protection diode (TVS cathode to ground)
  REQ-1   zero the capacitance on a pin that requires one (e.g. USB-PD controller CC1)
  REQ-4   unhook the 0.1 uF ceramic from a supply input (bulk on the net remains)
  REQ-3   leave RESERVED pins of a controller floating
  ORD-3   give a connector the order code of a part with another pin count
  ORD-4   give a part an order code with zero stock
  DOC-1   point a part's datasheet at another package variant
  ORD-8   give a passive the order code of another value
  PIN-1   remove the pin-name mapping from a registry part file
  PKG-1   declare a wrong signal pad count in a registry part file
  THERM-1 record a thermal regime that overheats the junction
  VOLT-3  lower a supply abs-max in a registry part file below the rail voltage

Mutation targets are board-specific. Give them in kicad-agent.json (or a JSON file via
--targets); a case without a target is skipped and reported as such:

  "test_components": {
    "POL-1":   {"ref": "D4"},
    "REQ-1":   {"ref": "C10", "value": "0pF CC1"},
    "REQ-4":   {"ref": "C11", "pins": ["1", "2"]},
    "REQ-3":   {"ref": "U1", "pins": ["26", "27"]},
    "ORD-3":   {"ref": "J1", "lcsc": "C123456"},
    "ORD-4":   {"ref": "J1", "lcsc": "C654321"},
    "DOC-1":   {"ref": "U2", "datasheet": "https://example.com/other-variant.pdf"},
    "ORD-8":   {"ref": "C12", "lcsc": "C1525"},
    "PIN-1":   {"part": "flash_part.yaml"},
    "PKG-1":   {"part": "connector_part.yaml", "signal_pads": 40},
    "THERM-1": {"part": "controller_part.yaml"},
    "VOLT-3":  {"part": "flash_part.yaml", "abs_max": 3.0}
  }

ORD-4 needs a REAL code with zero stock: a made-up code (C0000000) is caught by another
check, ORD-2 ("code not in the catalog"), and the mutation would test the wrong thing.
ORD-8 needs a real, stocked code of another value: every earlier check stays silent.

    python3 tools/test_components.py                 # all cases
    python3 tools/test_components.py --case POL-1    # one
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import project

CHECK = os.path.join(HERE, 'check_components.py')
T = {}   # mutation targets: {case code: {...}}


# --- netlist mutations ------------------------------------------------------------------------
def swap_pins(net_text, ref):
    """Swap the nets of pins 1 and 2 of a part: how a reversed TVS looked before the fix."""
    a = re.search(r'\(node\s*\(ref "%s"\)\s*\(pin "1"\)' % ref, net_text)
    b = re.search(r'\(node\s*\(ref "%s"\)\s*\(pin "2"\)' % ref, net_text)
    if not (a and b):
        raise SystemExit('mutation impossible: %s has no pins 1 and 2 in the netlist' % ref)
    t = net_text
    t = t[:a.start()] + t[a.start():a.end()].replace('"1"', '"9"') + t[a.end():]
    b = re.search(r'\(node\s*\(ref "%s"\)\s*\(pin "2"\)' % ref, t)
    t = t[:b.start()] + t[b.start():b.end()].replace('"2"', '"1"') + t[b.end():]
    a = re.search(r'\(node\s*\(ref "%s"\)\s*\(pin "9"\)' % ref, t)
    return t[:a.start()] + t[a.start():a.end()].replace('"9"', '"2"') + t[a.end():]


def set_comp_field(net_text, ref, field, value):
    """Replace a field value in the (comp (ref REF) ...) block."""
    m = re.search(r'\(comp\s*\(ref "%s"\)' % ref, net_text)
    end = net_text.index('(comp ', m.end()) if '(comp ' in net_text[m.end():] else m.end() + 4000
    blk = net_text[m.start():end]
    nb, n = re.subn(r'(\(field\s*\(name "%s"\) )"[^"]*"' % field, r'\1"%s"' % value, blk)
    if not n:
        raise SystemExit('mutation impossible: %s has no field %s' % (ref, field))
    return net_text[:m.start()] + nb + net_text[end:]


def set_value(net_text, ref, value):
    m = re.search(r'\(comp\s*\(ref "%s"\)\s*\(value "[^"]*"\)' % ref, net_text)
    if not m:
        raise SystemExit('mutation impossible: %s has no value' % ref)
    return net_text[:m.start()] + re.sub(r'\(value "[^"]*"\)', '(value "%s")' % value,
                                         net_text[m.start():m.end()]) + net_text[m.end():]


def unhook(net_text, ref, pins):
    """Remove a part from its nets: the nodes are deleted, the pin is left without a net."""
    t = net_text
    for pin in pins:
        t = re.sub(r'\s*\(node\s*\(ref "%s"\)\s*\(pin "%s"\)(?:\s*\(pinfunction "[^"]*"\))?'
                   r'(?:\s*\(pintype "[^"]*"\))?\s*\)' % (ref, pin), '', t, count=1)
    return t


# --- registry mutations -----------------------------------------------------------------------
def reg_edit(reg_dir, name, fn):
    p = os.path.join(reg_dir, name)
    d = yaml.safe_load(open(p))
    fn(d)
    yaml.safe_dump(d, open(p, 'w'), allow_unicode=True, sort_keys=False, default_flow_style=False)


CASES = {}


def case(code, what):
    def deco(fn):
        CASES[code] = (what, fn)
        return fn
    return deco


@case('POL-1', 'polarized protection diode reversed (cathode to ground)')
def _(net, reg):
    return swap_pins(net, T['POL-1']['ref']), None


@case('REQ-1', 'required capacitance on a pin set to zero')
def _(net, reg):
    t = T['REQ-1']
    return set_value(net, t['ref'], t.get('value', '0pF')), None


@case('REQ-4', '0.1 uF ceramic unhooked from a supply input')
def _(net, reg):
    # Restores a historical defect: the bulk capacitance on the net stays (e.g. two 10 uF),
    # but there is no high-frequency ceramic next to the pin. A per-net sum does not see
    # this; only the REQ-4 axis, which measures pin-to-part distance, does.
    t = T['REQ-4']
    return unhook(net, t['ref'], t.get('pins', ['1', '2'])), None


@case('REQ-3', 'RESERVED pins of a controller left floating')
def _(net, reg):
    t = T['REQ-3']
    return unhook(net, t['ref'], t['pins']), None


@case('ORD-3', 'connector given the code of another pin count')
def _(net, reg):
    t = T['ORD-3']
    return set_comp_field(net, t['ref'], 'LCSC', t['lcsc']), None


@case('ORD-4', 'order code of a part with zero stock')
def _(net, reg):
    # A REAL code with zero stock in the catalog snapshot. A made-up code (C0000000) is
    # caught by another check, ORD-2 ("code not in the catalog"), and the mutation would
    # test something other than what it claims.
    t = T['ORD-4']
    return set_comp_field(net, t['ref'], 'LCSC', t['lcsc']), None


@case('DOC-1', 'datasheet link of another package variant')
def _(net, reg):
    t = T['DOC-1']
    return set_comp_field(net, t['ref'], 'Datasheet', t['datasheet']), None


@case('PIN-1', 'pin-name mapping removed from the registry')
def _(net, reg):
    def f(d):
        for v in d['pins']:
            v.pop('symbol_name', None)
    reg_edit(reg, T['PIN-1']['part'], f)
    return net, None


@case('PKG-1', 'wrong signal pad count declared in the registry')
def _(net, reg):
    t = T['PKG-1']
    reg_edit(reg, t['part'],
             lambda d: d['package'].__setitem__('signal_pads', t.get('signal_pads', 40)))
    return net, None


@case('THERM-1', 'thermal regime overheating the junction')
def _(net, reg):
    reg_edit(reg, T['THERM-1']['part'], lambda d: d.__setitem__('thermal', {
        'conditions': 'test mutation', 'pd_w': 2.0, 'rth_ja': 50.0, 'tj_max': 125,
        'source': 'test mutation'}))
    return net, None


@case('ORD-8', 'passive code replaced by the code of another value')
def _(net, reg):
    # E.g. a 100 nF code on a 10 uF capacitor: the code exists, is in stock, the catalog
    # knows it, and every earlier check stays silent. ORD-8 closes exactly this class.
    t = T['ORD-8']
    return set_comp_field(net, t['ref'], 'LCSC', t['lcsc']), None


@case('VOLT-3', 'supply abs-max below the rail voltage')
def _(net, reg):
    t = T['VOLT-3']

    def f(d):
        d['voltages'][0]['abs_max'] = t.get('abs_max', 3.0)
    reg_edit(reg, t['part'], f)
    return net, None


def run(code, base_net, base_reg, board='', keep=False):
    what, fn = CASES[code]
    if code not in T:
        print('  %-8s %-56s %s' % (code, what[:56], 'skipped (no target)'))
        return None
    tmp = tempfile.mkdtemp(prefix='cmpmut_')
    net = os.path.join(tmp, os.path.basename(base_net))
    reg = os.path.join(tmp, 'components')
    shutil.copy(base_net, net)
    shutil.copytree(base_reg, reg)
    text, _ = fn(open(net).read(), reg)
    open(net, 'w').write(text)
    # The board must be passed: the package axis compares the footprint pad count with the
    # registry, and without --board the PKG check honestly stays silent, while the test
    # counted the mutation as caught from the gap entry.
    cmd = [sys.executable, CHECK, '--net', net, '--registry', reg, '--allow-incomplete']
    if board:
        cmd += ['--board', board]
    r = subprocess.run(cmd, capture_output=True, text=True)
    # Codes are taken ONLY from the defects section: in the "not checked" section the same
    # codes mean registry gaps, and the test would wrongly count the mutation as caught
    # (a mutation that was not caught showed PIN-1, PKG-1, THERM-1 and VOLT-1 as gaps).
    # Section markers must match check_components.py output: "DEFECTS (n):" / "not checked".
    body = r.stdout.split('DEFECTS')[1].split('not checked')[0] if 'DEFECTS' in r.stdout else ''
    got = re.findall(r'\[(\w+-\d+)\s*\]', body)
    ok = code in got
    print('  %-8s %-56s %s' % (code, what[:56], 'caught' if ok else 'NOT CAUGHT'))
    if not ok:
        print('      codes in the output: %s' % (sorted(set(got)) or 'none'))
        if r.returncode not in (0, 1):
            print('      check crashed: %s' % (r.stderr or '')[-400:])
    if keep:
        print('      mutation directory: %s' % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--net', default=paths.NETLIST)
    ap.add_argument('--registry', default=os.path.join(
        paths.KAT_ROOT, project.get('component_registry', os.path.join('doc', 'components'))))
    ap.add_argument('--board', default=paths.BOARD,
                    help='board; without it the package axis is not checked')
    ap.add_argument('--targets', help='JSON file with mutation targets '
                    '(default: config key "test_components")')
    ap.add_argument('--case', help='only one code')
    ap.add_argument('--keep', action='store_true', help='keep the mutation directories')
    a = ap.parse_args()

    T.update(json.load(open(a.targets, encoding='utf-8')) if a.targets
             else (project.get('test_components', {}) or {}))

    print('mutation test of check_components.py')
    print('netlist : %s' % a.net)
    print('registry: %s' % a.registry)
    print('board   : %s' % (a.board or 'not given: the package axis is not checked'))
    codes = [a.case] if a.case else sorted(CASES)
    res = {c: run(c, a.net, a.registry, a.board, a.keep) for c in codes}
    bad = [c for c, ok in res.items() if ok is False]
    tested = [c for c, ok in res.items() if ok is not None]
    print()
    if not tested:
        print('RESULT: nothing tested, no mutation targets configured')
        return 1
    if bad:
        print('RESULT: not caught %d of %d: %s' % (len(bad), len(tested), ', '.join(bad)))
        return 1
    print('RESULT: all %d mutations caught (%d skipped)' % (len(tested), len(codes) - len(tested)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
