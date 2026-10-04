#!/usr/bin/env python3
"""Install a local symbol into the project library and into a sheet's symbol cache.

The sheet cache is mandatory: without it KiCad cannot resolve the symbol and silently
drops the component from the netlist. Sub-symbols in the cache are named WITHOUT the
library prefix (as KiCad itself writes them: "C_0_1", not "Device:C_0_1"); with the
prefix the symbol does not resolve either, again silently.

    python3 tools/install_symbol.py --sheet mcu.kicad_sch --symbol work/my_symbol.txt \\
        --lib lib/MyLib.kicad_sym [--stock Device:L,Device:C]

--symbol is a text file holding one `(symbol "NAME" ...)` block with the name without
prefix; the cached name is "<lib nickname>:NAME", the nickname being the --lib file stem.
--stock adds stock KiCad symbols ($KICAD_SYMBOL_DIR, default /usr/share/kicad/symbols)
to the sheet cache when they are missing.
"""
import sys, os, re, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp
import paths
ROOT = paths.KAT_ROOT
SYM_DIR = os.environ.get('KICAD_SYMBOL_DIR', '/usr/share/kicad/symbols')


def span(s, start):
    j, d, q = start, 0, False
    while j < len(s):
        c = s[j]
        if q:
            if c == '\\':
                j += 2; continue
            if c == '"':
                q = False
        else:
            if c == '"':
                q = True
            elif c == '(':
                d += 1
            elif c == ')':
                d -= 1
                if d == 0:
                    return j + 1
        j += 1
    raise ValueError('unclosed block')


def replace_symbol(text, qualified, body):
    """Replace (symbol "qualified" ...) with body, or insert it if absent."""
    tag = '(symbol "%s"' % qualified
    i = text.find(tag)
    if i >= 0:
        return text[:i] + body + text[span(text, i):], 'replaced'
    k = text.rstrip().rfind(')')
    return text.rstrip()[:k] + '\n\t' + body.replace('\n', '\n\t') + '\n)\n', 'inserted'


def stock_symbol(lib_file, name):
    txt = open(lib_file).read()
    i = txt.find('(symbol "%s"' % name)
    if i < 0:
        raise SystemExit('%s not found in %s' % (name, lib_file))
    return txt[i:span(txt, i)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sheet', required=True, help='sheet file, relative to the project dir')
    ap.add_argument('--symbol', required=True, help='text file with the (symbol "NAME" ...) block')
    ap.add_argument('--lib', required=True, help='project symbol library (.kicad_sym)')
    ap.add_argument('--stock', default='', help='comma-separated stock symbols to cache, e.g. Device:L')
    a = ap.parse_args()
    sch_path = os.path.join(ROOT, a.sheet)
    body = open(os.path.join(ROOT, a.symbol)).read().strip()
    name = re.match(r'\(symbol "([^"]+)"', body).group(1)

    # 1) project library: name without prefix
    lp = os.path.join(ROOT, a.lib)
    nick = os.path.splitext(os.path.basename(lp))[0]
    lib = open(lp).read()
    lib, how = replace_symbol(lib, name, body)
    open(lp, 'w').write(lib)
    syms = [x[1] for x in sexp.parse(open(lp).read())[0]
            if isinstance(x, list) and x and x[0] == 'symbol']
    print('%s: symbol %s, %d in total' % (a.lib, how, len(syms)))

    # 2) sheet cache: top level with prefix, sub-symbols without
    cached = body.replace('(symbol "%s"' % name,
                          '(symbol "%s:%s"' % (nick, name), 1)
    sch = open(sch_path).read()
    sch, how = replace_symbol(sch, '%s:%s' % (nick, name), cached)
    print('%s: symbol cache %s' % (a.sheet, how))

    # 3) stock symbols missing from the sheet cache
    ls = sexp.find(sexp.parse(sch)[0], 'lib_symbols')[0]
    have = {x[1] for x in ls if isinstance(x, list) and x and x[0] == 'symbol'}
    stock = [q for q in a.stock.split(',') if q]
    for qualified, libfile in [(q, os.path.join(SYM_DIR, q.split(':')[0] + '.kicad_sym')) for q in stock]:
        if qualified in have:
            print('   %s already cached' % qualified)
            continue
        bare = qualified.split(':')[1]
        blk = stock_symbol(libfile, bare)
        blk = blk.replace('(symbol "%s"' % bare, '(symbol "%s"' % qualified, 1)
        # sub-symbols stay without prefix, as KiCad writes them
        i = sch.find('(lib_symbols')
        j = i + len('(lib_symbols')
        sch = sch[:j] + '\n\t\t' + blk.replace('\n', '\n\t\t') + sch[j:]
        print('   %s added to the sheet cache' % qualified)

    sexp.parse(sch)
    open(sch_path, 'w').write(sch)
    ls = sexp.find(sexp.parse(open(sch_path).read())[0], 'lib_symbols')[0]
    print('symbols in the sheet cache: %s' % [x[1] for x in ls
          if isinstance(x, list) and x and x[0] == 'symbol'])


if __name__ == '__main__':
    main()
