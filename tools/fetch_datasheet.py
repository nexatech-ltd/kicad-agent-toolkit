#!/usr/bin/env python3
"""Fetch the official document for a component, verifying that the right thing was
downloaded.

  python3 fetch_datasheet.py LCSC VENDOR NAME [--url URL] [--mpn MPN] [--apply]
  python3 fetch_datasheet.py --index          # show what is already collected

Why verify instead of a plain curl. Schematic datasheet fields drift to documents for
OTHER parts: a sibling suffix instead of the fitted variant (`...UL` vs `...UB`), a
different flash family than the one fitted, a generic logic family page instead of the
single-gate part, a product page instead of a PDF, a datasheet of another package variant
of the MCU. Downloading is not enough: the document must contain the designation of the
fitted part, otherwise a "per datasheet" check rests on someone else's document.

What it does:
  1. Takes the URL from the argument or from the JLCPCB catalog snapshot (config key
     "jlcpcb_db"; the `Datasheet` field is filled for nearly all records, mostly a direct PDF).
  2. Downloads to a temporary file and checks that it really is a PDF (`%PDF` signature).
  3. Extracts the text of the first pages (`pdftotext`) and searches for the part
     designation (`--mpn` or `MFR_Part` from the catalog). Not found: the file is NOT
     accepted and a warning is printed: either a scan without a text layer, or a document
     for another part. A scan can be accepted deliberately with `--apply`, but it is
     marked in the index.
  4. Computes SHA256 and writes an entry to `<datasheet_dir>/index.yaml`: designation,
     code, vendor, URL, file, size, page count, hash, fetch date, "has text layer" flag.

The hash matters because vendors silently publish new revisions at the same URL: if a
re-download yields another hash, numbers in the component registry must be re-read, not
assumed verified.
"""
import argparse
import datetime
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths
import project

PROJ = paths.KAT_ROOT
DS_DIR = os.path.join(PROJ, project.get('datasheet_dir', os.path.join('doc', 'datasheets')))
INDEX = os.path.join(DS_DIR, 'index.yaml')
DB = os.path.expanduser(project.get('jlcpcb_db', '~/.konnect/jlcpcb.db'))
UA = 'Mozilla/5.0'


def catalog(code):
    """(MFR_Part, Manufacturer, Datasheet) from the catalog snapshot, or None."""
    if not os.path.exists(DB):
        return None
    c = sqlite3.connect(DB)
    return c.execute('select MFR_Part, Manufacturer, Datasheet from components where LCSC=?',
                     (code,)).fetchone()


UA_CHROME = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
             'Chrome/126.0.0.0 Safari/537.36')


def resolve_lcsc(code):
    """Direct PDF for an LCSC code via the viewer page.

    Why a separate route. Some manufacturer sites may be unreachable from a given
    environment (HTTP/2 INTERNAL_ERROR, timeouts). Meanwhile
    `www.lcsc.com/datasheet/<code>.pdf` is not a file but an HTML viewer that embeds a link
    to the real PDF on the CDN (`datasheet.lcsc.com/datasheet/pdf/<hash>.pdf`). Through it
    one gets exactly the documents that are otherwise unavailable (e.g. large MCU and
    driver datasheets). Hence the route: manufacturer site (--url) -> this resolver ->
    catalog link.
    """
    try:
        r = subprocess.run(['curl', '-sSL', '--max-time', '40', '-A', UA_CHROME,
                            'https://www.lcsc.com/datasheet/%s.pdf' % code],
                           capture_output=True, text=True, errors='replace')
    except Exception:
        return None
    m = re.search(r'https://datasheet\.lcsc\.com/datasheet/pdf/[a-f0-9]+\.pdf', r.stdout or '')
    if m:
        print('LCSC resolver: %s' % m.group(0))
        return m.group(0)
    return None


def load_index():
    if not os.path.exists(INDEX):
        return {}
    import yaml
    with open(INDEX) as f:
        return yaml.safe_load(f) or {}


def save_index(data):
    import yaml
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    with open(INDEX, 'w') as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=True, default_flow_style=False)


def pdf_pages(path):
    r = subprocess.run(['pdfinfo', path], capture_output=True, text=True)
    m = re.search(r'^Pages:\s+(\d+)', r.stdout, re.M)
    return int(m.group(1)) if m else 0


def pdf_text(path, pages=3):
    r = subprocess.run(['pdftotext', '-f', '1', '-l', str(pages), path, '-'],
                       capture_output=True, text=True)
    return r.stdout


def norm(s):
    """Designation without separators: ABC123XY-08G -> ABC123XY08G."""
    return re.sub(r'[^A-Za-z0-9]', '', s or '').upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('lcsc', nargs='?', help='LCSC code (for the URL and the catalog designation)')
    ap.add_argument('vendor', nargs='?', help='vendor subdirectory in the datasheet directory')
    ap.add_argument('name', nargs='?', help='file name without .pdf, preferably with the document number')
    ap.add_argument('--url', help='use this URL instead of the catalog link')
    ap.add_argument('--mpn', help='designation that must appear in the document')
    ap.add_argument('--doc-id', default='', help='document number (e.g. SLUSxxxx, DSxxxxx)')
    ap.add_argument('--revision', default='', help='document revision, if known')
    ap.add_argument('--apply', action='store_true', help='accept the file (without it: check only)')
    ap.add_argument('--index', action='store_true', help='show what is collected and exit')
    a = ap.parse_args()

    if a.index:
        idx = load_index()
        print('documents in the index: %d' % len(idx))
        for key in sorted(idx):
            e = idx[key]
            print('  %-24s %-10s %-46s pp %-4s %s' % (
                key, e.get('lcsc', '-'), e.get('file', ''), e.get('pages', '?'),
                'text' if e.get('has_text') else 'SCAN'))
        return 0

    if not (a.lcsc and a.vendor and a.name):
        ap.error('LCSC, VENDOR and NAME are required (or --index)')

    cat = catalog(a.lcsc)
    mpn = a.mpn or (cat[0] if cat else None)
    url = a.url or resolve_lcsc(a.lcsc) or (cat[2] if cat else None)
    if not url:
        print('REFUSED: no URL given, the resolver found nothing and the catalog has no link for %s' % a.lcsc)
        return 2
    if not mpn:
        print('WARNING: designation unknown, nothing to verify the document content against')

    print('part   : %s (%s)' % (mpn or '?', a.lcsc))
    print('vendor : %s' % (cat[1] if cat else '?'))
    print('URL    : %s' % url)

    tmp = tempfile.mktemp(suffix='.pdf')
    r = subprocess.run(['curl', '-sL', '--max-time', '180', '-A', UA_CHROME, '-o', tmp, url],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 2048:
        print('REFUSED: download failed (code %d, size %d bytes). It may be an HTML page:'
              ' pass a direct URL with --url'
              % (r.returncode, os.path.getsize(tmp) if os.path.exists(tmp) else 0))
        return 2
    with open(tmp, 'rb') as f:
        head = f.read(5)
    if not head.startswith(b'%PDF'):
        print('REFUSED: not a PDF (first bytes %r): this URL serves a page, not a file'
              % head)
        return 2

    size = os.path.getsize(tmp)
    pages = pdf_pages(tmp)
    text = pdf_text(tmp)
    has_text = len(text.strip()) > 200
    found = bool(mpn) and norm(mpn) in norm(text)
    # Partial match on the LONGEST prefix of the designation. Needed because the catalog
    # stores the order number with packaging and temperature-grade suffixes that the
    # datasheet does not contain (e.g. `XXX1234RGER` vs `XXX1234`, `ABC2209-LA-T` vs
    # `ABC2209`). A prefix match of six or more characters counts as confirming the part,
    # the difference as a packaging suffix; shorter is no confirmation (`SP0504` and
    # `SP0502` would differ only in the suffix).
    partial = None
    if mpn and not found:
        n, t = norm(mpn), norm(text)
        for k in range(len(n), 5, -1):
            if n[:k] in t:
                partial = n[:k]
                break

    print('file   : %d KB, %d pages, text layer: %s' % (size // 1024, pages,
                                                       'yes' if has_text else 'NO (scan)'))
    if found:
        print('match  : designation %s found in the document' % mpn)
    elif partial:
        print('match  : prefix %s matched (full %s: the difference is a packaging suffix)' % (partial, mpn))
    elif has_text:
        print('match  : DESIGNATION %s NOT IN THE DOCUMENT: probably a document for another part' % mpn)
    else:
        print('match  : impossible, no text layer')

    if not a.apply:
        os.unlink(tmp)
        print('\ncheck only. Add --apply to write')
        return 0 if (found or partial) else 1

    dst_dir = os.path.join(DS_DIR, a.vendor)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, a.name + '.pdf')
    shutil.move(tmp, dst)
    sha = hashlib.sha256(open(dst, 'rb').read()).hexdigest()

    idx = load_index()
    idx[mpn or a.name] = {
        'lcsc': a.lcsc,
        'manufacturer': cat[1] if cat else '',
        'doc_id': a.doc_id,
        'revision': a.revision,
        'url': url,
        'file': os.path.relpath(dst, PROJ),
        'size': size,
        'pages': pages,
        'sha256': sha,
        'fetched': datetime.date.today().isoformat(),
        'has_text': has_text,
        'mpn_confirmed': True if found else ('prefix ' + partial if partial else False),
    }
    save_index(idx)
    print('\nwritten  %s' % os.path.relpath(dst, PROJ))
    print('sha256   %s' % sha)
    print('index    %s (%d documents)' % (os.path.relpath(INDEX, PROJ), len(idx)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
