"""Helpers for text editing of KiCad schematic sheets (shared by set_*.py, add_cap.py, install_symbol.py and others).

Numbers are formatted the way KiCad writes them: no trailing zeros ("63.5", not "63.50").
"""
import re
import uuid


def span(s, i):
    """Bounds of the s-expression starting at s[i] == '('; quotes are respected."""
    d, q, j = 0, False, i
    while True:
        ch = s[j]
        if q:
            q = ch != '"'
        elif ch == '"':
            q = True
        elif ch == '(':
            d += 1
        elif ch == ')':
            d -= 1
            if d == 0:
                return i, j + 1
        j += 1


def block_start(s, idx, head='\n\t(symbol\n'):
    """Start of the nearest `head` block left of idx (returns the index of '(')."""
    k = s.rfind(head, 0, idx)
    assert k >= 0, head
    return k + 1


def remove_span(s, a, b):
    """Cut s[a:b] together with the leading newline and indentation."""
    while a > 0 and s[a - 1] in '\t ':
        a -= 1
    if a > 0 and s[a - 1] == '\n':
        a -= 1
    return s[:a] + s[b:]


def num(v):
    t = ('%.2f' % v).rstrip('0').rstrip('.')
    return t if t not in ('-0', '') else '0'


def find_label(s, kind, name, x, y):
    """Index of the label block of kind in {label, global_label, hierarchical_label} with this name and anchor."""
    pat = re.compile(r'\(%s "%s"\s*(?:\(shape \w+\)\s*)?\(at %s %s [-\d.]+\)' % (
        kind, re.escape(name), re.escape(num(x)), re.escape(num(y))))
    m = pat.search(s)
    return m.start() if m else -1


def find_nc(s, x, y):
    m = re.search(r'\(no_connect\s*\(at %s %s\)' % (re.escape(num(x)), re.escape(num(y))), s)
    return m.start() if m else -1


def symbol_block_by_ref(s, ref):
    """(a, b) of the symbol block with Reference == ref, verified to be that symbol's block.

    An earlier version found the block start by the pattern `'\n\t(symbol\n'` and, for symbols
    inserted by scripts without a newline before `\t(symbol`, returned the block of the PREVIOUS
    component, since the nearest match to the left belonged to it. Fields were then read from,
    and would be written into, the neighbouring component.

    So the block start is found by indentation without requiring a newline, and the found block
    is then CHECKED to carry the requested Reference as its first Reference.
    """
    i = s.index('(property "Reference" "%s"' % ref)
    k = s.rfind('\t(symbol\n', 0, i)
    assert k >= 0, ('symbol block start not found', ref)
    a, b = span(s, k + 1)
    blk = s[a:b]
    first = re.search(r'\(property "Reference" "([^"]*)"', blk)
    if not first or first.group(1) != ref:
        raise AssertionError(
            'symbol block %r not found: offset %d holds block %r. The usual cause is a symbol '
            'inserted without a newline before the tab and "(symbol"; fix the formatting with '
            'tools/fix_sch_indent.py' % (ref, a, first.group(1) if first else None))
    return a, b


def remove_label(s, kind, name, x, y):
    i = find_label(s, kind, name, x, y)
    assert i >= 0, (kind, name, x, y)
    a, b = span(s, i)
    return remove_span(s, a, b)


def remove_nc(s, x, y):
    i = find_nc(s, x, y)
    assert i >= 0, ('no_connect', x, y)
    a, b = span(s, i)
    return remove_span(s, a, b)


def glabel_from_template(tpl, name, x, y, rot, shape='passive', justify=None):
    """New global_label modelled on the existing block tpl."""
    t = re.sub(r'\(global_label "[^"]*"', '(global_label "%s"' % name, tpl, count=1)
    t = re.sub(r'\(shape \w+\)', '(shape %s)' % shape, t, count=1)
    t = re.sub(r'\(at [-\d.]+ [-\d.]+ [-\d.]+\)', '(at %s %s %d)' % (num(x), num(y), rot), t)
    t = re.sub(r'\(uuid "[^"]*"\)', '(uuid "%s")' % uuid.uuid4(), t, count=1)
    if justify:
        t = re.sub(r'\(justify \w+\)', '(justify %s)' % justify, t, count=1)
    return t


def no_connect(x, y):
    return '\t(no_connect\n\t\t(at %s %s)\n\t\t(uuid "%s")\n\t)\n' % (num(x), num(y), uuid.uuid4())


def prop(name, val, x, y, hide=False, rot=0, justify='left'):
    return ('\t\t(property "%s" "%s"\n\t\t\t(at %s %s %d)\n%s\t\t\t(show_name no)\n'
            '\t\t\t(do_not_autoplace no)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n'
            '\t\t\t\t(justify %s)\n\t\t\t)\n\t\t)\n'
            % (name, val, num(x), num(y), rot, '\t\t\t(hide yes)\n' if hide else '', justify))


def symbol_instance(lib_id, x, y, rot, sym_uuid, props, pin_numbers, instances, dnp=False):
    """Placed symbol block. props: list of tuples for prop(); instances: ready-made text."""
    pins = ''.join('\t\t(pin "%s"\n\t\t\t(uuid "%s")\n\t\t)\n' % (n, uuid.uuid4()) for n in pin_numbers)
    return ('\t(symbol\n\t\t(lib_id "%s")\n\t\t(at %s %s %d)\n\t\t(unit 1)\n'
            '\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp %s)\n'
            '\t\t(uuid "%s")\n' % (lib_id, num(x), num(y), rot, 'yes' if dnp else 'no', sym_uuid)
            + ''.join(prop(*p) for p in props) + pins + instances + '\t)\n')


def insert_before_tail(s, text):
    """Insert text before (embedded_fonts ...) or before the last ')'."""
    k = s.rindex('\n\t(embedded_fonts') if '\n\t(embedded_fonts' in s else s.rindex('\n)')
    return s[:k] + '\n' + text.rstrip('\n') + s[k:]


def replace_cached_symbol(s, old_lib_id, new_lib_id, new_sym_text):
    """In the sheet's lib_symbols remove old_lib_id (if present) and add new_sym_text as new_lib_id.
    new_sym_text is the stock symbol text (top-level name and sub-symbols without library prefix)."""
    if old_lib_id and '(symbol "%s"' % old_lib_id in s:
        a, b = span(s, s.index('(symbol "%s"' % old_lib_id))
        s = remove_span(s, a, b)
    bare = new_lib_id.split(':', 1)[1]
    assert new_sym_text.startswith('(symbol "%s"' % bare), new_sym_text[:60]
    t = new_sym_text.replace('(symbol "%s"' % bare, '(symbol "%s"' % new_lib_id, 1)
    assert t.count('(symbol "%s' % new_lib_id.split(':')[0] + ':') == 1
    k = s.index('(lib_symbols') + len('(lib_symbols')
    return s[:k] + '\n\t\t' + t.replace('\n', '\n\t') + s[k:]


def extract_symbol(lib_path, name):
    """Text of symbol `name` from a .kicad_sym library file."""
    s = open(lib_path).read()
    i = s.index('(symbol "%s"' % name)
    a, b = span(s, i)
    return s[a:b]


def check_balance(s):
    d, q = 0, False
    for ch in s:
        if q:
            q = ch != '"'
        elif ch == '"':
            q = True
        elif ch == '(':
            d += 1
        elif ch == ')':
            d -= 1
    assert d == 0, 'bracket balance %d' % d


def symbol_pins(sym_text):
    """[(number, name, x, y, rot, type)] of all symbol pins (all sub-symbols)."""
    out = []
    for m in re.finditer(r'\(pin (\w+) \w+\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', sym_text):
        a, b = span(sym_text, m.start())
        blk = sym_text[a:b]
        nm = re.search(r'\(name "([^"]*)"', blk)
        no = re.search(r'\(number "([^"]*)"', blk)
        out.append((no.group(1), nm.group(1), float(m.group(2)), float(m.group(3)), float(m.group(4)), m.group(1)))
    return out


def pin_end(x0, y0, px, py, rot=0):
    """Sheet coordinate of a pin end for a symbol at (x0, y0) rotated by rot (0/90/180/270)."""
    if rot == 0:
        return x0 + px, y0 - py
    if rot == 90:
        return x0 + py, y0 + px
    if rot == 180:
        return x0 - px, y0 + py
    if rot == 270:
        return x0 - py, y0 - px
    raise ValueError(rot)


def label_from_template(tpl, name, x, y, rot, justify='left'):
    """New local label modelled on the block tpl."""
    t = re.sub(r'\(label "[^"]*"', '(label "%s"' % name, tpl, count=1)
    t = re.sub(r'\(at [-\d.]+ [-\d.]+ [-\d.]+\)', '(at %s %s %d)' % (num(x), num(y), rot), t)
    t = re.sub(r'\(uuid "[^"]*"\)', '(uuid "%s")' % uuid.uuid4(), t, count=1)
    t = re.sub(r'\(justify \w+\)', '(justify %s)' % justify, t, count=1)
    return t


def set_property(sym_text, name, value):
    """Replace or add a property inside a library symbol text."""
    pat = re.compile(r'\(property "%s" "[^"]*"' % re.escape(name))
    if pat.search(sym_text):
        return pat.sub('(property "%s" "%s"' % (name, value), sym_text, count=1)
    k = sym_text.index('\n\t\t(property ')
    return sym_text[:k] + '\n\t\t(property "%s" "%s"\n\t\t\t(at 0 0 0)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)' % (name, value) + sym_text[k:]


def add_to_project_lib(lib_path, sym_text):
    """Add a symbol (name without prefix) to the project library if it is not there yet."""
    s = open(lib_path).read()
    name = re.match(r'\(symbol "([^"]*)"', sym_text).group(1)
    if '(symbol "%s"' % name in s:
        return False
    k = s.rstrip().rfind(')')
    s = s[:k].rstrip('\n') + '\n\t' + sym_text.replace('\n', '\n\t') + '\n)\n'
    open(lib_path, 'w').write(s)
    return True


def rename_label(s, kind, old, new, x, y, shape=None):
    """Rename the label of kind `old` at (x, y); with shape, also change its shape."""
    i = find_label(s, kind, old, x, y)
    assert i >= 0, (kind, old, x, y)
    a, b = span(s, i)
    t = s[a:b].replace('(%s "%s"' % (kind, old), '(%s "%s"' % (kind, new), 1)
    if shape:
        t = re.sub(r'\(shape \w+\)', '(shape %s)' % shape, t, count=1)
    return s[:a] + t + s[b:]


def assert_empty(s, x1, y1, x2, y2):
    """Assert that a sheet window holds no symbols, labels, wires or NC flags."""
    hits = []
    for m in re.finditer(r'\(symbol\s*\(lib_id "[^"]*"\)\s*\(at ([-\d.]+) ([-\d.]+)', s):
        if x1 <= float(m.group(1)) <= x2 and y1 <= float(m.group(2)) <= y2:
            hits.append('symbol@%s,%s' % (m.group(1), m.group(2)))
    for m in re.finditer(r'\((?:global_label|label|hierarchical_label) "([^"]*)"\s*(?:\(shape \w+\)\s*)?\(at ([-\d.]+) ([-\d.]+)', s):
        if x1 <= float(m.group(2)) <= x2 and y1 <= float(m.group(3)) <= y2:
            hits.append('label %s@%s,%s' % (m.group(1), m.group(2), m.group(3)))
    for m in re.finditer(r'\(wire\s*\(pts\s*\(xy ([-\d.]+) ([-\d.]+)\)\s*\(xy ([-\d.]+) ([-\d.]+)\)', s):
        for gx, gy in ((1, 2), (3, 4)):
            if x1 <= float(m.group(gx)) <= x2 and y1 <= float(m.group(gy)) <= y2:
                hits.append('wire@%s,%s' % (m.group(gx), m.group(gy)))
    for m in re.finditer(r'\(no_connect\s*\(at ([-\d.]+) ([-\d.]+)\)', s):
        if x1 <= float(m.group(1)) <= x2 and y1 <= float(m.group(2)) <= y2:
            hits.append('nc@%s,%s' % (m.group(1), m.group(2)))
    assert not hits, 'window (%s,%s)-(%s,%s) is occupied: %s' % (x1, y1, x2, y2, hits[:6])


def flattened_derived(lib_path, name):
    """Flattened text of a derived symbol (extends): parent body + child properties, with
    sub-symbols renamed after the child, as KiCad writes them into the sheet cache."""
    child = extract_symbol(lib_path, name)
    m = re.search(r'\(extends "([^"]*)"\)', child)
    if not m:
        return child
    parent = extract_symbol(lib_path, m.group(1))
    pname = m.group(1)
    t = parent.replace('(symbol "%s"' % pname, '(symbol "%s"' % name, 1)
    t = t.replace('(symbol "%s_' % pname, '(symbol "%s_' % name)
    for pm in re.finditer(r'\(property "([^"]*)" "([^"]*)"', child):
        t = set_property(t, pm.group(1), pm.group(2))
    return t


def wire(x1, y1, x2, y2):
    return ('\t(wire\n\t\t(pts\n\t\t\t(xy %s %s) (xy %s %s)\n\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid "%s")\n\t)\n'
            % (num(x1), num(y1), num(x2), num(y2), uuid.uuid4()))


def instances_block(sheet_uuid, ref, root_uuid, project=None):
    """(instances ...) of a placed symbol. root_uuid is the uuid of the root sheet;
    project defaults to the project name (paths.NAME)."""
    if project is None:
        import paths
        project = paths.NAME
    return ('\t\t(instances\n\t\t\t(project "%s"\n\t\t\t\t(path "/%s/%s"\n\t\t\t\t\t(reference "%s")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
            % (project, root_uuid, sheet_uuid, ref))


def add_cached_symbol(s, lib_id, sym_text):
    """Add a symbol to the sheet cache if missing (name with prefix, sub-symbols without)."""
    if '(symbol "%s"' % lib_id in s:
        return s
    return replace_cached_symbol(s, None, lib_id, sym_text)
