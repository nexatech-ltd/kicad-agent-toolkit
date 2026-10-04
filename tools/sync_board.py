#!/usr/bin/env python3
"""Synchronise the board with the schematic: what F8 does, but from the command line.

Why a custom tool. F8 in KiCad exists only in the GUI and only when the board is opened FROM
THE PROJECT MANAGER: a standalone pcbnew answers "Cannot update the PCB because PCB editor is
opened in stand-alone mode". kicad-cli has no synchronisation at all. The Konnect MCP tool
update_pcb_from_schematic refuses when an edit changes a footprint or moves the net of a pin
that already has copper attached (e.g. a 0402 -> 0603 package change, or a pin moved from one
supply to another), and it has no flag to allow that.

The task itself is deterministic: the netlist is the source of truth for components,
footprints, values and pin-to-net assignment, and the library is the source of geometry. The
proof of correctness is not in the code but in acceptance: after a run, DRC with
--schematic-parity must report zero mismatches.

What it does:
  * adds footprints missing from the board (parked at a provisional position; place them afterwards, e.g. with fit_near.py or move_group.py);
  * replaces a footprint that differs in the schematic, keeping position, rotation and pin nets;
  * moves a pin's net if it changed;
  * updates the value and the dnp attribute.

    python3 tools/sync_board.py [--netlist work/B.net]
    python3 tools/sync_board.py [--netlist work/B.net] --apply
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import uuid as _uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import project
from sexp import blocks, find, parse, val

LIB_DIRS = [os.environ.get('KICAD_FOOTPRINT_DIR', '/usr/share/kicad/footprints')]


def lib_path(fp_id, project_dir):
    """'Resistor_SMD:R_0402_1005Metric' -> path to the .kicad_mod."""
    if ':' not in fp_id:
        raise ValueError('footprint without a library: %s' % fp_id)
    lib, name = fp_id.split(':', 1)
    cands = [os.path.join(project_dir, 'lib', lib + '.pretty', name + '.kicad_mod')]
    for d in LIB_DIRS:
        cands.append(os.path.join(d, lib + '.pretty', name + '.kicad_mod'))
    for c in cands:
        if os.path.exists(c):
            return c
    raise FileNotFoundError('footprint %s not found (looked in: %s)' % (fp_id, cands[0]))


def read_netlist(path):
    t = parse(open(path).read())[0]
    comps = {}
    for c in find(find(t, 'components')[0], 'comp'):
        ref = val(c, 'ref')
        props = {}
        dnp = False
        for p in find(c, 'property'):
            nm = val(p, 'name')
            props[nm] = val(p, 'value', '')
            if nm == 'dnp':
                dnp = True          # property without a value, present only on DNP parts
        sp = find(c, 'sheetpath')
        sheet_ts = val(sp[0], 'tstamps', '/') if sp else '/'
        sheet_nm = val(sp[0], 'names', '/') if sp else '/'
        units = []
        for u in find(c, 'units'):
            for un in find(u, 'unit'):
                pins = []
                for pl in find(un, 'pins'):
                    for pn in find(pl, 'pin'):
                        pins.append(val(pn, 'num'))
                units.append((val(un, 'name', 'A'), pins))
        comps[ref] = {
            'value': val(c, 'value', ''),
            'fp': val(c, 'footprint', ''),
            'desc': val(c, 'description', ''),
            'sym_uuid': val(c, 'tstamps', ''),
            'sheet_ts': sheet_ts, 'sheet_nm': sheet_nm,
            'sheetfile': props.get('Sheetfile', ''),
            'fp_filters': props.get('ki_fp_filters', ''),
            'units': units, 'pads': {}, 'funcs': {}, 'dnp': dnp,
        }
    for nt in find(find(t, 'nets')[0], 'net'):
        name = val(nt, 'name')
        for nd in find(nt, 'node'):
            r, p = val(nd, 'ref'), val(nd, 'pin')
            if r in comps:
                comps[r]['pads'][p] = name
                fn = val(nd, 'pinfunction')
                if fn:
                    comps[r]['funcs'][p] = fn
    return comps


def board_footprints(txt):
    """{ref: (start, end, block)} from the top-level blocks."""
    out = {}
    for a, b in blocks(txt, 'footprint'):
        blk = txt[a:b]
        m = re.search(r'\(property "Reference" "([^"]+)"', blk)
        if m:
            out[m.group(1)] = (a, b, blk)
    return out


def esc(s):
    return s.replace('\\', '\\\\').replace('"', '\\"')


def make_footprint(ref, c, x, y, project_dir, rot=0.0):
    """A board (footprint ...) block from a library .kicad_mod (rot is the footprint rotation,
    needed only for zones inside the footprint: KiCad stores those in board coordinates)."""
    src = open(lib_path(c['fp'], project_dir)).read()
    body = src[src.find('(footprint'):].rstrip()
    # In a standalone .kicad_mod the footprint is top level and its content is indented by ONE
    # tab; on the board it is nested and indented by two. Without this re-indent none of the
    # patterns below (\n\t\t(attr ...) etc.) match and the build fails on the first new
    # footprint. Add one tab to every line except the first.
    _ln = body.split('\n')
    body = '\n'.join([_ln[0]] + ['\t' + x if x else x for x in _ln[1:]])
    # drop the library file's service fields and the old name
    body = re.sub(r'^\(footprint\s+"[^"]*"', '(footprint "%s"' % esc(c['fp']), body, count=1)
    body = re.sub(r'\n\t\(version \d+\)', '', body)
    body = re.sub(r'\n\t\(generator[^)]*\)', '', body)
    body = re.sub(r'\n\t\(generator_version[^)]*\)', '', body)
    # fresh uuids for every item: library ones would repeat on the board
    body = re.sub(r'\(uuid "[0-9a-fA-F-]+"\)',
                  lambda m: '(uuid "%s")' % _uuid.uuid4(), body)
    # footprint position
    body = body.replace('(footprint "%s"' % esc(c['fp']),
                        '(footprint "%s"\n\t\t(uuid "%s")\n\t\t(at %.4f %.4f)'
                        % (esc(c['fp']), _uuid.uuid4(), x, y), 1)
    # reference and value
    body = re.sub(r'(\(property "Reference" ")[^"]*(")',
                  lambda m: m.group(1) + esc(ref) + m.group(2), body, count=1)
    body = re.sub(r'(\(property "Value" ")[^"]*(")',
                  lambda m: m.group(1) + esc(c['value']) + m.group(2), body, count=1)
    # A library footprint may have no Description or Datasheet properties at all (stock
    # kicad-footprint-generator footprints do not). Then there is nothing to fill, and parity
    # later reports "Field 'Description' differs": text in the schematic, empty on the board.
    # Create the property when it is missing.
    for name, value in (('Description', c['desc']), ('Datasheet', '')):
        body = set_property(body, name, value)
    # link to the schematic: without it parity reports the footprint as extra
    units = ''.join('\n\t\t\t(unit\n\t\t\t\t(name "%s")\n\t\t\t\t(pins %s)\n\t\t\t)'
                    % (u, ' '.join('"%s"' % p for p in pins)) for u, pins in c['units'])
    link = ('\n\t\t(property ki_fp_filters "%s")'
            '\n\t\t(path "%s%s")'
            '\n\t\t(sheetname "%s")'
            '\n\t\t(sheetfile "%s")'
            '\n\t\t(units%s\n\t\t)'
            % (esc(c['fp_filters']), c['sheet_ts'], c['sym_uuid'],
               esc(c['sheet_nm']), esc(c['sheetfile']), units))
    m = re.search(r'\n\t\t\(attr ', body)
    if m:
        body = body[:m.start()] + link + body[m.start():]
    else:
        m2 = re.search(r'\n\t\t\((?:fp_|pad|model)', body)
        body = body[:m2.start()] + link + '\n\t\t(attr smd)' + body[m2.start():]
    body = set_dnp(body, c['dnp'])
    # pin nets
    def padnet(m):
        num = m.group(1)
        net = c['pads'].get(num)
        if net is None:
            return m.group(0)
        return m.group(0) + '\n\t\t\t(net "%s")' % esc(net)
    body = re.sub(r'\(pad "([^"]*)"[^\n]*\n(?:\t+\([^\n]*\)\n)*?', padnet, body, count=0)
    body = transform_zones(body, x, y, rot)
    return body


def transform_zones(body, x, y, rot=0.0):
    """KiCad stores zones (rule areas) inside a footprint in BOARD coordinates, not footprint
    coordinates, unlike pads and graphics. A library .kicad_mod keeps them in local coordinates,
    so on a text insert the polygon must be rotated and moved here, otherwise e.g. a module's
    antenna keepout lands at the board origin."""
    import math
    th = math.radians(float(rot or 0))
    out, pos = [], 0
    while True:
        i = body.find('(zone', pos)
        if i < 0:
            out.append(body[pos:]); break
        d, q, j = 0, False, i
        while True:
            ch = body[j]
            if q:
                q = ch != '"'
            elif ch == '"':
                q = True
            elif ch == '(':
                d += 1
            elif ch == ')':
                d -= 1
                if d == 0:
                    break
            j += 1
        blk = body[i:j + 1]

        def xy(m):
            lx, ly = float(m.group(1)), float(m.group(2))
            return '(xy %.4f %.4f)' % (x + lx * math.cos(th) + ly * math.sin(th),
                                       y - lx * math.sin(th) + ly * math.cos(th))
        out.append(body[pos:i]); out.append(re.sub(r'\(xy ([-\d.]+) ([-\d.]+)\)', xy, blk))
        pos = j + 1
    return ''.join(out)


def set_property(blk, name, value):
    """Set a footprint property, creating it if missing.

    It is inserted right after the Value property, where KiCad keeps it; property order in
    the block matters only to a human reader.
    """
    pat = r'(\(property "%s" ")[^"]*(")' % re.escape(name)
    if re.search(pat, blk):
        return re.sub(pat, lambda m: m.group(1) + esc(value) + m.group(2), blk, count=1)
    m = re.search(r'\(property "Value" "', blk)
    if not m:
        return blk
    d, j, q = 0, m.start(), False
    while j < len(blk):
        ch = blk[j]
        if q:
            q = ch != '"'
        elif ch == '"':
            q = True
        elif ch == '(':
            d += 1
        elif ch == ')':
            d -= 1
            if d == 0:
                break
        j += 1
    ins = ('\n\t\t(property "%s" "%s"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")'
           '\n\t\t\t(hide yes)\n\t\t\t(uuid "%s")\n\t\t\t(effects\n\t\t\t\t(font'
           '\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)'
           % (name, esc(value), _uuid.uuid4()))
    return blk[:j + 1] + ins + blk[j + 1:]


def set_dnp(blk, want):
    """The dnp flag in (attr ...). Without it --exclude-dnp in the placement export changes
    nothing: the export reads the board attribute, not a note in the value."""
    m = re.search(r'\(attr ([^)]*)\)', blk)
    if not m:
        if not want:
            return blk
        k = re.search(r'\n\t\t\((?:fp_|pad|model)', blk)
        return blk[:k.start()] + '\n\t\t(attr dnp)' + blk[k.start():]
    flags = m.group(1).split()
    has = 'dnp' in flags
    if has == want:
        return blk
    if want:
        flags.append('dnp')
    else:
        flags = [f for f in flags if f != 'dnp']
    return blk[:m.start(1)] + ' '.join(flags) + blk[m.end(1):]


def set_pad_funcs(blk, funcs):
    """Set/fix (pinfunction ...) on pads from a {number: pin name} dict.

    Why separately from nets. When an MCU's order code (package variant) changes, the pad nets
    get updated but the pin names on the board keep the old numbering, exactly on the renumbered
    pads. The field looks informational, but checks that reason about peripherals (e.g. EXTI line
    conflicts in verify.py) read it: a stale name such as `VDDA` where the pin is now `PA0` hides
    a real conflict between two inputs on the same EXTI line.

    Any conclusion about peripherals drawn from the board rather than the netlist shifts along
    with the pins.
    """
    out, prev = [], 0
    for m in re.finditer(r'\(pad "([^"]*)"', blk):
        num = m.group(1)
        i = m.start(); d = 0; j = i; q = False
        while j < len(blk):
            ch = blk[j]
            if q:
                q = ch != '"'
            elif ch == '"':
                q = True
            elif ch == '(':
                d += 1
            elif ch == ')':
                d -= 1
                if d == 0:
                    break
            j += 1
        pad = blk[i:j + 1]
        fn = funcs.get(num)
        cur = re.search(r'\(pinfunction "([^"]*)"\)', pad)
        if fn is None:
            np = pad
        elif cur:
            np = pad[:cur.start(1)] + esc(fn) + pad[cur.end(1):]
        else:
            k = pad.rfind('(uuid')
            ins = '(pinfunction "%s")\n\t\t\t' % esc(fn)
            np = pad[:k] + ins + pad[k:] if k > 0 else pad
        out.append(blk[prev:i]); out.append(np); prev = j + 1
    return ''.join(out) + blk[prev:]


def pad_block(blk, num):
    """The whole block of pad NUM, by bracket balance.

    This used to read a fixed-length window (900 characters) after the pad opening. For a pad
    WITHOUT its own `(net ...)` the window spilled into the next pad and picked up its net: a pad
    with no net at all was shown with the neighbouring pad's net. The sync report then lied in
    the most dangerous direction: "net changes" where there was none.
    """
    m = re.search(r'\(pad "%s"' % re.escape(num), blk)
    if not m:
        return None
    i = m.start()
    d = 0
    q = False
    j = i
    while j < len(blk):
        ch = blk[j]
        if q:
            q = ch != '"'
        elif ch == '"':
            q = True
        elif ch == '(':
            d += 1
        elif ch == ')':
            d -= 1
            if d == 0:
                return blk[i:j + 1]
        j += 1
    return blk[i:]


def set_pad_nets(blk, pads):
    """Set/fix (net ...) on the block's pads from a {number: net} dict."""
    out, prev = [], 0
    for m in re.finditer(r'\(pad "([^"]*)"', blk):
        num = m.group(1)
        # find the end of the pad block by bracket balance
        i = m.start(); d = 0; j = i; q = False
        while j < len(blk):
            ch = blk[j]
            if q:
                q = ch != '"'
            elif ch == '"':
                q = True
            elif ch == '(':
                d += 1
            elif ch == ')':
                d -= 1
                if d == 0:
                    break
            j += 1
        pad = blk[i:j + 1]
        net = pads.get(num)
        nm = re.search(r'\(net "([^"]*)"\)', pad)
        if net is None:
            np = re.sub(r'\n\t+\(net "[^"]*"\)', '', pad)
        elif nm:
            np = pad[:nm.start(1)] + esc(net) + pad[nm.end(1):]
        else:
            k = pad.rfind('(uuid')
            ins = '(net "%s")\n\t\t\t' % esc(net)
            np = pad[:k] + ins + pad[k:] if k > 0 else pad[:-1] + '\n\t\t\t(net "%s")\n\t\t)' % esc(net)
        out.append(blk[prev:i]); out.append(np); prev = j + 1
    return ''.join(out) + blk[prev:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', default=paths.BOARD)
    ap.add_argument('--netlist', default=paths.NETLIST)
    ap.add_argument('--apply', action='store_true')
    # where new footprints are parked before placement; config key park_xy [x, y], mm
    park = project.get('park_xy', [0.0, 0.0])
    ap.add_argument('--park-x', type=float, default=float(park[0]))
    ap.add_argument('--park-y', type=float, default=float(park[1]))
    a = ap.parse_args()

    proj = os.path.dirname(os.path.abspath(a.board))
    lck = os.path.join(proj, '~' + os.path.basename(a.board) + '.lck')
    if a.apply and os.path.exists(lck):
        raise SystemExit('REFUSED: the board is open in the KiCad editor')

    if not os.path.exists(a.netlist):
        sch = paths.sibling(a.board, '.kicad_sch')
        os.makedirs(os.path.dirname(os.path.abspath(a.netlist)), exist_ok=True)
        subprocess.run([paths.KICAD_CLI, 'sch', 'export',
                        'netlist', '--format', 'kicadsexpr', '-o', a.netlist, sch],
                       check=True, capture_output=True)
        print('netlist exported: %s' % a.netlist)
    comps = read_netlist(a.netlist)
    txt = open(a.board).read()
    bf = board_footprints(txt)
    print('schematic: %d components, board: %d footprints' % (len(comps), len(bf)))

    add = [r for r in comps if r not in bf]
    extra = [r for r in bf if r not in comps]
    repl, netfix, valfix, dnpfix = [], [], [], []
    for ref, c in comps.items():
        if ref not in bf:
            continue
        blk = bf[ref][2]
        cur_fp = re.search(r'\(footprint\s+"([^"]*)"', blk).group(1)
        if cur_fp != c['fp']:
            repl.append(ref)
            continue
        cur_val = re.search(r'\(property "Value" "([^"]*)"', blk).group(1)
        if cur_val != c['value']:
            valfix.append(ref)
        dm = re.search(r'\(property "Description" "([^"]*)"', blk)
        if (dm.group(1) if dm else '') != c['desc']:
            valfix.append(ref + '.Description')
        am = re.search(r'\(attr ([^)]*)\)', blk)
        if ('dnp' in (am.group(1).split() if am else [])) != c['dnp']:
            dnpfix.append(ref)
        for num, net in c['pads'].items():
            seg = pad_block(blk, num)
            if seg is None:
                continue
            nm = re.search(r'\(net "([^"]*)"\)', seg)
            if (nm.group(1) if nm else '') != net:
                netfix.append((ref, num, nm.group(1) if nm else '', net))
    print('add: %d | replace footprint: %s | pin nets: %d | values: %d'
          ' | dnp attribute: %d'
          % (len(add), repl or '-', len(netfix), len(valfix), len(dnpfix)))
    if dnpfix:
        print('    dnp: %s' % ', '.join(sorted(dnpfix)))
    if extra:
        print('EXTRA ON THE BOARD (not in the schematic): %s' % ', '.join(sorted(extra)))
    for ref, num, old, new in netfix[:8]:
        print('    %s.%s: "%s" -> "%s"' % (ref, num, old, new))
    if add:
        print('    new: %s' % ', '.join(sorted(add, key=lambda r: (r[0], len(r), r))))

    if not a.apply:
        print('plan only, add --apply to write')
        return 0

    shutil.copy2(a.board, a.board + '.bakSync')
    # 1. replace footprints and update existing ones
    pieces, prev = [], 0
    for ref in sorted(bf, key=lambda r: bf[r][0]):
        s, e, blk = bf[ref]
        c = comps.get(ref)
        if c is None:
            continue
        nb = blk
        cur_fp = re.search(r'\(footprint\s+"([^"]*)"', blk).group(1)
        if cur_fp != c['fp']:
            at = re.search(r'\n\t\t\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)', blk)
            x, y = float(at.group(1)), float(at.group(2))
            rot = at.group(3)
            nb = '\t' + make_footprint(ref, c, x, y, proj, rot=float(rot or 0))
            if rot:
                nb = re.sub(r'\n\t\t\(at [-\d.]+ [-\d.]+\)',
                            '\n\t\t(at %.4f %.4f %s)' % (x, y, rot), nb, count=1)
        else:
            nb = set_pad_nets(nb, c['pads'])
            nb = set_pad_funcs(nb, c['funcs'])
            nb = set_property(nb, 'Value', c['value'])
            nb = set_property(nb, 'Description', c['desc'])
            nb = set_dnp(nb, c['dnp'])
        pieces.append(txt[prev:s]); pieces.append(nb); prev = e
    body = ''.join(pieces) + txt[prev:]

    # 2. new footprints go to the end, at a provisional position
    if add:
        head = body.rstrip()
        assert head.endswith(')')
        head = head[:-1].rstrip('\n')
        news = []
        for i, ref in enumerate(sorted(add, key=lambda r: (r[0], len(r), r))):
            x = a.park_x + (i % 12) * 2.0
            y = a.park_y + (i // 12) * 2.0
            news.append('\t' + make_footprint(ref, comps[ref], x, y, proj))
        body = head + '\n' + '\n'.join(news) + '\n)\n'
    open(a.board, 'w').write(body)
    print('board written, backup .bakSync')
    d = 0
    q = False
    for ch in body:
        if q:
            q = ch != '"'
            continue
        if ch == '"':
            q = True
        elif ch == '(':
            d += 1
        elif ch == ')':
            d -= 1
    print('bracket balance: %s' % ('ok' if d == 0 else 'BROKEN %d' % d))
    return 0


if __name__ == '__main__':
    sys.exit(main())
