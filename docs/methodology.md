# Methodology: agent-driven board design with KiCad

This document describes how the tools in this repository are meant to be used together. It is
written for an AI agent (or a human driving one) that works on a KiCad 10 project from the
command line. It is not a tutorial on KiCad. The rules below come from routing and releasing a
dense six-layer board with fine-pitch packages, and each one was paid for with a wrong
conclusion first. [lessons-learned.md](lessons-learned.md) has the individual cases, and
[routing.md](routing.md) covers the routing pipeline in detail.

## Core principles

### 1. KiCad is the judge

The agent never decides on its own that a change is correct. Every change that touches copper,
footprints or the schematic ends with a KiCad verdict:

| Artifact | Judge | Command |
|---|---|---|
| board | DRC, all severities | `kicad-cli pcb drc --refill-zones --severity-all --format json` |
| board ↔ schematic | parity | same command plus `--schematic-parity` |
| schematic | ERC | `kicad-cli sch erc --format json` (violations are nested per sheet) |
| netlist | component and pin count | `kicad-cli sch export netlist` then count `(comp` and pins |

Severity classification is KiCad's, not the tool's. One hand-made list of "significant types"
once let three 0.45 mm vias through on a board with a 0.6 mm minimum. Read the whole report with
`--severity-all`. A `--severity-error` filter once hid 20 `hole_to_hole` items, 19 of them below
the fab minimum.

### 2. Lay copper by calculation, let DRC have the last word

Board rules live in three places: net classes (`.kicad_pro`), custom rules (`.kicad_dru`, often
conditional on object type) and board settings (edge clearance, hole-to-hole, rule areas).
Reproducing all of that in a tool's raster model is expensive and never complete. So every tool
that writes copper follows the same loop:

1. Snapshot the board.
2. Place copper by the tool's own calculation.
3. Run DRC.
4. Remove the objects *from this run* that DRC flagged. They are identified by `uuid`, meaning
   objects absent from the snapshot (`tools/prune_new.py --since <snapshot>`).

By construction, the loop converges: only copper that DRC accepted stays on the board. This
doesn't excuse a sloppy model. Each pruned violation points to a rule the model missed (in
practice: rule areas, edge clearance, and objects of one run not reserving space against each
other), and that rule should be fixed. The prune step means a missed rule can't damage the board.

### 3. Monotone acceptance

A routing round, a rip-up, a part nudge or a transplant is accepted only if:

```
breaks_after < breaks_before   AND   significant_violations_after <= significant_violations_before (= 0)
```

Otherwise the step rolls back to the snapshot. A run of accepted steps can only get better,
which is what makes long unattended campaigns safe. Three refinements make it work in practice:

- **Per-net acceptance.** A round is almost never better as a whole: it closes six breaks and
  adds one violation. `tools/merge_ses.py` (Freerouting) and the replay logic in
  `gpu_campaign.sh` / `maze_campaign.sh` keep the nets that improved. DRC names the offending
  nets, those nets roll back, and the round is replayed without them.
- **Whole-round first.** A good round is internally consistent. Mixing half its nets with the
  old copper of the rest can turn "accept" into "reject". Check the round as a whole first
  (`tools/accept.py`) and fall back to per-net merging only when the whole round fails.
- **A dirty start is refused.** Monotonicity takes its baseline from the board. If an
  interrupted run left one violation behind, the next campaign treats `E = 1` as the ceiling
  and starts accepting rounds with errors. Every campaign script exits up front if the board
  already has violations. Stop campaigns only between rounds.

A gate that judges only errors lets warnings pile up (overlapping vias from the router:
`holes_co_located`, `hole_to_hole`). Clean them up inside the loop after each accepted round
(`tools/dedup_vias.py`, `tools/fix_holes.py`), not once at the end.

### 4. Snapshots, copies, and a transfer discipline

- Work on a **copy** of the board, judge it there, then transfer the result to the live board
  as explicit operations (`tools/util/board_diff_ops.py` → `tools/util/edit_ops.py`). Run DRC
  again and take a new snapshot.
- A copy must sit next to its own `.kicad_pro`, `.kicad_dru` and `fp-lib-table`, and must keep
  the board's file name. Without them `kicad-cli` falls back to default rules and reports
  hundreds of phantom violations (683 in one case), or loses the footprint library and reports
  `lib_footprint_issues` that hide real `lib_footprint_mismatch` items.
- Some finishing tools write to the project board no matter what (`--board` missing or
  ignored). Before a "dry run on a copy", grep the script for its board argument. After each
  step, `cmp` the live board against the latest snapshot.
- When developing tools in a copy of the tool tree, keep a baseline of that copy and transfer
  only files changed *relative to the baseline*. "Different from live" doesn't mean "changed
  in the copy". Back up every file you replace.
- Scripts, logs and state live in the project (`KAT_WORK`, default `<project>/work`), never in
  `/tmp`. A reboot once wiped a whole pipeline kept there.
- Campaign state files (attempt counters per net) are memory. Deleting one makes the campaign
  hit the same stubborn nets again.

### 5. A check must prove it ran

The most expensive false result is a check that didn't look and reported zero. Every check in
this toolkit starts by asserting that its input is complete, and prints that assertion:

| Mechanism of a silent zero | Example | Guard |
|---|---|---|
| severity filter | `--severity-error` hid 20 hole-to-hole items | always `--severity-all` |
| missing side file | gate without `.kicad_pro` ran 5 of 9 checks | refuse to run, print the inputs |
| flag not passed | `schematic_parity` section present but empty | assert the section is non-empty |
| empty input | hierarchical-root BOM audit: "0 components, 0 issues" | assert `len(footprints) > N` |
| key mismatch | requirement keyed `UART_TX`, net named `UART_TX_MCU` | a third bucket: "not checked" |
| edit that didn't apply | `sed` pattern didn't match, exit code 0 | grep for the new line after editing |
| doubled path | `lib/X.pretty/X.pretty` never existed | assert at least one dir has content |
| stale input | ERC log older than the last schematic save | input fingerprint (below) |

A check reports three classes, not two: **passed**, **defect**, and **not checked (no
requirement / no data)**. Never write "everything else counts as GPIO". A blanket default like
that hides the signal.

The mirror image: a **write that didn't happen reports success**. A text substitution that
matched nothing (`(width 0.15)` against a file that says `(width 0.1500)`) still counts its
intentions. Substitute with a token regex, assert `re.subn(...)[1] == expected`, re-read the
file and measure the result with a *different* tool (`tools/util/net_geom.py`).

Test the tool on a case where the answer is known to be positive, not only on a negative one.
Mutation tests must take codes from the defect section only, give the mutant all of the inputs
the check uses, and require the expected code, not "something was found".

### 6. Gate verdicts carry an input fingerprint

A gate log is valid only for the inputs it ran on. `tools/gate_stamp.py --write` stores sha256
of every schematic sheet, the board and `.kicad_dru` after a full gate run. `--check` answers
"stale" and names the files that changed. File times aren't used: a zone refill rewrites the
file without changing the result, and two edits in the same second look identical. A quick
gate run (without DRC/ERC) *removes* the fingerprint so it can't vouch for old reports. The same
applies to rendered views (`tools/check_views_fresh.py`). Compare content, not mtime.

### 7. GUI cross-checks see what the CLI cannot

"Update PCB from Schematic" (F8) looks in the opposite direction from DRC and parity: from
schematic to board. In one session it found two blocking defects (transistors with mismatched
pin numbers, a module footprint with the wrong pad count) that ERC, DRC and parity all missed.
`--schematic-parity` walks from pads to pins, so a symbol pin without a pad, or a pad without a
pin, is invisible to it.

The scriptable parts of F8 are now replicated:

| F8 aspect | Tool |
|---|---|
| footprints, values, DNP, pin→net assignment | `tools/sync_board.py` (zero changes = in sync) |
| purchasing fields on both symbol and footprint | `tools/sync_fields.py`, `tools/set_field.py` |
| "No net found for component X pad N" | `tools/check_pads_no_net.py` |
| symbol pins covered by footprint pads | `tools/verify.py` |

F8 itself works only when the PCB editor is launched from the project manager. A standalone
`pcbnew` refuses ("stand-alone mode"), `kicad-cli` has no sync command, and Konnect's
`update_pcb_from_schematic` refuses footprint swaps and net changes on routed pads. When a GUI
check is needed, ask the operator for exactly that action and nothing else. The rest of the work
is autonomous.

### 8. APIs before text parsing

KiCad has three programmatic layers. Use them before regexes:

1. **pcbnew SWIG** (KiCad's bundled Python): exact pad shapes (`GetEffectiveShape`),
   connectivity, `ZONE_FILLER`. Works on files. With an editor open, it's read-only.
2. **KiCad IPC API** (`enable_server`): transactional edits to an *open* editor. Each KiCad app
   opens its own socket, and the first one takes `api.sock`. To talk to the PCB editor, start
   the PCB editor alone.
3. **Konnect** (third-party MCP server over IPC): many toolsets. Only `project` and `config` load
   at start, so load the rest explicitly. Some audits return "0 issues" on a hierarchical root
   because they don't descend into sheets. Call them per sheet.

SWIG writes aren't neutral. `SaveBoard` rebuilds connectivity and can reassign a new via to the
net of a stale zone it sits on. After `Add`, set the net code, refill zones before saving,
reload the file in a subprocess and verify the nets of the new objects.

When text parsing of `.kicad_pcb` / `.kicad_sch` / netlists is unavoidable, use
`tools/sexp.py` (block extraction by balanced parentheses, quote-aware) and respect these traps:

- A non-greedy group before an escaped `)` loses the last element. Every keepout became half its
  area (exactly 0.50 of the shoelace area) because the last vertex was dropped.
- Parentheses inside quoted strings (pin names like `PA14(JTCK`) break naive balance counting.
  Unrelated nets got merged, producing a phantom 269-node net.
- Fixed-length windows (`text[i:i+900]`) run into the neighbouring block and report its
  net or fields. Find block boundaries by balance, then assert that the block is the one you
  wanted (its `Reference`).
- Number formats depend on the last writer: `kicad-cli` writes `0.15`, SWIG writes `0.1500`.
- Formats differ between files. A schematic field has no `uuid` and keeps `(hide yes)` inside
  `(effects)`. A footprint field needs a `uuid`. Symbol sub-units in a sheet's `lib_symbols`
  cache carry no library prefix. Get one of these wrong and KiCad drops the symbol **silently**:
  the file opens, ERC runs, and the netlist has 4 components instead of 363.
- Pad angles in the board file are absolute and default to 0, not to the footprint angle.
  Rule areas inside footprints are stored in board coordinates.

The universal check for text parsing: compare the result with a number KiCad knows (net count in
the netlist versus DRC, polygon area versus bounding box, components in the netlist versus the
schematic).

### 9. Work in measured steps, one change at a time

- Change one thing per experiment. A run that changed both batch size and via hiding couldn't
  say which one hurt.
- Judge a flag that "helps the router" by the acceptance verdict, not by the router's own
  numbers. Hiding 588 ground vias made Freerouting six times faster and every round unusable
  (149 shorts on import).
- Don't infer the cause of a failure from the acceptance result if an unverified link sits
  between engine and judge. A ten-line synthetic test (`tools/test_protect.py`) overturned a
  conclusion that had stood for a day.
- Before declaring "no option exists", build the full table of options with a cost for each.
  A pin-by-pin search missed a free pin that a full enumeration found at once.
- Read the first lines of a tool's output, not just the summary. They show what the tool
  actually worked on. A wave labelled "longest first" listing 2.00, 2.09, 2.13 mm breaks was
  a sign that the parameter never arrived.

### 10. Agent hygiene

- Work autonomously and accumulate results. Interrupt the operator only for a blocking
  decision that's theirs (a part choice, a datasheet parameter that's not in the project) or for
  a GUI action. Search the project documents before asking.
- `pkill -f PATTERN` matches its own command line and any wrapper that contains the pattern.
  Symptom: exit code 144 and no output. Filter by process name
  (`pgrep -f ... | ps -o comm=`) or kill by PID in a separate command.
- If the agent's shell access starts being denied mid-session, don't rephrase the command.
  Write a resume file (what's done, what's unverified, the commands to verify it, the facts you
  found) and restart. Split long jobs into sessions per stage, each with its own progress file.
- A session transcript that recorded `cat > file <<EOF` and in-place edits can rebuild lost
  scripts. Replay the whole chain of edits, not just the heredoc.

## Stage by stage

### Schematic

| Rule | Tool |
|---|---|
| After swapping a symbol, check the netlist pin by pin, not "ERC clean". A `no_connect` marker can land on a new pin and silence ERC. | `tools/install_symbol.py`, `kicad-cli sch export netlist` |
| Every edit to a sheet is followed by a component count in the netlist. | `tools/apply_passives.py` (field template), `tools/schedit.py` |
| Fields live twice: symbol property and footprint field. Write both. | `tools/set_field.py --board` |
| Topology isn't values. For each pin whose resistor or capacitor *selects a mode* (PROG, ILIM, ADDR, MODE, RT, threshold dividers), cite the datasheet table row. | `tools/check_components.py` (registry axes) |
| Datasheet-required parts (capacitance windows on a pin, "tie RESERVED to GND") are a separate axis that ERC, DRC and parity can't see. | component registry `doc/components/<mpn>.yaml` |
| Local decoupling is checked by distance to the pin, not by total capacitance on the net. | `check_components.py` (distance requirement) |
| Unidirectional TVS polarity lives in the footprint (pad 1 = cathode). A bidirectional TVS symbol hides it. Check that the cathode is not on GND. | `check_components.py` (polarity axis), `tools/fix_tvs_polarity.py` |
| Net names follow the peripheral side or the MCU side. Take direction from the pin names in the netlist, not from the net name. RTS/CTS connect same-to-same. | registry tables with justification |
| Package variant suffixes can rename a third of the pins of an identical package. Select pin data by name, size *and* variant. | registry, symbol check |
| Registry lookup must tolerate order suffixes (`PART` vs `PARTRQMR`), or every check for that part goes quiet. | `check_components.py` |

Fill the debug map's **expected value** column (designator, net, expected, tolerance, *source*,
scenario) while there's no board yet. It's the most productive check in the project. On one day
it found three of six defects (missing I²C pull-ups, a wrong thermistor divider, a battery rail
in the default net class), all invisible to ERC/DRC.

### Placement

- **Measure escape capacity first.** On any package with pitch ≤ 0.5 mm and more than 100 pins,
  count: pins with nets / pins with an escape / pins with room for a via. If the last number is
  well below the first, routing campaigns are pointless. Tools: `tools/blockers.py`,
  `tools/gpuroute/locked.py`, `tools/gpuroute/probe.py`.
- **Pins are locked by local support parts, not by board size.** Growing the board by 10 mm and
  spreading a row of QFNs gave *more* breaks (136 vs 118). Move the 0402s and the wide power
  tracks next to the pin column first (`tools/gpuroute/nudge.py`, `tools/nudge_part.py`), and
  widen the board only together with nudges (`tools/gpuroute/widen_board.py`,
  `tools/gpuroute/spread_row.py`).
- **A node with a single keyhole is a placement limit.** If a 1×1 mm block scan shows one via
  spot shared by several nets, that's a re-placement decision, not a routing problem.
- **Search for placements with the real rules.** A simplified "no foreign copper" search said
  "nowhere"; an exhaustive search with pad↔copper clearance, hole clearance and courtyards found
  6254 positions (`tools/fit_cap.py`, `tools/fit_near.py`). Separate the conditions: body vs
  foreign *courtyards*, pads vs foreign *copper*, body inside the outline, and for power pins,
  landing inside the own-net plane. An SMD electrolytic only needs clear copper under its pads.
- **Use exact geometry.** A rectangle-vs-segment test needs a real intersection test
  (Liang–Barsky), not sampled points. A segment-to-segment distance taken as the minimum of four
  endpoint distances misses crossings. Pads are rectangles, not circles of half-diagonal (a
  2.9×5.4 mm inductor pad "eats" 1.6 mm that way).
- **On a routed board, `declash` moves parts onto foreign copper.** It works by courtyard only.
  Run it, then restore every part except the one you meant to move from the snapshot.
- **Flip in KiCad is a mirror in Y plus 180°.** Verify any side change by measuring pad
  coordinates before and after. Do the same for pad 1 under each rotation.
- **Pads don't move, nets do.** When an MCU order code renumbers pins inside the same package,
  move the *end* of each escape to the new pad instead of re-routing. Only boundaries between
  segments with different shifts collide, and DRC finds them.
- **A pin boxed in by its neighbours' escapes is better moved than ripped.** Rip-up and smaller
  vias don't open a wall that sits on every layer. Moving the signal to a free pin with via room
  (checked against the alternate-function table) routes on the first round.
- **The RF feed length is set by placement.** Measure the nets crossing the straight corridor
  (`tools/util/corridor_nets.py`) before setting a length target. Via count is a realistic
  target; length usually isn't.
- **In a buck converter the fast loop is the input loop** (Cin → switch → ground → Cin). Place
  Cin and its return first, then the switch-node snubber or filter, then the inductor.

### Routing

The method is in [routing.md](routing.md). In short:

1. Fan out fine-pitch packages deterministically (`tools/strip_annulus.py --any`,
   `tools/fanout.py`), with DRC pruning.
2. Bulk routing: Freerouting rounds or GPU campaigns with per-net acceptance.
3. Last breaks: maze router, diagnosis by reachability, minimal rip-up of named wall nets.
4. Plane-net pads: vias into the *main* fill, or route as signals to the nearest own copper.
5. Widen to class and current only after zero breaks.

Policies the router can't know (no signal copper in an antenna feed corridor) become temporary
rule areas (`tools/keepout_tmp.py`). The post-route check is the second line of defence, not
the first.

### Planes and stitching

- **Stitch ground last.** Through-hole stitching vias block all layers. In one 40×34 mm corridor
  they formed a wall of 320 vias, and removing 575 stitches reopened routing. Order: zero
  breaks → widths → thermal vias → `tools/stitch_gnd.py` → refill → silk → test points → gate.
- **A via into a plane must land in the main fill, not an island.** `HitTestFilledArea` says
  "this net's copper is here", not "this is the plane". Find the fill polygon that contains
  the point and require area above a threshold (50 mm² worked). Tools: `tools/gnd_link.py`,
  `tools/util/in_island.py`, `tools/island_stitch.py`.
- **Tracks on plane layers cut fills into islands.** A via can sit in a 1 mm² island of a
  10 000 mm² plane. Compare island counts and areas before and after any copper edit on a plane
  layer (`tools/util/zone_islands.py`, `tools/util/island_where.py`). Widening tools skip plane
  layers unless `--planes` is given, and then the island check is mandatory.
- **"Zone ↔ Zone" DRC items hide cut-off pads.** Their coordinates are the zone anchor, not the
  defect. Use `tools/island_pads.py` and `tools/util/ratsnest_pos.py` (clusters by
  `GetConnectedItems` after a refill) to find the real objects.
- **A ground stitch can be the only link.** Before removing a stitch, check whether its fill
  polygon contains a pad that has no other connection.
- **Plane vias are routing artifacts.** In placement search, treat them as removable copper.
  Model and apply step must use literally the same removal rule.

### Release gate

`tools/gate.sh` runs DRC, ERC, parity and the checks that those three don't cover. It reports a
verdict through its exit code and records an input fingerprint. Among the checks:

| Check | Catches |
|---|---|
| `verify.py` | footprint pads cover symbol pins, decoupling to plane, accepted pads without nets |
| `check_pads_no_net.py` | pads with no symbol pin (missing boot/thermal pins) |
| `check_components.py` | registry axes: pinout, package, polarity, required parts, values |
| `check_ir_drop.py` | cross-section and drop by real currents (the acceptance criterion for widths) |
| `audit_widths.py` | copper narrower than its class (informational: class width is policy) |
| `check_rf_reference.py` | continuity of the reference plane under RF feeds |
| `check_netlist.py`, `check_power_sequence.py`, `check_uniformity.py` | open drains without pull-ups, sequencing, inconsistent support parts |
| `check_stock.py`, `tools/lcsc_lookup.py` | order codes, stock, buildability of uncoded lines |
| `check_views_fresh.py` | rendered views match the current board |

Rules for a release gate:

- **Separate defect from decision.** A pinout check that lists all intentionally unused pins as
  "mismatches" (37 of them) hides the three real ones. Print accepted decisions under their own
  code, with a source reference, and compare the accepted set *exactly* so that a new item
  shows up.
- **Before accepting a finding, see what the check compared.** A "nearest object" criterion
  measured the wrong capacitor and flagged a correctly decoupled pin. Fix the criterion rather
  than adding an exception.
- **A check that always fails on its own design geometry gets ignored.** RF reference "gaps"
  were the antipads of the feed's own vias. Split by cluster, refine the rule, and fix the one
  real defect.
- **Checks phrased as "intersects" are silent on objects completely outside.** A reference
  designator entirely outside the outline breaks no rule and doesn't get printed.
- **A justification that refers to an artifact needs that artifact in the kit.** "Readable on
  the assembly drawing" meant nothing while the kit had no assembly drawing. Open the files and
  compare kit contents with what the documents cite.
- **The kit can tell the assembler to place copper.** Test points, mounting holes and printed
  antennas need `in_bom no` on the symbol *and* `exclude_from_bom` + `exclude_from_pos_files`
  on the footprint (`tools/mark_not_purchased.py`).
- **Release DRC needs the library tables** next to the copied board. Compare the violation count
  in the release manifest with the gate's count. A mismatch means one environment is incomplete.
- **Renders only show parts that have a 3D model.** A part that silently doesn't render can
  hide a mechanical collision. A model path can be broken twice (wrong variable, missing file).
  KiCad reads VRML in 0.1 inch units, and `pcb export step` ignores `.wrl`.

### Bring-up

The board arrives with an expected-value map (CSV: test point, net, expected value, tolerance,
source, failure scenario). The agent drives the instruments. The operator only places probes,
plugs cables and moves switches.

| Element | Role |
|---|---|
| USB oscilloscope / logic analyser (e.g. Digilent Analog Discovery) behind an MCP server | `measure_dc(tp)`, `scope_capture`, `logic_capture`, supplies (`instruments/ad3_mcp/`) |
| SWD probe behind a debugger MCP (probe-rs / pyOCD) | ID code, flashing, registers, RTT |
| Serial MCP on the debug console | bring-up firmware shell with `KEY=VALUE` output for parsing |
| sigrok | protocol decoding of captures |

Sequence: (0) unpowered resistance of every rail to GND (kΩ, not Ω). (1) First power-up from a
current-limited supply, quiescent current and power-good ladder. (2) All test points against the
CSV. (3) Alternative input paths (USB-PD without battery, etc.). (4) SWD ID and flash.
(5) Console bus scans and peripheral ID reads. (6) Dynamic transitions on the scope. (7) Buses
and clocks. Then function by function, then thermal soak. Write explicit stop conditions
(quiescent current above limit, any rail out of tolerance, ID code unreadable) and log every
step against the map.

For switching-regulator models: vendor PSpice models with digital behavioural blocks often parse
in ngspice after text fixes but don't oscillate (behavioural flip-flops don't toggle). Use the
vendor's simulator for transients, and keep an analytic calculation in the project for
acceptance criteria.
