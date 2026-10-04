"""The "window, target net first" technique for a locked connection end.

Usage: python3 tools/util/window_first.py BOARD.kicad_pcb x0,y0,x1,y1 NET [--apply]

Removes all copper lying entirely inside the window around the locked end; then route NET
first and restore the other nets of the window afterwards. DRC (run separately) is the judge.
The window's other nets are written to $KAT_WORK/win_nets.txt for a follow-up
route.py --only-net run. Keep the window narrow: a wide window rips many nets and the
restore pass may leave more breaks than it fixes.
"""

import sys

if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0 if len(sys.argv) > 1 else 2)
import sys, os, pcbnew
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import paths
MM = 1e6
board = sys.argv[1]
x0, y0, x1, y1 = (float(v) for v in sys.argv[2].split(','))
net = sys.argv[3]
apply = '--apply' in sys.argv
b = pcbnew.LoadBoard(board)
dead = []
for t in b.GetTracks():
    if t.GetClass() == 'PCB_VIA':
        p = t.GetPosition()
        if x0 <= p.x / MM <= x1 and y0 <= p.y / MM <= y1:
            dead.append(t)
    else:
        s, e = t.GetStart(), t.GetEnd()
        if all(x0 <= q.x / MM <= x1 and y0 <= q.y / MM <= y1 for q in (s, e)):
            dead.append(t)
nets = sorted({t.GetNetname().split('/')[-1] for t in dead if t.GetNetname()})
print('window %.1f,%.1f..%.1f,%.1f: removing %d items, %d nets' % (x0, y0, x1, y1, len(dead), len(nets)))
print('window nets:', ', '.join(nets))
out = paths.work('win_nets.txt')
try:
    with open(out, 'w') as fh:
        fh.write('\n'.join(n for n in nets if n and n != net) + '\n')
    print('other window nets ->', out)
except OSError:
    pass
if apply and dead:
    for t in dead:
        b.Remove(t)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(board, b)
    print('removed, written', board)
