# Lessons learned

A catalogue of failures behind the rules in [methodology.md](methodology.md) and
[routing.md](routing.md). Each entry has a one-line rule, then the symptom, the cause and the
correction, plus the tool that implements it where one exists. Numbers are kept where they show
scale.

Contents: [KiCad file formats and APIs](#kicad-file-formats-and-apis) ·
[Routing and Freerouting](#routing-and-freerouting) ·
[GPU and maze router](#gpu-and-maze-router) · [Planes and zones](#planes-and-zones) ·
[Placement](#placement) · [Checks and verification](#checks-and-verification) ·
[BOM and datasheets](#bom-and-datasheets) · [Fabrication outputs](#fabrication-outputs) ·
[Tooling hygiene](#tooling-hygiene)

---

## KiCad file formats and APIs

**Use the API before parsing text.**
Symptom: hours spent parsing `.kicad_pcb` with regexes while an MCP server with DRC, export and
sync tools sat unused. Cause: only the `project` and `config` toolsets of Konnect load at start.
Rule: list the toolsets, load what you need, and use pcbnew SWIG or the IPC API for geometry and
connectivity.

**IPC needs the PCB editor; F8 needs the project manager. You can't have both at once.**
Symptom: Konnect talked to an app with no open documents, and standalone pcbnew refused F8.
Cause: the first KiCad app takes `api.sock`, and F8 refuses in stand-alone mode. Rule: launch the
PCB editor alone for IPC. Treat F8 as an operator action or replace it with `tools/sync_board.py`.

**KiCad 10 boards have no net table.**
Symptom: sync code looked for a net list. Cause: the net name is stored in each pad as
`(net "name")`, and the schematic link is `(path "/sheet-uuid/symbol-uuid")`. Rule: the netlist is
the source of truth for membership, and the `.kicad_mod` library for geometry.
Tool: `tools/sync_board.py`.

**Sub-symbols in a sheet's `lib_symbols` cache carry no library prefix.**
Symptom: a component vanished from the netlist. Export exited 0 and ERC was silent. Cause: the
sub-units were written as `LIB:NAME_0_1` instead of `NAME_0_1`. Rule: after replacing a symbol,
export the netlist and count the component's pins before running ERC. Tool:
`tools/install_symbol.py`.

**KiCad silently drops a symbol with a malformed field.**
Symptom: a netlist of 4 components instead of 363, and 840 "label not connected". Cause: a field
with a `uuid`, `hide` outside `effects`, or a field after `(instances)`. Rule: copy the format of
an existing node of the same type from the same project, and verify every sheet edit by netlist
component count. Tool: `tools/apply_passives.py` (template).

**Pad angles in the board file are absolute and default to 0.**
Symptom: 201 of 1763 pads on 44 rotated parts had wrong outlines, so widening went into occupied
space and the gate still passed. Cause: code assumed a missing pad angle means the footprint
angle. Rule: missing means 0. Bake rotations into the geometry when exporting
(`tools/dsn_export.py`).

**A `custom` pad's `(size)` is only the anchor.**
Symptom: a 0.15 mm "pad" that is really 1.3×1.2 mm of copper. Rule: take copper from the
primitives or `GetEffectiveShape`. A bounding box is acceptable where erring large is safe
(export, widening) and wrong in checks (8 false overlaps).

**Rule areas inside footprints are stored in board coordinates.**
Symptom: an antenna keepout landed at the origin, and the part's bbox stretched across the board
("no room" for unrelated parts). Cause: a text insert of `.kicad_mod` transformed pads but not
the zone polygon. Rule: transform zone outlines by position and rotation. When placement says
"no room", print any bounding box larger than 40 mm.

**Flip in KiCad is a mirror in Y plus 180°.**
Symptom: after a side change, tracks landed on a GND pad. Cause: the model mirrored in X at the
same angle, while KiCad stores the back side as a Y mirror. Rule: verify any side or mirror
operation by measuring pad coordinates before and after on a copy, and derive the apply rule from
that measurement. Tool: `tools/util/place_at.py` prints pad coordinates.

**Where pad 1 goes under rotation is measured, not derived.**
Symptom: a polarity fix put the feed on the GND pad. Rule: in pcbnew, pad 1 of a symmetric
two-pad part goes −x at 0°, +y at 90°, +x at 180°, −y at 270°. Print and check the pad positions
after placing.

**Rotating a symmetric footprint by 180° keeps the tracks.**
A polarity fix on a routed board costs two operations: rotate the part and swap the pad nets.
The copper stays put. Measure that pad 1 moved by exactly twice its offset. Tool:
`tools/fix_tvs_polarity.py --only rotate|swap` (a second full run would undo the fix).

**`SaveBoard` changes nets of new items.**
Symptom: a via added for one net was saved as GND, and three hours went into "the router can't see
its via". Cause: `SaveBoard` rebuilds connectivity and copies the net of a stale fill under the
new via. Rule: `Add`, then `SetNetCode`, refill zones before saving, then reload in a subprocess
and assert the nets. Tool: `tools/util/edit_ops.py`.

**SWIG returns new wrapper objects.**
Compare board items by `uuid`, not by Python identity, when clustering by
`GetConnectedItems`. Tool: `tools/util/ratsnest_pos.py`.

**Non-greedy regex before an escaped parenthesis loses the last element.**
Symptom: all 24 keepouts exported at exactly 0.50 of their area. Cause: `(.*?)\)\s*\)` gave the
last point's `)` to the outer pattern. Rule: extract blocks by bracket balance. If identical
objects all show the same area ratio, a vertex is missing. Tool: `tools/sexp.py`.

**Count brackets outside quotes.**
Symptom: 292 nets instead of 471, and a phantom 269-node net. Cause: pin names like `PA14(JTCK`.
Rule: quote-aware balance, then compare the net count with DRC.

**A fixed-length window over an s-expression grabs the neighbour.**
Symptom: sync reported a net change on a pad that had no net. The net belonged to the next pad.
Rule: block boundaries come from balance, never from a slice.

**A "nearest delimiter" lookup must verify what it found.**
Symptom: reading fields of 14 symbols returned the previous symbol's order codes. Cause: symbols
inserted without a newline before `(symbol`. Rule: after locating a block, assert that its
`Reference` is the one you asked for, and fail loudly otherwise. Tool: `tools/schedit.py`,
`tools/fix_sch_indent.py`.

**The writer decides the number format.**
`kicad-cli` writes `0.15`, SWIG writes `0.1500`. Substitute by token regex (`\(width [\d.]+\)`),
never by a string containing a number.

**A field in the schematic also needs the footprint field.**
Symptom: adding an order code made parity report `Missing symbol field 'LCSC' in footprint`.
Rule: write both places and check parity afterwards, not just the BOM. Tool: `tools/set_field.py
--board`.

**KiCad reads VRML in 0.1-inch units.**
Symptom: a placeholder body in millimetres rendered 2.54× too large. Rule: divide by 2.54 and keep
`(scale 1 1 1)`. `pcb export step` ignores `.wrl`, so a placeholder can't leak into the
mechanical assembly. Tool: `tools/make_box_model.py`.

**A 3D model path can be broken twice.**
Symptom: an empty spot on the render after fixing the path variable. Cause: the wrong KiCad
version variable *and* a missing file. Rule: check separately that the variable expands to an
existing directory and that the file exists under it.

**Vendor PSpice models parse in ngspice, but their digital blocks don't run.**
Six text fixes (nested braces, `0 .5`, `&`/`|`, `IF()`, `VSWITCH`, floating nodes via `rshunt`) get
a switching-regulator model to load. Behavioural flip-flops still don't toggle, so the oscillator
never starts. Use the vendor's simulator for transients and keep analytic calculations in the
project. To diagnose, print the shutdown, UVLO, soft-start, clock and ramp nodes.

## Routing and Freerouting

**Per-net acceptance makes a campaign monotone.**
Symptom: every round closed breaks and added a violation, so whole-round acceptance stalled.
Rule: keep only the nets that improved, let DRC name the offenders, roll those back and rebuild
(up to 4 times). Ten unattended rounds went 256 → 226 at zero violations, and none of them would
have passed whole. Tool: `tools/merge_ses.py`.

**Per-net merging harms a good round.**
Symptom: "ACCEPT 247 → 235" from the trial import, then "REJECT, violations didn't clear in 4
builds" from the merge. Cause: a router solution is consistent only with itself. Rule: check the
whole round first (`tools/accept.py`) and merge per net only as a fallback.

**A gate that judges errors lets warnings accumulate.**
Symptom: 31 `holes_co_located` and 4 `hole_to_hole` after ten clean rounds. Rule: clean up after
every accepted round inside the loop. Tool: `tools/dedup_vias.py`, `tools/fix_holes.py`.

**An interrupted campaign poisons monotone acceptance.**
Symptom: a run killed mid-round left one violation, and later rounds were "accepted with 1 error".
Cause: the baseline is read from the board. Rule: refuse to start on a board with violations and
stop only between rounds. Tools: all campaign scripts refuse at start.

**Hiding vias from the router invalidates every round.**
Symptom: every Freerouting round rejected with 149 `shorting_items` (`Via [GND]` vs tracks).
Cause: `DROP_STITCH` hid 588 through vias, so tracks went straight through them. The earlier
"benefit" measured speed, not validity. Rule: judge a helper flag by the acceptance verdict.
Use `campaign.py --keep-stitch`, or remove stitching from the board and restore it at the end.

**Hiding plane vias is worse.**
Symptom: 0.00 breaks/min and 37 violations. Cause: power vias sit next to pads, exactly where the
router must go. Rule: keep `DROP_PLANE_VIAS` off. Change one variable per experiment.

**Protected copper is an obstacle for Freerouting.**
Symptom: a day-long conclusion that stepwise mode "routes over protected copper". Cause: the
shorts actually came from keepouts exported at half their area. Rule: don't infer an engine's
behaviour through an unverified pipeline link. Write the ten-line synthetic test. Tool:
`tools/test_protect.py`.

**Specctra `keepout` doesn't forbid vias.**
Symptom: vias under an antenna keepout. Rule: also export `(via_keepout …)` for every zone with
`(vias not_allowed)`. Tool: `tools/dsn_export.py`.

**Stepwise mode must be set at both ends.**
Without `ONLY_NETS_FILE`, export drops ordinary copper (1095 "unrouted" instead of 302, and the
SES would overwrite the board) and import drops untouched nets (372 breaks instead of 254). Never
route plane nets as tracks (105 shorts).

**Freerouting jobs never finish, and cancelling destroys the result.**
Rule: run the API server and pull the current SES from `/v1/jobs/{id}/output/stream` while the job
runs. `/cancel` makes the result unreachable and doesn't stop the thread. The stream never closes,
so parse incrementally. Tools: `tools/fr_api.py`, `tools/fr_grab.py`.

**Freerouting 2.4.x fails silently in three ways.**
It needs a newer JDK (Java 25 for 2.4.1). API auth is disabled only by the CLI flag
`--api_server.authentication.enabled=false`. The endpoints env var never applies (array vs
string). Tool: `tools/run_route.sh`.

**SES coordinates can come back scaled ×10.**
Rule: calibrate the scale from the `placement` section against the board, fail on a spread over
0.1 %, and compare the number of parsed wires with the number declared. A parser that read zero
segments once reported success. Tool: `tools/ses_import.py`.

**Small batches beat large ones under per-net acceptance.**
20–45 nets gave 0.3–1.0 breaks/min, 120 nets gave 0.00–0.06. Measure speed in breaks per minute,
or modes can't be compared. Freerouting's own `unrouted` count isn't comparable between modes.

**Don't delete the campaign state between experiments.**
Symptom: repeated zeros. Cause: deleting `campaign.json` reset the attempt counters, so the same
stubborn short breaks came first again. Tool: `tools/campaign.py`.

**Transplant copper from another solution one net at a time.**
Symptom: moving 17 nets at once gave more breaks, and pruning wiped out the gain. Rule: transplant
one net at a time and keep it only if breaks drop with zero violations. Tool:
`tools/transplant_loop.sh`, `tools/transplant_net.py`.

**For the last breaks the maze router beats Freerouting.**
Symptom: three stepwise Freerouting runs did zero passes, and the campaign said "no gain". The
maze router closed both nets in 0.1 s and 5.5 s. Rule: `maze_route.py --list` then `--apply
--limit N`. Keep the campaigns for bulk work.

**Keep a single judge.**
Two clearance models and two judges in one loop disagree: a micro-gap invisible to the raster
counts as a break for DRC. Port ideas from Freerouting (deterministic fan-out, post-route
tightening, a per-round score) into one pipeline instead of cascading engines.

## GPU and maze router

**Raster precision locks fine-pitch pads.**
Symptom: 93 of 187 break ends couldn't be left at all. Cause: a 0.1 mm raster with ceil dilation
ate a real 0.15 mm lane. Rule: 0.025 mm sub-raster plus EDT brought it to 46, all real. Verify
the obstacle model by reachability, not by DRC of the result. Tool: `tools/gpuroute/locked.py`,
`probe.py`.

**Read DRC rules by object type.**
Symptom: pins that were already routed showed as "locked, reachability 0", and the count stuck at
118. Cause: class clearances in `.kicad_dru` apply only to Track↔Track, but the model applied
them to pads. Rule: separate pad and track clearance maps. The same campaign then went
118 → 110 without moving a part. Diagnose with a per-cell reason map (pad, track, strict, hole,
edge).

**A rule `A.NetClass == 'X'` matches either object.**
Symptom: widening a default-class net violated a neighbour's 0.25 mm rule. Rule: take the
maximum clearance of both nets' classes, and also count foreign vias. Test on a default-class net
next to a privileged class. Tool: `tools/widen_net.py`, `tools/project.py`.

**Rebuild model caches after mutating the board.**
Symptom: 12–30 shorts per round although negotiation converged. Cause: a basket cached by
`id(board)` didn't see copper that rip-up had returned. Rule: rebuild per model (0.12 s), or key
by content. The same bug class hit a silk tool that measured positions once before moving.

**Parallelise only bit-exact.**
`Model.__init__` went from 31 s to 6 s with one SWIG pass, a thread pool for EDT/label (scipy
releases the GIL) and grouped SDFs. Rule: profile first, then verify every tensor against the
serial path before enabling. Check defaults separately, because `workers()` returned 1. Tool:
`tools/gpuroute/par.py`, `check_par.py`.

**Rip-up converges with a small radius.**
r = 4 mm: 76 → 60 at 0 errors. r = 14 mm (89 nets): no convergence, 198 shorts. Rule: chains of
small windows. If negotiation doesn't converge, don't write; shrink the radius. Tool:
`tools/gpuroute/ripup.py`, `rip_campaign.py`, `rip_ends.py`.

**A break that survives every campaign is invisible to the model.**
Symptom: two breaks untouched through dozens of rounds. Cause: edges closer than one raster cell
form one component for the router and a break for DRC. Rule: if a break persists, check that it
appears in the router's work list at all. Tool: `tools/close_gaps.py`.

**The A* goal must be a free cell.**
Symptom: "no path, 1 106 501 cells" to a reachable goal. Cause: DRC gives a track's `start`, which
fell inside a pad dilation. Rule: target the whole connected component of own copper. Tool:
`tools/maze_route.py`.

**Your own hole locks a pre-placed via.**
Symptom: "93 cells explored" from a hand-placed via. Rule: exclude own-net holes from the *track*
mask only. Hole-to-hole applies to vias of the same net. Tool: `tools/raster.py` (`nets_skip`).

**Read "no path" by the cell count.**
A small N (13, 93, 365) means the end is locked. N in the millions means the corridor is locked,
and you need to find whose copper forms the boundary. Tools: `tools/reach.py --path`,
`tools/corridor_open.py`.

**Map the free cells, then rip only the wall nets.**
Symptom: regional rip of all neighbours rebuilt the same wall. Rule: a free-cell map with reasons
per layer shows one or two specific tracks. Remove those (`edit_ops`, `ripup --nets`), judge on a
copy and transfer. Tool: `tools/util/diag_free.py`.

**Geometry-first tools close the last breaks faster than campaigns.**
7 → 0 breaks in an evening with one-second answers from pcbnew: `via_spots` (room and walls),
`ratsnest_pos` (real clusters), `board_diff_ops` (copy → live operations). Campaigns spend
minutes saying "no path" without a reason.

**Window width decides monotonicity.**
A window over a whole fine-pitch package side ripped 14 nets and turned 1 break into 5. Ripping
the 3 named wall nets gave 0/0. Rule: size the window by blocking objects and take a snapshot
before every window. Tool: `tools/util/rip_at_nets.py`.

**Priority in the escape ring is electrical.**
A crystal output and a slow interrupt competed for one corridor, and length order chose wrong.
Rule: route the sensitive net alone first. If the other then fails, widen the window and
negotiate both together.

**A one-keyhole node: hard net first.**
Ripping one wall at a time let the hard net take the only keyhole and left the wall unable to
return. Rule: rip all wall nets at once, route the hard net, then the rest in one pass. Tools:
`tools/rip_area.py`, `tools/keyhole.py`.

**Expensive vias push the router into the planes.**
Symptom: three new `+3V3` breaks after routing an unrelated net. Cause: a higher `KAT_VIA_COST`
with default layer costs laid 180 mm of signal in the plane layers. Rule: raise it together with
`KAT_LAYER_COST` and compare unconnected items across all nets.

**Policy the router can't know needs a keepout.**
Symptom: a via twice placed 0.2 mm from an RF feed, both times caught only by the post-check.
Rule: add a temporary rule area, route, remove it. Size it by the clash (a generous box gave 82
temporary violations). Tool: `tools/keepout_tmp.py`.

**Router self-checks.**
Three router bugs looked like "routed" while the break stayed: start cells on any layer (a track
on an inner layer with no via), a straight-segment substitution that dropped the via, and a
zero-length path counted as success. Symptom in DRC: two objects of one net at the same
coordinates on different layers. Rule: start from layers with own copper, then outer layers;
reject results shorter than 0.05 mm without a via.

**Learning isn't the bottleneck.**
The last dead ends closed by computable geometry, not by path search. Formalise the moves (rip
the wall, place a via stack, move the signal, nudge the part) as deterministic tools. Learning
pays off later for parameters (order, costs) and for predicting locked pins.

## Planes and zones

**Stitch ground last.**
Symptom: the final routing stuck at ~20 "no path" breaks. Cause: 320 through vias in a 40×34 mm
corridor. Removing 575 stitches reopened it. Rule: zero breaks → widths → stitching → refill.
Tool: `tools/stitch_gnd.py`, `tools/unstitch_walls.py`.

**A ground stitch can be the only link.**
Symptom: removing one stitch disconnected a GND pin. DRC reported it at the zone anchor. Rule:
before removing a stitch, check its fill polygon for pads with no other link.

**Sew a plane pad to the main fill, not to an island.**
Symptom: vias placed "where own fill exists" didn't close breaks. Cause: the fill was an island
that survives island removal because it contains a pad. Rule: require the containing polygon to
exceed a threshold (50 mm²). That change took 19 → 11 breaks in one pass. Tools:
`tools/gnd_link.py`, `tools/util/in_island.py`.

**Tracks on plane layers cut the fill into islands.**
Symptom: plane pads "disconnected" although their vias were inside the fill. One via sat in a
0.99 mm² island of a 10 942 mm² plane. Rule: check polygon area, keep a guard belt around plane
vias (`KAT_PLANE_VIA_GUARD`), and route fragments with pads via `route.py --planes`. Widening on a
plane layer by just 0.15 → 0.3 mm cut off a 3 mm² island. Tools: `tools/util/zone_islands.py`,
`island_where.py`.

**"Zone ↔ Zone" DRC items hide cut-off pads.**
Symptom: 34 items at one point (the zone anchor), surviving island removal. Behind them were 33
GND pads with a fill fragment and no via. Rule: find them by raster connectivity, then route as
signals with `--any-target`. Smaller stitching vias and clearance (`KAT_STITCH_VIA=0.5,0.2
KAT_STITCH_CLEAR=0.15`) reach more of them. Tools: `tools/island_pads.py`, `tools/free_pad.py`.

**Route plane-net breaks as signals to the nearest copper.**
Symptom: 8 plane pads untouched by via placement or rip-up. Cause: there was no via stack on any
layer, and the router aimed at the fill. Rule: `KAT_PLANE_AS_SIGNAL=1 route.py --planes
--any-target`, plane net first, neighbours second. That closed 7 of 8.

**Plane vias are routing artifacts.**
Symptom: a placement search found no way to open three pins under any nudge. Cause: it treated
plane vias placed by earlier tools as static. Rule: treat all pipeline-generated copper as
removable, and use one literal removal rule in the model and in the apply script. Tool:
`tools/gpuroute/place/apply_place.py`.

**Remove the walls before placing vias, not after.**
Symptom: "stitching doesn't stick", the same pads returned at new coordinates. Cause: vias saved
over foreign copper took its net, and ripping that net deleted them. Rule: relax (list the walls)
→ rip → place → reroute. Tool: `tools/island_stitch.py --relax`.

**A lone fragment is stitched from inside.**
A fill fragment cut off by a bundle of signals is often the size of a 0402 pad, and any jumper
would cross a track. Put the via inside the fragment. Tool: `tools/island_stitch.py`.

**A check that flags its own geometry gets ignored.**
Symptom: an RF reference check that would always be red. 8.17 of 8.42 mm² of "gap" were antipads
of the feed's own vias. Rule: split by cluster and owner, refine the rule (own vias and own
through-hole pads excluded, foreign copper and SMD pads still count), and fix the real defect
(one foreign via, two merged antipads). Tool: `tools/check_rf_reference.py`.

## Placement

**Your own escapes lock the fan-out ring.**
Symptom: both engines closed 0–1 breaks per minute for a long time. Cause: 40 diagonal escapes of
one 0.5 mm-pitch package locked 82 other pins. Rule: measure escape capacity first, strip the
ring (`--any`) and build a systematic fan-out from the package side. Tools:
`tools/strip_annulus.py`, `tools/fanout.py`, `tools/push_ring.py`.

**Clearing F.Cu lanes alone doesn't help.**
Symptom: −42 breaks net after clearing 158 segments of foreign copper. Cause: the escape needs via
room on all layers. Rule: clear lanes only together with via room, which in practice means
re-laying the area. Tool: `tools/lane_clear.py`.

**QFN pins are locked by local support parts, not board size.**
Symptom: +10 mm of board and a spread row gave 136 breaks instead of 118. Cause: 0402s 0.2 mm
from the pin column and wide power tracks. Rule: nudge support parts first and grow the board
only together with nudges. Tools: `tools/gpuroute/nudge.py`, `widen_board.py`.

**Via size is a routing-capacity knob.**
Symptom: months of campaigns couldn't route a fine-pitch corridor. A 0.5/0.2 via did it in one
round. Rule: try a smaller via on a copy before rip-up, and match the size to the fab capability.
`KAT_VIA`.

**A pin boxed in by neighbours: move the signal to a pin with via room.**
Twelve rounds of rip-up and smaller vias opened nothing. Moving four signals to free pins routed
all four in the first round. Measure suitability by free via cells 1.5–2.5 mm out from the pad,
not by F.Cu reachability. Check alternate functions, and the target pad must have no copper.

**Pads don't move, nets do.**
A package variant renumbered 52 pins. Shifting escape ends kept 42 of 48 nets routed (219 → 269
breaks vs ~271 for a full strip). Only segment boundaries collide. Strip short escapes of the
offenders (`tools/strip_pocket.py --max-len 3`).

**One keyhole per node is a placement limit.**
A 14×18 mm quadrant had exactly one via spot, needed by a ground stitch and wanted by two signals.
Rule: scan via spots in 1×1 mm blocks on all layers before clearing, and escalate a re-placement
decision.

**Search placements with the real rules.**
"Nowhere within 3 mm" turned into 6254 valid positions, the nearest at 1.25 mm. Rule: exact pad
↔ copper, hole and courtyard checks in one pass with a clipping window. DRC is the final judge,
not the search method. Tools: `tools/fit_cap.py`, `tools/fit_near.py`.

**An SMD electrolytic only needs clear copper under its pads.**
Symptom: zero positions on the whole top side. Cause: the full body was required to be free of
copper. Rule: separate body vs courtyards, pads vs copper, inside the outline, and power pin inside
its own plane. That gave 170 positions, then 2 with the plane condition.

**Rectangle vs segment needs a real intersection.**
Symptom: a track ran through a pad rectangle between sample points, and DRC found shorts. Rule:
inflate by the half-width and clip (Liang–Barsky). Keep copper together with its net, because
each pad has its own allowed nets.

**Segment distance as the minimum over endpoints misses crossings.**
Symptom: 8 of 9 ground jumpers crossed signal tracks (`tracks_crossing`). Rule: test intersection
by cross-product signs first. Test cases: cross (0), parallel, T-touch (0), apart. Tools:
`tools/gnd_link.py`, `tools/lane_clear.py`, `tools/close_gaps.py`.

**`declash` moves by courtyard and ignores copper.**
Symptom: one overlap fixed, six parts moved onto foreign tracks, 59 of 86 violations. Rule: on a
routed board, restore every part except the target from the snapshot. That gave 59 → 0. Tool:
`tools/declash.py`.

**The RF feed length is set by placement.**
84 nets crossed the straight corridor, and the router reproduced the same perimeter path three
times. Rule: measure the corridor (`tools/util/corridor_nets.py`) before setting a length target.
Via count and layer share are achievable targets.

**In a buck converter the fast loop is the input loop.**
Inductor current is continuous; the input loop switches. Place Cin and its return first, then the
switch-node filter, then the inductor. Take the values from the datasheet's external-component
table.

**Measure a nudge by the cycle, not by itself.**
Breaks go up right after a nudge, because stubs get stripped. Accept a nudge on violations only,
and judge the gain over "nudge → routing rounds", rolling back the cycle if breaks didn't drop.
Strip stretched stubs instead of stretching them.

## Checks and verification

**A check that didn't run reports zero.**
Severity filters, missing side files, missing flags, empty inputs, a `no_connect` that swallowed
a new pin, a `sed` that didn't match: six cases, all "clean". Rule: assert input completeness and
print it (`len(footprints) > N`), and distinguish "0" from "not checked". Tool: `tools/verify.py`,
`tools/gate.sh`.

**A write that didn't happen reports success.**
Symptom: "widened 36 segments" and not one byte changed. Cause: string replace of `0.15` vs
`0.1500`. Rule: token regex, assert the substitution count, re-read the file and measure with
another tool (`tools/util/net_geom.py`).

**The gate log needs an input fingerprint.**
Symptom: an ERC count quoted from a log 58 s older than the last schematic save. Rule: store
sha256 of the inputs after a full run, check before quoting, and remove the fingerprint on a quick
run. Tool: `tools/gate_stamp.py`.

**Check freshness of views by content, not mtime.**
Renders were 30 s older than a zone refill. Rule: store the board hash with the views and compare.
Tool: `tools/check_views_fresh.py`, `tools/make_views.sh`.

**GUI cross-checks find what the CLI cannot.**
F8 found transistors with mismatched pin numbers and a module with the wrong pad count. ERC, DRC
and parity were all clean, because parity goes pad → pin. Rule: when a check runs in the opposite
direction, it sees different defects. Script it once it's understood. Tool: `tools/verify.py`.

**F8 sees pads without nets.**
41 "no net for pad" warnings were invisible to everything else, and one was a real defect (a boot
configuration pin missing from the symbol, so there was no firmware recovery path). Tool:
`tools/check_pads_no_net.py`, with an exact accepted set.

**A pad with an empty net means the symbol lacks that pin.**
Every schematic pin has a net, real or `unconnected-(…)`. A pad with no net is legitimate for
mounting, fiducials, thermal pads, merged pads and intentionally unused module pins. It's a defect
if the pin is soldered and required. Document accepted pads with a datasheet reference.

**A check must separate defect from decision.**
37 pinout "mismatches" hid three real ones, including a typo in the vendor's own summary table.
Rule: name mismatches and extra symbol pins are defects. Datasheet pins not brought into the symbol
are decisions, printed under their own code.

**The check key must match the real net name.**
A requirement keyed `X_UART` didn't match the net `X_UART_MCU`, so the main pinout check silently
stopped running. Rule: three buckets (checked, defect, not checked), and never "everything else is
GPIO".

**Registry lookup must tolerate order suffixes.**
`PART` in the schematic, `PARTRQMR.yaml` in the registry: every check for that part was silent,
and a missing decoupling cap went through. Rule: compatible-prefix matching, plus a "not checked"
section. Tool: `tools/check_components.py`.

**A mutation test can be green where the check is blind.**
Codes collected from the whole output (including the "registry incomplete" section) and a mutant
run without the board made blind spots look covered. Rule: take codes from the defect section
only, give the mutant all of the inputs, and require the expected code. Tool:
`tools/test_components.py`.

**A tool found by checking a known positive.**
A doubled path (`lib/X.pretty/X.pretty`) meant the project library was never scanned. The
negative result was right by accident. Rule: assert that at least one listed directory has
content, and test on a case with a known positive answer.

**See which object the check compared.**
A decoupling check measured the *nearest* capacitor (12 mm away) instead of the one sewn to the
plane 1.2 mm away. Rule: before accepting a finding, read what was compared. Fix the criterion,
don't add an exception.

**Checks phrased as "intersects" are blind to objects completely outside.**
Two reference designators sat entirely off the board, and `silk_edge_clearance` stayed silent.
Rule: check bounding boxes against the outline separately. Tool: `tools/util/silk_report.py`.

**`silk_over_copper` is mask clipping, not paint on pads.**
The DRC item has one object, and moving labels only shifts them between own and foreign pads.
Rule: measure labels over *foreign* parts and keep the class count from growing. Silk is exported
with `--subtract-soldermask`. Tools: `tools/util/silk_report.py`, `silk_edit.py`, `tools/fix_silk.py`.

**Release DRC needs the library tables too.**
A release copy without `fp-lib-table` had 7 phantom library items that hid 2 real mismatches. The
manifest said 490, the gate 485. Rule: compare the counts. The release builder fails on "does not
include the footprint library". Tool: `tools/make_fab.sh`.

**ERC JSON nests violations per sheet.**
A gate that read top-level `violations` printed "0 ERC errors" for any schematic. Rule: walk the
sheets and test the parser on a schematic with a known error.

**Expected values are the most productive check.**
Filling the "expected value" column of a debug map found three of six defects in one day
(missing pull-ups, a wrong thermistor divider, a battery rail in the default class). The "source"
column is mandatory. Write "TBD" rather than a number from memory.

**After a symbol swap, check the netlist, not ERC.**
A `no_connect` marker from a vanished pin landed on a new power pin, and ERC called it
intentional. Only the netlist (`unconnected-(U-PIN-Pad)`) showed it.

## BOM and datasheets

**Topology isn't values.**
A power audit by netlist declared the design correct and passed over two strap resistors that
kept the converter off. Rule: for each pin whose part *selects a mode* (PROG, ILIM, ADDR, MODE,
RT, threshold dividers), cite the table row. A resistor to ground on an analog input works only if
the IC has an internal current source. Sibling parts differ, and the schematics look the same.

**Datasheet-required parts are a separate axis.**
A capacitance window on a pin (an external capacitor is mandatory because the internal one is too
small) and "tie RESERVED pins to GND" are invisible to ERC, DRC and parity: KiCad gives a floating
pin an `unconnected-` net. Rule: keep them in the component registry with a table reference and
check them on their own axis. Tool: `tools/check_components.py`.

**Local decoupling needs a distance check.**
Bulk capacitance was present, and the required 0.1 µF "as close as possible" was missing. A
sum-of-capacitance check passed. Rule: look for a part of the required value on the net and
measure its distance to the pin, mapping pin names to pad numbers via the registry.

**Percent thresholds in a datasheet imply a divider.**
Thresholds given as % of a regulator rail "with a 103AT thermistor" assume a specific divider that
isn't written anywhere. Solve it from two table points and verify against the rest (5 points within
0.64 %). Round 10 k/10 k made the charger read room temperature as "hot" and suppress charging.

**Size the cross-section by average current, the drop by peak current.**
Judge the drop against the load's datasheet minimum, not as a percentage. A converter's rating
isn't the load current. Tool: `tools/check_ir_drop.py`.

**Net-class width isn't a current requirement.**
665 "too narrow" segments, one electrically significant. Inner copper is thinner (15 µm vs 35 µm),
and IPC-2221 grossly overstates heating of short segments next to a plane. Rule: `check_ir_drop.py`
to zero, then widen where possible.

**Read the absolute maximum from the right row.**
"26 V" belonged to the CC pins, and the supply pin was rated 28 V. An open question stood on that
misreading. Rule: copy the whole row with pin names and check against `pdftotext -layout` with
surrounding lines.

**A footnote defines the selection window for protection.**
"A TVS with breakdown between the recommended max and the abs max is recommended." With its VBR
spread, a common 24 V TVS starts conducting *after* the abs max. Rule: read the footnotes under
abs-max rows.

**The bus limit is set by the most sensitive consumer.**
The limit of a shared rail is the minimum abs max over all its consumers (23 V at a charger pin,
not 32 V at the motor drivers). A TVS with 24 V VRWM didn't protect it. Take the working range
from the strapping, not from prose.

**A package-variant suffix can shift the pinout.**
Two order codes of the same 176-pin package differed in about a third of the pin names. Crystal,
reset and the SMPS pin would have moved. Rule: select pin data by name, size and variant.
Alternate functions follow port names, so a function audit survives a variant change; the symbol
and footprint don't.

**Net names follow the peripheral, not the MCU.**
`DEV_TXD` is the device's output, so the MCU needs RX there. Both conventions appear in one
project. RTS/CTS connect same-to-same. Rule: take direction from the netlist pin names and pin
types.

**Enumerate the whole option space first.**
"No free USART pair exists" was wrong: a full table found a free TX pin at once. Rule: build the
full table with costs, then sort.

**A BOM line can be physically unbuildable.**
"100 µF/35 V 0805" doesn't exist; the 0805 maximum for 100 µF is 6.3 V. And the consumer's
datasheet asked for an electrolytic anyway. Rule: query the catalogue by value + package + voltage
for every uncoded line. Zero hits means unbuildable. Tools: `tools/lcsc_lookup.py`,
`tools/pick_passives.py`.

**The assembler's default for an uncoded passive isn't what you need.**
"100nF 0402" defaults to a 16 V part. On a 20 V rail, that part runs beyond its derating. Rule:
class II ceramics at ≤ 50 % of rated voltage. Check each passive against its *net* from the
netlist. Tools: `tools/check_stock.py`, `tools/set_lcsc.py`.

**TVS polarity lives in the footprint.**
All four unidirectional TVS diodes were placed cathode-to-GND under a bidirectional symbol. That
shorts a supply rail and shunts the PD CC lines. Rule: use a Zener-type symbol (K/A) and check
that the cathode isn't on ground. Tool: `check_components.py` (polarity), `tools/fix_tvs_polarity.py`.

**Merged pads hide pin numbers.**
A WQFN footprint lacks six pad numbers because parallel pins share one pad (area 3× a single pad).
Rule: judge by pad area. Keep `merged_pins` and `thermal_pads` in the registry and count unique pad
numbers.

## Fabrication outputs

**A justification needs a file in the kit.**
"Readable on the assembly drawing" while the kit had no assembly drawing, and the title block said
the previous revision. Rule: open the outputs. Kit contents must match what the documents cite.
Tool: `tools/make_fab.sh` (assembly PDF with `--scale 0`).

**The kit can tell the assembler to place copper.**
19 BOM lines without order codes and 36 placement rows for test points, holes and a printed
antenna. Rule: `in_bom no` on the symbol *and* `exclude_from_bom` + `exclude_from_pos_files` on
the footprint. Don't touch `on_board`. Tool: `tools/mark_not_purchased.py`.

**A render shows what has a 3D model, not what is assembled.**
Identical connectors rendered partly. Board footprints keep their own `(model …)` copy, and some
had lost it. A part that doesn't render can hide a collision. Rule: restore model references from
the library and calibrate size against a part with a known body. Tool: `tools/make_views.sh`.

**A soldermask test pad on a board with poured outer layers bridges to the fill.**
`probe_pads.py` gave 12–17 `solder_mask_bridge` items. Rule: put test points on vias, which can be
probed from both sides. Tool: `tools/testpoint_vias.py`.

**Release tools write to the project board, not the copy.**
A finishing script ignored `--board` and wrote vias into the live board. A copy→live diff then
removed them. Rule: grep for the board argument before a "copy run", and `cmp` with the last
snapshot after each step. Tools: `tools/stitch_gnd.py`, `tools/fix_silk.py`,
`tools/testpoint_vias.py`.

**Thermal-via footprints already contain the vias.**
Adding thermal vias over a `*_ThermalVias` footprint clogs the inner layers under the part. Check
the footprint first. Tool: `tools/thermal_vias.py`.

## Tooling hygiene

**`pkill -f` kills its own command.**
Exit code 144 and no output: the pattern matched the wrapper's command line, and once the bracket
trick also matched elsewhere in the same line. Rule: `pgrep` + filter by `ps -o comm=`, or kill by
PID in a separate command. Keep kill logic inside script files. Tool: `tools/run_route.sh`,
`tools/util/killtree.sh`.

**Waiting with `ps | grep name` matches itself.**
Filter on the process name column (`ps -eo comm,args | awk '$1=="python3" && /name/'`).

**Copy-tree transfers clobber live edits.**
"Different from live" isn't "changed in the copy". Rule: keep a baseline snapshot of the copy,
transfer only files changed against it, back up everything you replace, and grep for function
markers afterwards.

**Don't keep the pipeline in `/tmp`.**
A reboot erased eight scripts. Rule: keep tools and state in the project. Paths resolve from
`tools/paths.py` / `tools/env.sh`.

**The session transcript is an archive of the toolchain.**
Lost scripts can be rebuilt from heredocs and in-place edit commands in the agent's session log.
Replay the whole chain of edits in order (patches that `assert old in s` report mismatches). Never
re-run whole logged commands, because they include kills and board writes.

**When the agent's shell gets blocked, stop and write a resume point.**
In very long sessions the agent harness may start denying shell commands one after another.
Rephrasing doesn't help, and editing without verification piles up risk. Rule: the first denial is
the signal. Write the resume file (done, unverified, commands to check, facts found) and restart.
Plan big jobs as one session per stage.

**Work autonomously; interrupt only for GUI or a blocking decision.**
The operator's time is needed for F8, clicking through a DRC panel, or a part choice. Search the
project documents before asking, since one question had its answer in an RFQ already in the repo.
