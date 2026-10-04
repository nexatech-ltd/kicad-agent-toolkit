# Tool catalogue

All tools are standalone scripts. Run any of them with `--help` for usage.
Tools that edit a board print a plan by default and write only with `--apply`.

Paths: `tools/` for the main tools, `tools/util/` for small diagnostics and point edits,
`tools/gpuroute/` for the GPU router, `tools/gpuroute/place/` for the placement search.

## Project setup and shared modules

Every tool imports these; they hold no board knowledge.

| Tool | What it does |
|---|---|
| [`paths.py`](../tools/paths.py) | Single source of paths for every tool in this toolkit. |
| [`project.py`](../tools/project.py) | Project-specific knowledge, kept out of the tools. |
| [`env.sh`](../tools/env.sh) | Single source of paths for the shell scripts. |
| [`sexp.py`](../tools/sexp.py) | Minimal s-expression helpers for KiCad files. |
| [`schedit.py`](../tools/schedit.py) | Helpers for text editing of KiCad schematic sheets (shared by set_*.py, add_cap.py, install_symbol.py and others). |
| [`drcinfo.py`](../tools/drcinfo.py) | Reading a KiCad DRC report: breaks per net and violations by severity. |
| [`raster.py`](../tools/raster.py) | Per-layer obstacle raster of a board - the common base for placement and routing. |
| [`place_geom.py`](../tools/place_geom.py) | Footprint geometry helpers shared by the placement tools (pcbnew, millimetres). |

## Schematic, netlist and board sync

Edit sheets as text safely and keep the board in step with the schematic without the GUI.

| Tool | What it does |
|---|---|
| [`sync_board.py`](../tools/sync_board.py) | Synchronise the board with the schematic: what F8 does, but from the command line. |
| [`sync_fields.py`](../tools/sync_fields.py) | Copy symbol fields (MPN, LCSC, Datasheet, ...) from the netlist into footprint fields. |
| [`check_pads_no_net.py`](../tools/check_pads_no_net.py) | Board pads without a net: the one thing sync_board/sync_fields do not cover. |
| [`remove_refs.py`](../tools/remove_refs.py) | Remove footprints that are no longer in the schematic, together with the copper of nets that are left without a single pad. |
| [`rename_net.py`](../tools/rename_net.py) | Rename a net on the board, both on pads and on copper. |
| [`normalize_nets.py`](../tools/normalize_nets.py) | Make the net names of copper in the board file match what KiCad sees. |
| [`set_field.py`](../tools/set_field.py) | Set or change a component field in the schematic (LCSC, MPN, or any custom field). |
| [`set_lcsc.py`](../tools/set_lcsc.py) | Set LCSC / MPN (and optionally Value, Footprint, lib_id) on schematic components from a table. |
| [`lcsc_lookup.py`](../tools/lcsc_lookup.py) | Fill the LCSC field from the part number (MPN) using a local JLCPCB database. |
| [`set_dnp.py`](../tools/set_dnp.py) | Set the (dnp yes) attribute on parts that must not be assembled. |
| [`install_symbol.py`](../tools/install_symbol.py) | Install a local symbol into the project library and into a sheet's symbol cache. |
| [`add_cap.py`](../tools/add_cap.py) | Add a capacitor between a net and GND on a given schematic sheet. |
| [`fix_tvs_polarity.py`](../tools/fix_tvs_polarity.py) | Turn unidirectional TVS diodes around: the cathode must face the protected line, the anode GND. |
| [`fix_sch_indent.py`](../tools/fix_sch_indent.py) | Restore the newline before symbols inserted by scripts: `)\t(symbol` -> `)\n\t(symbol`. |
| [`sch_window.py`](../tools/sch_window.py) | List what lies inside a window of a schematic sheet: symbols, labels, wires, NC flags, junctions. |

## Components, sourcing and datasheets

A per-part registry checked against the schematic; order codes justified by the rail they sit on.

| Tool | What it does |
|---|---|
| [`check_components.py`](../tools/check_components.py) | Check schematic components against a component registry (`doc/components/`). |
| [`make_component_index.py`](../tools/make_component_index.py) | Coverage map of the component registry: ref -> what it is verified by. |
| [`pick_passives.py`](../tools/pick_passives.py) | Pick an order code for every passive group and justify the choice by the rail it sits on. |
| [`apply_passives.py`](../tools/apply_passives.py) | Write the passive order codes recorded in passives.yaml into the schematic and the board. |
| [`check_stock.py`](../tools/check_stock.py) | JLCPCB stock for every LCSC code in the schematic: availability, library type, price, shortage. |
| [`fetch_datasheet.py`](../tools/fetch_datasheet.py) | Fetch the official document for a component, verifying that the right thing was downloaded. |
| [`mark_not_purchased.py`](../tools/mark_not_purchased.py) | Mark non-purchased items so the fab kit matches the component registry. |
| [`test_components.py`](../tools/test_components.py) | Mutation test of check_components.py: the check must fail when a fixed defect is put back. |

## Placement

Move, fit and declash footprints by the board's real rules.

| Tool | What it does |
|---|---|
| [`move_group.py`](../tools/move_group.py) | Rigidly move a group of footprints (re-placement): shift/rotate about an anchor, check footprint overlaps. |
| [`declash.py`](../tools/declash.py) | Push apart overlapping small footprints after re-placement: for every clashing passive/test point the nearest free position is searched (spiral with a 0.5 mm step, radius up to --r mm); large footprints are not moved. |
| [`fit_cap.py`](../tools/fit_cap.py) | Find a spot for a two-pad part using the board's REAL rules, ranked by distance to a pin. |
| [`fit_near.py`](../tools/fit_near.py) | Where a footprint fits next to a given pin - and what is in the way if it does not. |
| [`place_decoup.py`](../tools/place_decoup.py) | Pull decoupling capacitors close to an IC's power pins. |
| [`push_ring.py`](../tools/push_ring.py) | Push the ring of small passives away from a large package to free its fan-out zone. |
| [`nudge_part.py`](../tools/nudge_part.py) | Nudge a small part through a list of offsets so that its locked pad gets connected; DRC is the judge. |
| [`thermal_vias.py`](../tools/thermal_vias.py) | Thermal vias under exposed pads (EP): a via array into the pad's own plane; DRC is the judge. |

## Routing: Freerouting round trip

Export, route, import and accept per net - DRC decides what stays.

| Tool | What it does |
|---|---|
| [`dsn_export.py`](../tools/dsn_export.py) | Export the project board (paths.BOARD) to Specctra DSN for Freerouting. |
| [`ses_import.py`](../tools/ses_import.py) | Import a Specctra SES back into the project board (paths.BOARD). |
| [`fr_api.py`](../tools/fr_api.py) | Run one job through the Freerouting REST API: enqueue -> input -> start -> cancel -> output. |
| [`fr_grab.py`](../tools/fr_grab.py) | Fetch the current SES from a RUNNING Freerouting job. |
| [`run_route.sh`](../tools/run_route.sh) | One Freerouting round: export DSN, run the engine, import SES into a trial copy, accept or reject. |
| [`after_ses.sh`](../tools/after_ses.sh) | Import a Freerouting session (SES) into a trial copy of the board and judge it with DRC. |
| [`accept.py`](../tools/accept.py) | Acceptance of a routing round: a verdict with an exit code, not just numbers. |
| [`merge_ses.py`](../tools/merge_ses.py) | Per-net acceptance of a routing round: take from the round only the nets that improved. |
| [`pick_nets.py`](../tools/pick_nets.py) | Pick nets for an incremental (per-batch) routing round from a DRC report. |
| [`campaign.py`](../tools/campaign.py) | Unattended campaign of routing rounds: round after round, only upward. |
| [`route_all.sh`](../tools/route_all.sh) | Alternate two routing engines until no breaks remain. |
| [`freerouting.json`](../tools/freerouting.json) | Freerouting settings used by the round scripts. |
| [`test_protect.py`](../tools/test_protect.py) | Decisive router-engine test: does it treat (type protect) copper as an obstacle? |

## Routing: maze router and last breaks

A single-net router and the tools that explain why a connection has no path.

| Tool | What it does |
|---|---|
| [`maze_route.py`](../tools/maze_route.py) | Single-net maze router: what Freerouting could not finish. |
| [`maze_campaign.sh`](../tools/maze_campaign.sh) | Campaign of the built-in maze router (maze_route.py) with per-net rollback. |
| [`gpu_campaign.sh`](../tools/gpu_campaign.sh) | Campaign of the GPU router (tools/gpuroute) with per-net rollback - same as |
| [`route_gpu_all.sh`](../tools/route_gpu_all.sh) | Alternate the GPU router and regional rip-up until no breaks remain. |
| [`reach.py`](../tools/reach.py) | Reachability from a point and wall analysis: which nets hold the corridor shut. |
| [`corridor_open.py`](../tools/corridor_open.py) | Iteratively clear a corridor along the reachability boundary: a minimal list of wall nets. |
| [`keyhole.py`](../tools/keyhole.py) | Where to open a "keyhole" for a through via while removing the MINIMUM of foreign copper. |
| [`blockers.py`](../tools/blockers.py) | Who exactly locks a pin: for every unconnected pin, the first obstacle in its escape lane. |
| [`close_gaps.py`](../tools/close_gaps.py) | Close micro-gaps that the router's raster model cannot see. |
| [`keepout_tmp.py`](../tools/keepout_tmp.py) | Temporary keepout for the router: add a rectangle, route, remove it. |
| [`transplant_net.py`](../tools/transplant_net.py) | Transplant a net's copper from another board - same schematic, routed differently. |
| [`transplant_loop.sh`](../tools/transplant_loop.sh) | Transplant copper from another board ONE net at a time, with a DRC check after each. |
| [`layers8.py`](../tools/layers8.py) | 6 -> 8 layers: a text transformation of a .kicad_pcb (fallback when routing plateaus on 6 layers). |

## Fan-out and rip-up

Escape dense packages and clear exactly the copper that blocks them.

| Tool | What it does |
|---|---|
| [`fanout.py`](../tools/fanout.py) | Systematic fan-out from under a package: radial escape + via. |
| [`fanout_prune.py`](../tools/fanout_prune.py) | Remove the fan-out escapes that DRC pointed at. |
| [`fanout_refs.py`](../tools/fanout_refs.py) | List the footprints involved in unconnected items - input for the fan-out. |
| [`lane_clear.py`](../tools/lane_clear.py) | Clear the escape lanes of locked pins of foreign copper on F.Cu. |
| [`strip_annulus.py`](../tools/strip_annulus.py) | Remove a package's copper in a ring around its pads - to make room for a systematic fan-out. |
| [`strip_pocket.py`](../tools/strip_pocket.py) | Remove the copper of the given nets inside a window - to free a spot that matters more. |
| [`strip_class.py`](../tools/strip_class.py) | Remove the copper of a given net class - for re-routing by the router. |
| [`rip_area.py`](../tools/rip_area.py) | Remove the copper of the given nets inside an area - for targeted re-routing of a node. |
| [`prune_new.py`](../tools/prune_new.py) | Remove objects added since a given board copy that ended up in violations. |
| [`prune_dangling.py`](../tools/prune_dangling.py) | Iteratively remove dangling segments/vias of the given nets inside a window - by KiCad DRC's verdict (track_dangling, via_dangling), until DRC stops finding them. |

## Planes, stitching and vias

Connect plane pads, stitch fragments, and stitch ground last.

| Tool | What it does |
|---|---|
| [`fix_plane_pads.py`](../tools/fix_plane_pads.py) | A via into its own plane for every unconnected pad of a plane net. |
| [`plane_via_stub.py`](../tools/plane_via_stub.py) | Unconnected plane pads: a via into the plane on a stub or next to the pad; DRC has the last word. |
| [`gnd_link.py`](../tools/gnd_link.py) | Sew detached pads of a plane net to the plane: for every pad KiCad considers unconnected, place a via into the plane next to it and join them with a short segment. |
| [`sig_link.py`](../tools/sig_link.py) | Unlock trapped ends of signal nets: next to each break end, place a via in free space (with a check of the straight corridor to it) and join it with a short segment; the router then finishes the net (--any-target). |
| [`free_pad.py`](../tools/free_pad.py) | Free a trapped plane pad: rip the copper of the "wall" nets around it, connect the pad first, reroute the walls. |
| [`island_pads.py`](../tools/island_pads.py) | Plane-net pads cut off from the main copper of their net -> synthetic Pad-to-Pad breaks for the router. |
| [`island_stitch.py`](../tools/island_stitch.py) | Stitch a fill fragment cut off from the plane with a via INSIDE the fragment itself. |
| [`stitch_gnd.py`](../tools/stitch_gnd.py) | Ground stitching with vias; run it AFTER routing. |
| [`unstitch_walls.py`](../tools/unstitch_walls.py) | Remove GND stitching vias (vias without tracks) that stand as a wall next to narrow segments of power/motor nets. |
| [`fix_missing_vias.py`](../tools/fix_missing_vias.py) | Break "same point, different layers": an orphaned stub left after a layer change. |
| [`fix_holes.py`](../tools/fix_holes.py) | Drilling cleanup: remove duplicate vias and same-net vias that are too close, driven by a DRC report. |
| [`clean_dangling.py`](../tools/clean_dangling.py) | Remove dangling tracks and vias. |
| [`dedup_vias.py`](../tools/dedup_vias.py) | Remove overlapping vias. |
| [`refill_zones.py`](../tools/refill_zones.py) | Refill all zones and save the board (pcbnew ZONE_FILLER + SaveBoard) in a separate process. |

## Track widths

Route narrow, then widen to the class width as far as the neighbours allow.

| Tool | What it does |
|---|---|
| [`audit_widths.py`](../tools/audit_widths.py) | Audit copper against the current rules: where routing is narrower than its class width. |
| [`widen_net.py`](../tools/widen_net.py) | Widen the tracks of one net to its net-class width, as far as clearances allow. |
| [`widen_all.py`](../tools/widen_all.py) | Widen the copper of all nets to their class width - a wrapper around widen_net.py (one net per run, ~1 s). |
| [`widen_push.py`](../tools/widen_push.py) | Bring the copper of the given net classes up to class width by clearing neighbours: widen everything possible, then, guided by the DRC report, rip up blocking signal tracks, re-route them, and roll back unresolvable wide |
| [`widen_segs.py`](../tools/widen_segs.py) | Widen narrow segments one by one to the maximum the neighbours' geometry allows (not all-or-nothing class width). |
| [`widen_walls.py`](../tools/widen_walls.py) | What blocks widening a net's narrow segments to class width: for every segment narrower than the class width (except necks <= KEEP near pads/vias of its own net) - foreign objects on the same layer within the band (width |
| [`set_widths.py`](../tools/set_widths.py) | Set segment widths by uuid: python3 set_widths.py --board B --json {uuid: width_nm}. |

## Verification gate

Checks that ERC, DRC and parity do not cover, and the one-command gate that runs them.

| Tool | What it does |
|---|---|
| [`gate.sh`](../tools/gate.sh) | Full acceptance gate in one command. |
| [`gate_stamp.py`](../tools/gate_stamp.py) | Fingerprint of the acceptance-gate inputs: which state a verdict belongs to, and whether it is stale. |
| [`verify.py`](../tools/verify.py) | Board check gate. |
| [`check_netlist.py`](../tools/check_netlist.py) | Static netlist check - what neither ERC nor DRC sees. |
| [`check_flyback.py`](../tools/check_flyback.py) | Flyback diodes on switches: cathode must be on the supply, anode on the drain. |
| [`check_power_sequence.py`](../tools/check_power_sequence.py) | Power-up sequencing: does a rail's enable depend on a rail that does not exist yet. |
| [`check_uniformity.py`](../tools/check_uniformity.py) | Uniformity of support parts around identical ICs: where five of six were done right. |
| [`check_track_on_pad.py`](../tools/check_track_on_pad.py) | Tracks running over pads of other nets. |
| [`check_ir_drop.py`](../tools/check_ir_drop.py) | Voltage drop and conductor cross-section of rails, from the ACTUAL copper geometry. |
| [`check_rf_reference.py`](../tools/check_rf_reference.py) | Continuity of the reference plane under an RF feed, plus an audit of signal tracks on ground-plane layers. |
| [`check_views_fresh.py`](../tools/check_views_fresh.py) | Board views (renders, SVGs) were taken from the board that is on disk now. |
| [`test_decouple.py`](../tools/test_decouple.py) | Decisive test of the decoupling check in verify.py: does it catch a missing path through the plane? |

## Fabrication outputs

Release kit, views and test access.

| Tool | What it does |
|---|---|
| [`make_fab.sh`](../tools/make_fab.sh) | Release of the fabrication kit. |
| [`make_views.sh`](../tools/make_views.sh) | Board views for documents: 3D top and bottom + SVG of copper with silkscreen. |
| [`make_box_model.py`](../tools/make_box_model.py) | Approximate part body as a box (VRML) for board views. |
| [`fix_silk.py`](../tools/fix_silk.py) | Silkscreen: bring it up to fab minimums and move reference texts off copper and outlines. |
| [`testpoint_vias.py`](../tools/testpoint_vias.py) | Signal test points: remove the solder mask from vias. |
| [`probe_pads.py`](../tools/probe_pads.py) | Debug probe pads: open the solder mask directly on a track of important nets (bare copper). |

## GPU router (`tools/gpuroute/`)

Needs `torch` (CUDA recommended) next to `pcbnew`; run it with `$TORCH_PYTHON`. See [routing.md](routing.md#2-gpu-router-toolsgpuroute).

| Tool | What it does |
|---|---|
| [`check_par.py`](../tools/gpuroute/check_par.py) | Verify KAT_PAR: build the router model by the serial and the parallel path in one process and require every tensor to match bit-exact. |
| [`connectivity.py`](../tools/gpuroute/connectivity.py) | Raster connectivity of a net's copper: which pieces are already connected and which are not. |
| [`locked.py`](../tools/gpuroute/locked.py) | Locked ends of breaks: copper components from which the wave cannot escape at all. |
| [`model.py`](../tools/gpuroute/model.py) | Board model for the GPU router: obstacles per net class, own copper of the routed nets, fixed copper of the negotiation neighbours - all as GPU tensors. |
| [`negotiate.py`](../tools/gpuroute/negotiate.py) | PathFinder negotiation between nets competing for board space. |
| [`nudge.py`](../tools/gpuroute/nudge.py) | Push the small parts that lock the pins of a package outwards, to open an escape corridor. |
| [`nudge_campaign.py`](../tools/gpuroute/nudge_campaign.py) | Nudge the locking parts package by package under DRC acceptance. |
| [`par.py`](../tools/gpuroute/par.py) | Parallelism of the CPU stages of the pipeline: environment flags and a thread pool. |
| [`probe.py`](../tools/gpuroute/probe.py) | Raster around a point for one net: where its own copper is, what is free, where the wall is and why. |
| [`qfn_campaign.py`](../tools/gpuroute/qfn_campaign.py) | The "open up a package" operator over every package with locked pins, under DRC acceptance. |
| [`rip_campaign.py`](../tools/gpuroute/rip_campaign.py) | Regional rip-up campaign: find locked break ends, remove foreign copper around them and re-route everything together; DRC judges, round after round, monotone only. |
| [`rip_ends.py`](../tools/gpuroute/rip_ends.py) | A series of small rip-ups at the ends of ALL remaining signal breaks (not only the "locked" ones, as in rip_campaign): centres are break ends grouped within 3 mm, IC pins first; DRC judges each step (fewer breaks with ze |
| [`ripup.py`](../tools/gpuroute/ripup.py) | Regional rip-up: remove foreign copper around a locked pin and re-route it together with the pin. |
| [`route.py`](../tools/gpuroute/route.py) | GPU router: pick breaks, negotiate board space between nets, write the copper. |
| [`spread_row.py`](../tools/gpuroute/spread_row.py) | Spread a row of identical packages together with their support parts and clear the region's copper. |
| [`wave.py`](../tools/gpuroute/wave.py) | Multi-layer wavefront on the GPU (torch): Dijkstra as iterated relaxation. |
| [`widen_board.py`](../tools/gpuroute/widen_board.py) | Widen the board: everything right of X0 moves right by dx - footprints (with their zones), tracks, vias, zone vertices, outline graphics and silkscreen. |

## Placement search (`tools/gpuroute/place/`)

Simulated annealing over a "pocket" (a window around a group of ICs) with trial GPU routing in the loop.

| Tool | What it does |
|---|---|
| [`apply_place.py`](../tools/gpuroute/place/apply_place.py) | Apply a pocket placement found by the search to the board (one subprocess per pocket). |
| [`check_l0.py`](../tools/gpuroute/place/check_l0.py) | Checkpoint B: L0 (locked ends) of a pocket model at the current placement. |
| [`check_l1.py`](../tools/gpuroute/place/check_l1.py) | Checkpoint C: L1 (negotiated routing of the pocket nets to the window ring) at the current placement. |
| [`check_par_pocket.py`](../tools/gpuroute/place/check_par_pocket.py) | KAT_PAR check for the pocket model: Pocket + model_for + L0/L1 via the old and the new path — own/cover/free_for must be bit-exact, the L0/L1 numbers equal; also reports timings. |
| [`check_sdf.py`](../tools/gpuroute/place/check_sdf.py) | Checkpoint A: GPU distance maps (place/sdf) against the CPU model (model.Model). |
| [`cmodel.py`](../tools/gpuroute/place/cmodel.py) | Placement-candidate model in a pocket window — the gpuroute.model.Model interface (cost_for, footprint, free_for, own, cover, fine, labels), built from the GPU maps of place/sdf. |
| [`constraints.py`](../tools/gpuroute/place/constraints.py) | Hard constraints of a placement candidate — checked before any wave (CPU, plain Python: N movable <= ~40). |
| [`escape.py`](../tools/gpuroute/place/escape.py) | Pocket: a window around a group of ICs, its movable parts, static copper and the candidate model. |
| [`fitness.py`](../tools/gpuroute/place/fitness.py) | Scoring of a placement candidate: L0 — locked ends (cheap), L1 — negotiated routing of the pocket escapes to the window ring (more expensive, for the best candidates). |
| [`geom.py`](../tools/gpuroute/place/geom.py) | Footprint geometry in LOCAL coordinates — a one-off export from pcbnew. |
| [`place_campaign.py`](../tools/gpuroute/place/place_campaign.py) | Per-pocket placement acceptance: search -> apply -> re-route -> DRC -> accept/roll back. |
| [`sdf.py`](../tools/gpuroute/place/sdf.py) | Distance-to-copper maps on the GPU — analytic, instead of a 0.025 mm raster + EDT on the CPU. |
| [`search.py`](../tools/gpuroute/place/search.py) | Simulated annealing over a pocket: moves of support parts and ICs, L0 scored on the GPU, L1 for the best. |
| [`wavebatch.py`](../tools/gpuroute/place/wavebatch.py) | Batched waves: reachability and routing over many windows at once (leading dimension B). |

## Utilities (`tools/util/`)

One-purpose diagnostics and point edits, used interactively and by the tools above.

| Tool | What it does |
|---|---|
| [`add_label.py`](../tools/util/add_label.py) | Add a net label to a schematic sheet. |
| [`board_diff_ops.py`](../tools/util/board_diff_ops.py) | Copper difference between two boards (live vs. |
| [`cc_spots.py`](../tools/util/cc_spots.py) | Free positions for an 0402 capacitor near a given pin, on the same side of the board. |
| [`corridor_nets.py`](../tools/util/corridor_nets.py) | Which nets occupy the straight corridor between two points. |
| [`diag_free.py`](../tools/util/diag_free.py) | Map of the router model's free cells for one net on one layer inside a window. |
| [`edit_ops.py`](../tools/util/edit_ops.py) | Point edits of a board from an operation list (pcbnew, one LoadBoard, SaveBoard at the end). |
| [`fix_clash.py`](../tools/util/fix_clash.py) | Remove copper that sits in DRC violations (by the report's item coordinates) and refill zones. |
| [`fp_attrs.py`](../tools/util/fp_attrs.py) | Set or clear footprint fabrication attributes. |
| [`fp_diff.py`](../tools/util/fp_diff.py) | How a footprint on the board differs from its library copy - decoding `lib_footprint_mismatch`. |
| [`in_island.py`](../tools/util/in_island.py) | Which zone-fill island contains a point, and how large that island is. |
| [`island_where.py`](../tools/util/island_where.py) | Where the fill islands of zones on a layer are: area, bounding box, centre. |
| [`killtree.sh`](../tools/util/killtree.sh) | Usage: tools/util/killtree.sh PID... |
| [`mirror_fp_keepouts.py`](../tools/util/mirror_fp_keepouts.py) | Duplicate a footprint's keepout (rule-area) zones as board-level zones. |
| [`net_area.py`](../tools/util/net_area.py) | Track copper area of a net (sum of length * width) and a histogram of track widths. |
| [`net_geom.py`](../tools/util/net_geom.py) | Net geometry: copper per layer, vias, pads and detour ratio. |
| [`netnodes.py`](../tools/util/netnodes.py) | Which component pins sit on a net, read from a KiCad netlist (multi-line s-expression format). |
| [`path_trace.py`](../tools/util/path_trace.py) | Walk a net's copper as a path from pad to pad and show where it leaves the straight line. |
| [`pinmap.py`](../tools/util/pinmap.py) | Pin map of components from a KiCad netlist (multi-line s-expression format). |
| [`place_at.py`](../tools/util/place_at.py) | Move a footprint to a point, optionally with a new rotation. |
| [`ratsnest_pos.py`](../tools/util/ratsnest_pos.py) | Locate the exact break in a net. |
| [`rip_at_nets.py`](../tools/util/rip_at_nets.py) | Remove copper of the listed nets around planned via points, to make room for pre-placed vias. |
| [`rollback_links.py`](../tools/util/rollback_links.py) | Undo the stitching links and vias (gnd_link / sig_link ops) that produced DRC errors. |
| [`set_via_nets.py`](../tools/util/set_via_nets.py) | Assign a net to the vias at planned points (after foreign copper under them was removed). |
| [`silk_edit.py`](../tools/util/silk_edit.py) | Point edits of silkscreen and fabrication texts from an operation list. |
| [`silk_look.py`](../tools/util/silk_look.py) | Silkscreen objects of a footprint: texts (height/width/thickness, visibility) and lines. |
| [`silk_report.py`](../tools/util/silk_report.py) | Triage of silkscreen DRC findings: what hinders assembly, what is cosmetic, what will not print. |
| [`thermal_audit.py`](../tools/util/thermal_audit.py) | Thermal-pad audit: how many vias sit inside the large pads of a footprint. |
| [`via_near_pin.py`](../tools/util/via_near_pin.py) | Distance from a pad to the nearest via of its own net. |
| [`via_spots.py`](../tools/util/via_spots.py) | Geometric search for through-via spots of net NET inside a window (no router model, ~1 s). |
| [`widen_diff.py`](../tools/util/widen_diff.py) | Which track segments changed width between two boards. |
| [`window_first.py`](../tools/util/window_first.py) | The "window, target net first" technique for a locked connection end. |
| [`zone_islands.py`](../tools/util/zone_islands.py) | Zone fill polygons: how many islands each zone has and their areas. |

## Simulation and instruments

| Tool | What it does |
|---|---|
| [`sim/microstrip.py`](../sim/microstrip.py) | Characteristic impedance of a microstrip (Hammerstad-Jensen with copper-thickness correction), e.g. |
| [`sim/ant_compare.py`](../sim/ant_compare.py) | Compare S11 curves of several openEMS runs: resonances and per-band performance. |
| [`instruments/ad3_mcp/server.py`](../instruments/ad3_mcp/server.py) | MCP server for the Digilent Analog Discovery 3 (stdio, JSON-RPC 2.0) - a measurement tool that lets an AI assistant take readings during board bring-up and acceptance testing. |
