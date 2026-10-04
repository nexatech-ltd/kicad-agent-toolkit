"""Decisive router-engine test: does it treat (type protect) copper as an obstacle?

Why. The incremental ONLY_NETS_FILE mode is the only way to close a board monotonically,
in batches of nets, without losing the routing accumulated so far. It was believed not to
work on Freerouting 2.1.0: the router does not modify (type protect) copper, but seemed not
to route around it either, laying tracks on top (a real board showed 91 shorting_items,
40 clearance, 31 tracks_crossing). Judging this on a real board is expensive - a round
takes 15 minutes, and the cause has to be read out of the acceptance report.

Test. A 20 x 20 mm two-layer board. Net NET1 runs from a pad at (2, 10) to a pad at (18, 10).
Across the board on F.Cu stands a 0.5 mm wide wall from edge to edge - copper of another
net with (type protect). It cannot be bypassed on F.Cu: 0.1 mm is left at the wall ends
against the required 0.25 + 2 x 0.15 = 0.55 mm.

  routed straight through the wall on F.Cu  -> protect is not an obstacle
  went to B.Cu through vias                 -> protect is an obstacle, incremental mode works

Run:  python3 tools/test_protect.py work/fr.jar work/fr-2.4.1.jar
JAVA selects the Java binary (default: java on PATH).
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import os
import re
import subprocess
import sys

U = 10000                      # DSN units per millimetre at (resolution um 10)
W, H = 20.0, 20.0              # board size, mm
WALL_X = 10.0                  # wall position, mm
PAD_Y = 10.0
JAVA = os.environ.get('JAVA', 'java')
OUT_SCALE = 10                 # the SES is exactly 10x larger than the input at the same (resolution)


def mm(v):
    return int(round(v * U))


DSN = """(pcb protect_test.dsn
  (parser
    (string_quote ")
    (space_in_quoted_tokens on)
    (host_cad "KiCad")
    (host_version "10.0.6")
  )
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu
      (type signal)
      (property (index 0))
    )
    (layer B.Cu
      (type signal)
      (property (index 1))
    )
    (boundary
      (path pcb 0 0 0 {w} 0 {w} {mh} 0 {mh} 0 0)
    )
    (via PS_VIA_800_400)
    (rule
      (width 2500)
      (clearance 1500)
      (clearance 1500 (type default_smd))
      (clearance 1500 (type smd_smd))
    )
  )
  (placement
    (component IMG_PAD
      (place P1 {p1x} {pady} front 0)
      (place P2 {p2x} {pady} front 0)
    )
  )
  (library
    (image IMG_PAD
      (pin PS_PAD 1 0 0)
    )
    (padstack PS_PAD
      (shape (circle F.Cu 8000))
      (attach off)
    )
    (padstack PS_VIA_800_400
      (shape (circle F.Cu 8000))
      (shape (circle B.Cu 8000))
      (attach off)
    )
  )
  (network
    (net NWALL)
    (net NET1
      (pins P1-1 P2-1)
    )
    (class kicad_default NWALL NET1
      (circuit (use_via PS_VIA_800_400))
      (rule
        (width 2500)
        (clearance 1500)
      )
    )
  )
  (wiring
    (wire (path F.Cu 5000 {wx} {wy0} {wx} {wy1}) (net NWALL) (type protect))
  )
)
""".format(w=mm(W), mh=-mm(H), p1x=mm(2.0), p2x=mm(18.0), pady=-mm(PAD_Y),
           wx=mm(WALL_X), wy0=-mm(0.1), wy1=-mm(H - 0.1))

_WIRE = re.compile(r'\(wire\s*\(path\s+(\S+)\s+(\d+)((?:\s+-?\d+)+)\s*\)', re.S)


def verdict(ses_text):
    """Parse the SES and decide whether NET1 copper crossed the wall on F.Cu."""
    crossings, vias, layers, paths = [], 0, set(), []
    vias = len(re.findall(r'\(via\s', ses_text))
    for layer, width, coords in _WIRE.findall(ses_text):
        pts = [int(v) for v in coords.split()]
        xy = [(pts[i] / (U * OUT_SCALE), -pts[i + 1] / (U * OUT_SCALE))
              for i in range(0, len(pts) - 1, 2)]
        layers.add(layer)
        paths.append('%-5s w=%.2f mm  %s' % (layer, int(width) / U / OUT_SCALE,
                     ' -> '.join('(%.1f,%.1f)' % q for q in xy)))
        if layer != 'F.Cu':
            continue
        for (x1, _), (x2, _) in zip(xy, xy[1:]):
            if (x1 - WALL_X) * (x2 - WALL_X) < 0:      # the segment crossed the wall line
                crossings.append((round(x1, 2), round(x2, 2)))
    return crossings, vias, layers, paths


def run(jar, work):
    dsn = os.path.join(work, 'protect_test.dsn')
    ses = os.path.join(work, 'protect_test_%s.ses' % os.path.basename(jar).replace('.jar', ''))
    with open(dsn, 'w') as f:
        f.write(DSN)
    if os.path.exists(ses):
        os.unlink(ses)
    env = dict(os.environ, FREEROUTING__GUI__ENABLED='false')
    r = subprocess.run([JAVA, '-Djava.awt.headless=true', '-jar', os.path.abspath(jar),
                        '-de', dsn, '-do', ses, '-mp', '4'],
                       capture_output=True, text=True, cwd=work, env=env, timeout=600)
    if not os.path.exists(ses):
        return None, (r.stdout + r.stderr)[-1500:]
    with open(ses) as f:
        return f.read(), None


def main():
    jars = sys.argv[1:] or ['work/fr.jar']
    work = os.path.dirname(os.path.abspath(jars[0])) or '.'
    for jar in jars:
        print('=' * 72)
        print('engine:', jar)
        text, err = run(jar, work)
        if text is None:
            print('  NO RESULT. Engine output:\n', err)
            continue
        crossings, vias, layers, paths = verdict(text)
        print('  layers in result: %s | vias: %d' % (sorted(layers), vias))
        for ln in paths:
            print('   ', ln)
        if crossings:
            print('  WALL CROSSINGS on F.Cu: %d  %s' % (len(crossings), crossings[:4]))
            print('  VERDICT: protect is NOT an obstacle - incremental mode is unusable')
        elif 'B.Cu' in layers and vias >= 2:
            print('  VERDICT: protect IS an obstacle - went to B.Cu through vias, mode is usable')
        else:
            print('  VERDICT inconclusive: did not cross the wall, but did not go to B.Cu either.')
            print('  Check manually:', os.path.join(work, 'protect_test.ses'))


if __name__ == '__main__':
    main()
