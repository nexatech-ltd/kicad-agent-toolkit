# Routing pipeline

How the routing tools fit together, from the first fan-out to the last break. All tools are in
`tools/` (GPU router in `tools/gpuroute/`, ad-hoc geometry helpers in `tools/util/`). Paths
resolve through `tools/paths.py` / `tools/env.sh`: `KAT_ROOT` is the project directory, `KAT_WORK`
the work directory (default `$KAT_ROOT/work`), and `BOARD` (or `BRD`) the board file. With
`BOARD` and `KAT_WORK` pointed at a copy, any campaign runs on that copy.

Project-specific facts (plane nets, protected nets such as an RF feed, fine-pitch references,
class clearances) come from `kicad-agent.json` in the project (`KAT_CONFIG`) and from the
board's `.kicad_dru`. See `examples/kicad-agent.json`.

Acceptance is the same at every stage: KiCad DRC (`--severity-all`, zones refilled) is the
judge, and a step is kept only if breaks went down and significant violations did not go up from
zero. See [methodology.md](methodology.md).

## Overview

```
placement check ──► fan-out ──► bulk routing ──► last breaks ──► plane pads ──► widen ──► stitch/release
 blockers.py        strip_annulus  campaign.py     maze_route.py   fix_plane_pads   widen_segs   stitch_gnd.py
 locked.py          fanout.py      gpu_campaign.sh  reach.py        plane_via_stub   check_ir_drop refill_zones
                    prune_new.py   route_all.sh     ripup.py        free_pad.py                    gate.sh
                                   route_gpu_all.sh edit_ops.py     island_pads.py
```

| Engine | Best at | Weak at |
|---|---|---|
| Freerouting (stepwise, via REST API) | bulk routing of many nets, long connections; rips up internally | slow per round (~5 min), a second clearance model, can't see raster-invisible gaps, no solution for single nets boxed in by protected copper |
| GPU router (`tools/gpuroute/`, PathFinder on torch) | batches of nets negotiated together, regional rip-up, plane nets as signals | needs a CUDA box, raster model must match DRC rules exactly |
| Maze router (`tools/maze_route.py`, A* per net) | the last few breaks: one net in 0.1–5 s; diagnostics by cell count | no rip-up, stalls on long blocked connections |

Every major jump on a hard board came from fixing the model or the placement, not from more
rounds: per-object-type clearance rules (118 → 110), a smaller via (0.6 → 0.5 mm opened a
fine-pitch corridor), moving four signals to free pins, closing micro-gaps.

## 0. Before routing: can the pins get out?

On packages with pitch ≤ 0.5 mm, measure escape capacity before any campaign:

```bash
python3 tools/blockers.py --board B --drc D            # first obstacle in each pin's radial lane
python3 tools/gpuroute/locked.py --board B --drc D     # break ends the wave cannot leave
python3 tools/gpuroute/probe.py --board B --net N --x X --y Y --r 1.5   # ASCII raster around a point
```

If many pins have no escape, routing rounds will keep answering "no path (1 cell)". Typical
causes, in order of frequency:

1. **The package's own escapes, laid diagonally by earlier campaigns.** In one case 40 routed pins
   locked 82 others. Remove the whole ring and rebuild the fan-out:

   ```bash
   python3 tools/strip_annulus.py --ref U1 --radius 3.2 --any --apply   # --any: copper with ANY point in the ring
   python3 tools/fanout.py --refs U1 --apply                             # radial stub + via per pin, two staggered rows
   python3 tools/prune_new.py --since <snapshot>                         # drop what DRC rejects
   ```

   Take the outward direction from the package **side**, not from its centre (115 vs 76 escapes).
   Without `--any`, long tracks that cross the ring without ending in it stay and keep blocking.
2. **Support parts 0.2 mm from the pin column** (0402 decoupling, shunts) and wide power tracks.
   Nudge them (`tools/gpuroute/nudge.py`, `tools/nudge_part.py`) before growing the board.
3. **Via size.** A straight exit past a neighbour's via at 0.5 mm pitch needs
   `0.5 − d/2 − clearance − w/2 ≥ 0`. With clearance 0.15 and w 0.15 that's −0.025 for d = 0.6
   (impossible), +0.025 for d = 0.5, +0.05 for d = 0.45. Try a campaign on a copy with
   `KAT_VIA=0.5,0.2` before any rip-up, and keep `min_via_diameter` in `.kicad_pro` consistent
   with the fab.
4. **Clearing F.Cu lanes alone doesn't help.** The escape needs via room on *all* layers. One
   attempt cost 42 breaks net.

## 1. Freerouting round

`kicad-cli` can't export Specctra DSN, so the toolkit has its own round-trip.

```bash
python3 tools/pick_nets.py work/cur_drc.json --limit 25 -o work/campaign_nets.txt
ONLY_NETS_FILE=work/campaign_nets.txt python3 tools/dsn_export.py   # board -> DSN + net-name map
bash tools/run_route.sh                                             # export, server, job, grab SES
ONLY_NETS_FILE=work/campaign_nets.txt bash tools/after_ses.sh       # import into work/trial, DRC, verdict
python3 tools/accept.py work/trial/drc.json --apply                 # whole round, if accepted
python3 tools/merge_ses.py --apply                                  # otherwise: per-net merge
```

`tools/campaign.py` runs this unattended:

```bash
python3 tools/campaign.py --rounds 12 --batch 25 --seconds 240 --keep-stitch
```

| Option | Meaning |
|---|---|
| `--batch` | nets per round. **Keep it 20–45.** Measured: 20–45 nets gave 0.3–1.0 closed breaks/min, 120 nets gave 0.00–0.06. A big batch diverges from the board, import collisions pile up, and per-net acceptance rolls nearly everything back. |
| `--seconds` | router time per round |
| `--max-len` | only breaks shorter than this |
| `--full-every N` | every N-th round is a full signal reroute (a different solution to merge from) |
| `--prune-upto N` | take a round whole if it adds ≤ N violations, then prune the flagged copper |
| `--keep-stitch` | **use it.** Don't hide ground stitching from the router (see below). |
| `--drop-plane-vias` | hide all plane vias. **Measured harmful**: plane vias sit exactly where the router needs to go. |

Campaign state (`work/campaign.json`) counts attempts per net, so nets are tried in order
(attempts, break length). Don't delete it between runs.

### Export decisions (`tools/dsn_export.py`)

- Footprint rotations are baked into per-instance images, and parts are placed at angle 0. Pad
  angles in the board file are absolute and default to 0, which is easy to get wrong silently.
- Nets are renamed `N0001…`, with a map file. Hierarchical names contain spaces, commas and
  slashes.
- Plane nets (`plane_nets` in the config) are declared with an empty pin list. Their copper stays
  as an obstacle.
- Keepouts are exported as both `(keepout …)` (conductors) and `(via_keepout …)` (vias). Specctra
  `keepout` alone doesn't forbid vias. Polygon points are read by bracket balance. A regex
  version once exported every keepout at half its area.
- `ONLY_NETS_FILE` (stepwise mode) must be set for **both** export and import. Without it the
  exporter drops ordinary nets' copper (the router starts from scratch: 1095 "unrouted" instead of
  302), and the importer loses copper of nets that weren't routed.
- Never put plane nets, RF nets or `unconnected-*` nets in the routing list. Plane breaks are
  fixed by vias, not tracks: a round that routed GND as tracks produced 105 shorts.
- `DROP_STITCH` / `DROP_PLANE_VIAS` hide vias from the router without removing them from the
  board. **Don't.** Hidden obstacles turn into violations on import: 588 hidden GND vias gave 149
  `shorting_items` per round, and every round was rejected. If stitching blocks routing, remove it
  from the board (`tools/unstitch_walls.py` or a snapshot without stitches) and stitch again at
  the end.

### Import (`tools/ses_import.py`)

- Coordinates in the SES can come back 10× larger than declared. The importer calibrates the scale
  against the `placement` section and fails if the spread exceeds 0.1 %.
- It compares the number of parsed wires with the number declared in the SES and fails on a
  mismatch. A first version parsed zero segments and reported success.
- It keeps plane-net and RF copper from the source board, not from the SES.
- A round-trip test of real board geometry (N segments → synthetic SES → N segments) guards the
  parser.

### Freerouting server notes

- Use the REST API, not the CLI. A job never finishes on its own, and the CLI writes the result
  only at the end. `GET /v1/jobs/{id}/output/stream` (SSE, base64 `data`) returns the current SES
  of a **running** job (`tools/fr_api.py`, `tools/fr_grab.py`). Accumulate `data:` lines and try
  to parse JSON after each one. The stream never closes, so a blocking read hangs.
- **Never cancel a job.** After `/cancel`, the stream returns zero bytes, `/output` still says
  "not finished", and the router thread keeps burning CPU.
- Requests need `Freerouting-Profile-ID` (any UUID) and `Freerouting-Environment-Host` headers.
- 2.4.x: needs a recent JDK (Java 25 for 2.4.1). API authentication must be disabled with the
  CLI flag `--api_server.authentication.enabled=false`, because the environment variable doesn't
  reach it. `FREEROUTING__API_SERVER__ENDPOINTS` never applies (array vs string). The server binds
  to `localhost:37864` by default anyway.
- Reuse a running server between rounds (saves ~30 s). Restart with `FR_RESTART=1`. Kill it by
  process name, not `pkill -f` (see methodology).
- `unrouted` counts in Freerouting logs aren't comparable between modes. Judge by acceptance only.
- Protected copper (`type protect`) **is** an obstacle for Freerouting. `tools/test_protect.py`
  proves it on a 20×20 mm two-layer board.

## 2. GPU router (`tools/gpuroute/`)

| Module | Role |
|---|---|
| `model.py` | obstacle masks per (width, clearance) group: foreign copper on a 0.025 mm sub-raster, Euclidean distance transform, 0.1 mm cells free at ≥ w/2 + clearance + safety. Pad and track clearances are **separate maps**, because `.kicad_dru` class rules often apply only to Track↔Track. |
| `wave.py` | wavefront over all copper layers as iterative relaxation, via stacks in one step, early exit |
| `connectivity.py` | copper components per net (copper planes + via-stack planes, vertical-only links). Check: components − 1 = DRC breaks per net. |
| `negotiate.py` | PathFinder: foreign paths cost `1 + pres·conflicts`, history accumulates, only conflicting nets are re-routed |
| `route.py` | picks breaks from a DRC report, negotiates, writes copper |
| `ripup.py`, `rip_campaign.py`, `rip_ends.py` | regional rip-up around locked ends, campaigns of small rip-ups |
| `locked.py`, `probe.py` | reachability of break ends, ASCII raster around a point |
| `nudge.py`, `nudge_campaign.py`, `qfn_campaign.py` | move support parts to open locked pins, judged per package |
| `widen_board.py`, `spread_row.py` | grow the board / spread a row (only together with nudges) |
| `par.py`, `check_par.py` | threaded CPU stages, bit-exact against the serial path |
| `place/` | placement search with trial routing (L0 reachability, L1 negotiated routing of a pocket) |

```bash
python3 tools/gpuroute/route.py --drc work/cur_drc.json --limit 30 --apply
BATCH=60 ROUNDS=30 RETRY=2 bash tools/gpu_campaign.sh          # rounds with per-net replay
bash tools/route_gpu_all.sh                                     # plane pads -> GPU -> rip-up -> nudges, until no gain
```

`gpu_campaign.sh` / `maze_campaign.sh`: if a round adds errors, DRC names the offending nets.
They're set aside and the round is **replayed** without them (blindly splitting the batch lost ~6
breaks to one bad net). A net set aside three times goes to `work/hard_nets.txt`. `STALL`
consecutive rounds without gain end the campaign so another engine can take over. `route.py
--nopath` excludes nets that had no path, so the queue reaches the longer ones.

Raster accuracy matters. On a 0.5 mm-pitch LQFP the lane between the pin axis and the neighbour
pad is 0.15 mm wide. A 0.1 mm raster with ceil dilation ate it, and 93 of 187 break ends looked
locked. A 0.025 mm sub-raster with EDT brought that to 46, all of them real. The raster step must
be several times finer than the narrowest lane.

Model caches must be rebuilt after mutating the board. A basket of board objects cached by
`id(board)` survived a rip-up that returned copper, and the next model routed through it (18–30
shorts per round).

## 3. Last breaks

When a handful of breaks remain, stop running campaigns and ask geometry questions.

```bash
python3 tools/maze_route.py --list                  # what remains, lengths
python3 tools/maze_route.py --limit 5 --apply       # 0.1-5 s per net
python3 tools/maze_route.py --only-net N --apply    # sensitive net first, on its own
```

### Reading "no path"

The maze router prints `NO PATH (N cells explored)`. Read N:

| N | Meaning | Next tool |
|---|---|---|
| 1–~100 | the **end** is locked: no free neighbour cell | `gpuroute/locked.py`, `gpuroute/probe.py`, `util/diag_free.py` (free-cell map with reasons per layer); rip-up around the end, nudge support parts |
| ~10⁵–10⁶ | the **corridor** is locked: the pad is free, the reachable area doesn't reach the target | `reach.py --path` (who holds the boundary), `corridor_open.py` (iterative list of wall nets on a copy) |
| break never attempted | the model doesn't see it | compare the model with DRC: `close_gaps.py` for gaps narrower than a cell |

```bash
python3 tools/reach.py --board B --net N --from X1,Y1 --to X2,Y2 --wall-r 3.0 --top 15 --path
python3 tools/corridor_open.py --board COPY --net N --from X1,Y1 --to X2,Y2 --iters 12 --protect RF50
python3 tools/keyhole.py --board B --net N --window x0,y0,x1,y1 --via 0.5,0.2 --top 10
python3 tools/util/via_spots.py B N x0,y0,x1,y1 --via 0.5 --need-fill L1,L2 --rank-walls
```

- `keyhole.py` answers "which copper must go for a through via to fit here, and where is that
  cheapest", ranking cells by the number and size of wall nets. Foreign pads, drills, keepouts
  and edge clearance disqualify a cell.
- `via_spots.py` answers "where does a via fit right now" in about a second, across all layers.
  `--need-fill` keeps only points over own-net fill, and `--rank-walls` names the signal nets in
  the way.
- An A* goal must be a free cell. DRC reports a track's `start` as its position, and that start
  may sit inside a fine-pitch pad's dilation. Target the whole connected component of own copper.
- Mask "where a track may go" excludes the net's own holes; mask "where a via may go" does not
  (hole-to-hole applies regardless of net). Otherwise a pre-placed via locks itself in.
- Micro-gaps: two pieces of copper closer than one raster cell form one component for the router
  and a break for DRC. `close_gaps.py --board B --drc D --max-gap 0.8 --apply` lays the missing
  segment, and `--undo` removes those that DRC rejects.

### Rip-up strategy

- **Small radius converges, large radius doesn't.** `ripup.py --at X,Y --radius 4 --window 6`
  took 76 → 60 with zero errors. A radius of 14 mm (89 nets) didn't converge and left 198 shorts.
  Clear a corridor with a chain of small windows. If negotiation doesn't converge, don't write the
  result. Shrink the radius instead.
- **"Remove less until it converges."** A ripped net that fails to reconnect goes back on the
  board as a wall, and negotiation repeats without it (up to three attempts).
- **Rip named nets, not areas.** A 4×5 mm window over a whole fine-pitch package side ripped 14
  nets and turned one break into five. Ripping the three nets named by `via_spots --walls` /
  `island_stitch --relax` closed it with 0/0. Tools: `ripup.py --nets A,B --layers L1,L2`,
  `tools/rip_area.py --nets ... --at X,Y --r R`, `tools/util/rip_at_nets.py`.
- **One-keyhole node: hard net first.** Rip all wall nets of the node at once, route the hard net
  alone (`--only-net`), then route the rest in one pass so the router distributes the keyholes.
- **Electrical priority beats length order.** Route a crystal or other sensitive net in its own
  run before the rest. If the other net then fails, widen the window and negotiate both together.
  Don't flip the order back.
- **Don't rip plane-net copper by default** (`--rip-planes none`). Cut-off plane pads don't all
  reconnect (124 → 129 breaks in one test).
- **Two nets wanting the same via spot:** place the via by hand (`util/via_spots.py` →
  `util/edit_ops.py`), then route with `--any-target` and `KAT_ANY_SIGNAL=1` so the end goes to
  the via, not on a 50 mm detour to the far end.
- **Order for vias placed by a tool:** find the wall nets (`--relax`), rip them, place the vias,
  re-route. If you place first, `SaveBoard` gives the via the net of the copper underneath, and
  ripping that net deletes your via.
- **Window width decides monotonicity.** Snapshot before every window and accept only if breaks
  went down.
- **Policies the router can't know become temporary keepouts.** `keepout_tmp.py --board B --box
  x0,y0,x1,y1 --add` / `--remove`. Size the box by the clash, not with margin: a generous box
  covered routed copper and produced 82 temporary `items_not_allowed`. A keepout also tests a
  hypothesis such as "is there a path the other way?".

Several small tools close classes of dead ends in 1–10 s each, with an explanation: `via_spots`
(room and wall), `ratsnest_pos` (where KiCad sees the break), `edit_ops` / `board_diff_ops` (edit
and transfer), `KAT_ANY_SIGNAL` (target = own piece), `free_pad`, `nudge_part`, signal moves to
free pins. Reach for them before another campaign.

## 4. Plane-net breaks

Plane nets (`plane_nets` in config) aren't routed by Freerouting. A pad without a via into its
plane stays broken forever unless a tool connects it.

| Situation | Tool |
|---|---|
| bare pad, room for a via nearby | `tools/fix_plane_pads.py <drc.json> --apply` |
| pad with a stub that doesn't reach the plane | `tools/plane_via_stub.py --board B --drc D --apply` |
| pad in a fill fragment cut off by tracks | `tools/island_pads.py` then route as a signal (below), or `tools/island_stitch.py` (via inside the fragment) |
| pad boxed in by signal walls | `tools/free_pad.py --board B --nets GND,+3V3 --apply` (rip walls, pad first, walls back) |
| ground pad to plane via a short jumper | `tools/gnd_link.py --board B --net GND --apply` |
| signal end locked | `tools/sig_link.py --board B --drc D --apply`, then `route.py --any-target` |

Route plane nets as signals to the nearest own copper when no via stack fits:

```bash
KAT_PLANE_AS_SIGNAL=1 python3 tools/gpuroute/route.py --drc D --planes --any-target --only-net +3V3 --apply
```

With `KAT_PLANE_AS_SIGNAL=1` the zone fill doesn't count as the net's copper, so a break is
routed by a track to the nearest pad, track or via of the same net within `KAT_MULTI_R` (default
6 mm), without a new via. This closed 7 of 8 remaining plane breaks in one case. Route the plane
net **first** and its neighbours in a second pass. When everything is rerouted together, the
walls come back first. A dangling track with no pads on either side (Track↔Track break) can just
be removed.

Gate `free_pad.py` steps on "signal breaks did not increase". Otherwise it trades a ground
connection for a signal break. Three adjacent 0.5 mm-pitch pins that all need a via are beyond
what one via row can serve.

Diagnosis: DRC "Zone ↔ Zone" items carry the zone anchor as their position. Use
`tools/util/ratsnest_pos.py B NET` (clusters by `GetConnectedItems` after refill) and
`tools/util/zone_islands.py` / `island_where.py` / `in_island.py`.

## 5. After zero breaks: widths, then stitching

1. **Widths.** Class width is project policy. The acceptance criterion is current:
   `tools/check_ir_drop.py` (per-layer copper thickness, real average current for cross-section,
   peak current for drop, judged against the load's datasheet minimum). In one audit, 665 segments
   were "narrower than class" and only one mattered electrically. IPC-2221 tables overstate
   heating on short segments next to a plane.

   ```bash
   python3 tools/widen_walls.py --board B --classes POWER --json walls.json   # what blocks each narrow segment
   python3 tools/widen_segs.py --board B --classes POWER --apply   # per segment to the geometric max, DRC-pruned
   python3 tools/set_widths.py --board B --json widths.json        # roll back individual segments
   ```

   Prefer `widen_segs.py` over `widen_push.py`, which is all-or-nothing per net. Compute clearance
   as the **max of both nets' classes** (a rule `A.NetClass == 'X'` matches either object). Vias
   are their own object type. Widening on plane layers cuts islands: `--planes` only with an island
   check afterwards.
2. **Thermal vias** (`tools/thermal_vias.py`). Check the footprint first, because `*_ThermalVias`
   footprints already have them.
3. **Ground stitching last** (`tools/stitch_gnd.py`, `KAT_STITCH_VIA`, `KAT_STITCH_CLEAR`). It
   writes to the project board. Then `tools/refill_zones.py`, silk (`tools/fix_silk.py`), test
   points (`tools/testpoint_vias.py`, vias rather than mask-opened pads on boards with poured
   outer layers), and `tools/gate.sh`.

## Environment flags

Flags read by the tools (originally project-prefixed, now `KAT_*`):

| Flag | Default | Read by | Effect |
|---|---|---|---|
| `KAT_ROOT` | cwd | `paths.py`, `env.sh` | project directory |
| `KAT_WORK` | `$KAT_ROOT/work` | all | work directory (state, logs, trial copies) |
| `KAT_CONFIG` | `$KAT_ROOT/kicad-agent.json` | `paths.py` | project config file |
| `KAT_PLANE_AS_SIGNAL` | `0` | `gpuroute/connectivity.py` | zone fill isn't own copper, so plane breaks route as tracks to the nearest own copper |
| `KAT_ANY_SIGNAL` | `0` | `gpuroute/route.py` | with `--any-target`, signal nets also target any own component in the window |
| `KAT_MULTI_R` | `6.0` mm | `gpuroute/route.py`, `negotiate.py` | window radius for `--any-target` |
| `KAT_VIA` | `0.5,0.2` | `maze_route.py` (shared model) | size and drill of new router vias |
| `KAT_VIA_COST` | `12` | `maze_route.py` | via cost in grid steps |
| `KAT_LAYER_COST` | outer 1.0, inner signal 1.8, inner `power` (plane) 3.0 | `maze_route.py` | per-layer cost override by canonical or alias name, e.g. `In1.Cu:30,In4.Cu:30` |
| `KAT_ROUTE_WIDTH` | `0.15` mm | `maze_route.py` | width of new tracks |
| `KAT_FINE_REFS` | config | `maze_route.py` | references whose courtyard uses the fine-pitch escape rule |
| `KAT_SIGNAL` | from board header | `raster.py` | copper layer list, e.g. for 8-layer boards (`tools/layers8.py`) |
| `KAT_SAFE_MM` | `0.02` | `gpuroute/model.py` | safety margin for copper edge position within a sub-cell |
| `KAT_PAD_SAFE_MM` | `max(SAFE, 0.03)` | `gpuroute/model.py` | safety margin toward pads |
| `KAT_PLANE_VIA_GUARD` | `0.40` mm | `gpuroute/model.py` | keep-clear belt around plane vias on their plane layer (zone clearance + min fill width) so tracks don't island them |
| `KAT_PAR` | `1` | `gpuroute/par.py` | threaded CPU stages (bit-exact with serial) |
| `KAT_PAR_WORKERS` | min(8, cores) | `gpuroute/par.py` | thread count |
| `KAT_PAR_CHECK` | `0` | `gpuroute/par.py` | compute both paths and compare |
| `KAT_STITCH_VIA` | `0.6,0.3` | `stitch_gnd.py` | stitching via size (smaller fits tight islands) |
| `KAT_STITCH_CLEAR` | `0.25` mm | `stitch_gnd.py` | clearance to foreign copper (set to the board rule, e.g. 0.15, to reach more pads) |
| `KAT_JLCPCB_DB` | `~/.konnect/jlcpcb.db` | `lcsc_lookup.py` | JLCPCB parts database |

Two rules for the cost flags:

- **Raise `KAT_VIA_COST` only together with `KAT_LAYER_COST`.** Expensive vias with default layer
  costs routed 180 mm of signal into the power planes and cut `+3V3` into islands, so three breaks
  appeared on a net nobody touched. After any cost change, compare `unconnected_items` across
  **all** nets with the baseline. The opposite extreme is useful for RF: a high via cost put a
  feed 95 % on one outer layer with half the vias.
- **Parameters can silently fail to arrive.** After editing a campaign script, grep for the new
  line, and check the first lines of the log to confirm the tool used the value.

Other variables: `BOARD`/`BRD` (board path), `ONLY_NETS_FILE` (stepwise Freerouting, both export
and import), `DROP_STITCH`/`DROP_PLANE_VIAS` (don't use), `FR_JAR`, `JAVA`, `FR_RESTART`
(Freerouting server), `BATCH`, `ROUNDS`, `RETRY`, `STALL`, `CYCLES`, `RIP_ROUNDS` (campaign
scripts), `TORCH_PYTHON` (interpreter with torch), `KICAD_CLI`.
