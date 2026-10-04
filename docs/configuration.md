# Configuration

The tools contain no knowledge of a particular board. Everything project-specific
comes from three places, in this order of precedence:

1. **Command-line arguments** of the tool.
2. **Environment variables** (paths, router tuning flags).
3. **`kicad-agent.json`** in the project directory — board policy: plane nets,
   protected nets, rail budgets, accepted findings (waivers).
4. **The board's own files**: copper stack and layer types from the `.kicad_pcb`
   header, net classes from the `.kicad_pro`, clearances from the `.kicad_dru`.

A project without `kicad-agent.json` still works: every key has a generic default.
A complete example is in [`examples/kicad-agent.json`](../examples/kicad-agent.json).

## Locating the project

| Variable | Meaning | Default |
|---|---|---|
| `KAT_ROOT` | Project directory (holds `.kicad_pro`, `.kicad_pcb`) | current directory |
| `KAT_WORK` | Scratch directory: DSN/SES, DRC reports, trial copies, logs | `$KAT_ROOT/work` |
| `KAT_CONFIG` | Path to the config file | `$KAT_ROOT/kicad-agent.json` |
| `BRD` | Board to work on. **Wins over `BOARD`**: a tool hands a trial copy to a child process through `BRD` while `BOARD` is inherited | — |
| `BOARD` | Board file | `board` key of the config, else the only `*.kicad_pcb` in `KAT_ROOT` |
| `SCH` | Root schematic | board name with `.kicad_sch` |
| `NETLIST` | KiCad s-expression netlist | `$KAT_WORK/<board>.net` |
| `KICAD_CLI` | `kicad-cli` binary | `PATH`, then the macOS app bundle |
| `KICAD_SYMBOL_DIR` | KiCad symbol libraries | `/usr/share/kicad/symbols` |
| `KICAD_FOOTPRINT_DIR` | KiCad footprint libraries | `/usr/share/kicad/footprints` |
| `PYTHON` | Interpreter with `pcbnew` (shell scripts) | `python3` |
| `TORCH_PYTHON` | Interpreter with `pcbnew` **and** `torch` for `tools/gpuroute` | `$PYTHON` |

Shell scripts source [`tools/env.sh`](../tools/env.sh), Python tools import
[`tools/paths.py`](../tools/paths.py); both resolve the same values.

## Values read from the board

Before reaching for the config, the tools read what the board already declares:

- **Track clearances per net class** — rules in `<board>.kicad_dru` of the form
  `A.NetClass == 'X' && A.Type == 'Track' && B.Type == 'Track'`
  (`project.class_track_clearance()`).
- **Fine-pitch escape** — footprints named in `A.insideCourtyard('U3')` conditions and the
  clearance of that rule (`project.fine_pitch_refs()`, `project.fine_pitch_clearance()`).
- **Track-to-pad / track-to-track defaults** — plain `A.Type == 'Track' && B.Type == 'Pad'`
  rules (`project.rule_clearance()`).
- **Copper stack** — layer names, user aliases and types from the `(layers ...)` header.
  Inner layers of type `power` are treated as planes by the routers: they cost more to cross.
- **Net classes and their patterns** — `net_settings` in `<board>.kicad_pro`.

Keeping one source of truth matters: the router and the DRC must judge by the same
numbers, otherwise the router produces copper that DRC rejects.

## `kicad-agent.json` keys

### Nets and layers

| Key | Type | Default | Used by | Meaning |
|---|---|---|---|---|
| `board` | string | — | all | Board file name relative to `KAT_ROOT` |
| `plane_nets` | list | `["GND"]` | routing, planes, checks | Nets poured as planes; the router treats them as zones, not signals |
| `plane_net_classes` | object | `{}` | `fix_plane_pads` | Net class of a plane net when it differs from the net name |
| `plane_layers` | list | `["In1.Cu".."In4.Cu"]` | `widen_net` | Plane layers skipped by widening unless `--planes` |
| `signal_layers` | list | `F.Cu, In1.Cu..In4.Cu, B.Cu` | `raster` | Fallback stack; normally replaced by the board header |
| `supply_nets` | list | flyback: `+5V, +3V3, +12V, +24V, VIN`; others `[]` | `check_flyback`, `check_netlist`, `util/via_spots` | Supply rails |
| `class_clearance` | object | `{}` | `project.class_track_clearance` | Extra/override track clearance per net class, mm |
| `fine_pitch_refs` | list | `[]` | routers | Extra fine-pitch footprints (in addition to the `.kicad_dru`) |
| `fine_pitch_clearance` | number | `0.09` | routers | Used when the rules file has no courtyard rule |
| `copper_edge_clearance` | number | `0.5` | `dsn_export`, `maze_route` | Copper-to-edge clearance, mm |

### Protection: what the router must not touch

| Key | Type | Default | Used by | Meaning |
|---|---|---|---|---|
| `rf_net_prefixes` | list | `["Net-(ANT", "RF_"]` | `dsn_export`, `ses_import`, `pick_nets`, `check_rf_reference` | RF nets: exported as protected copper, never rerouted |
| `protect_nets` | list | `[]` | `free_pad`, `corridor_open`, `keyhole`, `gpuroute/ripup`, `gpuroute/nudge` | Nets never ripped up |
| `no_touch_classes` | list | `[]` | `widen_push`, `corridor_open`, `keyhole`, `gpuroute/ripup` | Net classes never ripped up (e.g. an impedance-controlled class) |
| `never_rip_min_clearance` | number | `0.3` | `gpuroute/nudge`, `gpuroute/place` | Classes with a track clearance at least this are protected |

### RF reference plane

| Key | Type | Default | Meaning |
|---|---|---|---|
| `rf_reference_layer` | string | `"In1.Cu"` | Reference plane under the outer-layer RF feed |
| `rf_plane_layers` | list | `[rf_reference_layer]` | Ground layers checked for signal tracks. List **all** ground layers to check them all |
| `rf_fringe_mm` | number | `0.6` | Half-margin of the feed corridor on each side |

### Stitching, test points, probes

| Key | Type | Default | Meaning |
|---|---|---|---|
| `stitch_net` | string | `"GND"` | Net stitched by `stitch_gnd` |
| `stitch_pitch_mm` | number | `3.0` | Stitching grid. λ/10 of the highest frequency in the substrate (λ/20 to be cautious); 3 mm suits 5 GHz on FR-4 |
| `testpoint_nets` | list | `[]` | Signals to expose as test-point vias, in TP order |
| `testpoint_blocking_refs` | list | `[]` | Parts whose bottom side a probe cannot reach under |
| `probe_nets` | list | supplies, clocks, resets, buses | `fnmatch` patterns for mask-window probe pads |

### Power checks

| Key | Type | Default | Meaning |
|---|---|---|---|
| `rail_vmax` | `{net: [V, source] \| V}` | `{}` | Maximum working voltage per rail (capacitor derating, TVS selection) |
| `rail_nominal_v` | `{net: V}` | `{}` | Nominal rail voltage, to express IR drop in % |
| `rail_vmin` | `{net: [V, source]}` | `{}` | Minimum voltage at the load per datasheet |
| `ir_rails` | `{net: [A, peak A \| null, source]}` | `{}` | Rail currents for `check_ir_drop` |
| `copper_outer_um` / `copper_inner_um` | number | `35` / `17.5` | Copper thickness |
| `ambient_temp_c` | number | `40` | Ambient temperature for thermal checks |
| `power_in_pins` / `power_out_pins` / `enable_pins` | list | generic names | Pin-name prefixes giving a pin its role in `check_power_sequence` |
| `sequence_controllers` | list | `[]` | Refs whose GPIOs may legitimately drive an enable |
| `min_island_mm2` | number | `12` | Smallest ground fragment reported by `verify` |

### Netlist and design checks

| Key | Type | Default | Meaning |
|---|---|---|---|
| `netlist_skip_nets` | list | `["GND"]` | Nets ignored by `check_netlist` |
| `netclass_expect` | list of `{net_regex, netclass, min_count}` | `[]` | Nets that must belong to a net class |
| `exti_check` | `{mcu, net_pattern, min_count}` | absent | Inputs that must sit on distinct EXTI lines |
| `no_net_pads` | `{ref: {pads: [...], reason}}` | `{}` | Numbered pads reviewed and accepted as having no net (`verify`, `check_pads_no_net`) |
| `dnp_refs` | `{ref: reason}` | `{}` | Parts marked DNP by `set_dnp` when no refs are given |

### Components, sourcing, datasheets

| Key | Type | Default | Meaning |
|---|---|---|---|
| `component_registry` | string | `doc/components` | Directory of per-part YAML files (see below) |
| `datasheet_dir` | string | `doc/datasheets` | Datasheet store |
| `jlcpcb_db` | string | `~/.konnect/jlcpcb.db` | Local JLCPCB/LCSC catalogue (SQLite, as downloaded by the Konnect MCP server). `KAT_JLCPCB_DB` overrides it |
| `not_purchased_footprints` | list | `["TestPoint_", "MountingHole_"]` | Footprint substrings of parts that are copper, not purchased |
| `passive_fixed_choices` | `{group: [lcsc, reason]}` | `{}` | Passive choices that `pick_passives` must keep |
| `passive_blocked_codes` | `{lcsc: reason}` | `{}` | Codes `pick_passives` must never pick |

### Placement

| Key | Type | Default | Meaning |
|---|---|---|---|
| `place_fixed_refs` | list | `[]` | Refs the placement search never moves |
| `place_fixed_prefixes` | list | `["H", "ANT"]` | Ref prefixes never moved (mounting holes, antennas) |
| `top_side_only_net_prefixes` | list | `[]` | Parts with a pad on such a net must stay on F.Cu |
| `park_xy` | `[x, y]` | `[0, 0]` | Where `sync_board` parks newly added footprints |

### Waivers: accepted findings

A finding that is a design decision is recorded with its reason, so the check stays
green **and** the reasoning stays reviewable. Keys are check-specific:

| Check | Key format | Example |
|---|---|---|
| `verify_decoupling` | `"<ref>:<pin function>"` | `"U4:VDDA"` |
| `check_uniformity` | `"<symbol>:<pin>:<ref>"` | `"Q_NMOS:G_1:Q3"` |
| `check_components` | `"<axis>:<ref or group>"` | `"DOC-4:U2"` |

```json
"waivers": {
  "verify_decoupling": {"U4:VDDA": "100 nF at 2.1 mm; reference filtered separately"}
}
```

Write the reason with numbers. "Not needed" is not a reason; "static level, no
inductive load, 10 kΩ pull-down present" is.

### Mutation tests

`test_decouple` (`{cap_net, pin_net, pin_refs}`) and `test_components`
(`{case: {...}}`) name the parts a mutation test breaks on purpose to prove that a
check catches it. See the docstrings of [`tools/test_decouple.py`](../tools/test_decouple.py)
and [`tools/test_components.py`](../tools/test_components.py).

## Router tuning flags

The routers read `KAT_*` environment variables (via size, layer and via costs, plane
handling, parallelism). They are listed with defaults in
[routing.md](routing.md#environment-flags).

## Component registry layout

`check_components.py` validates the schematic against a registry of per-part YAML
files in `component_registry`:

```
doc/components/
  index.yaml            # components: {ref: {category, norm, value, footprint}}
  passives.yaml         # groups: {name: {refs, count, voltage_rating, rail, dielectric, ...}}
  not-purchased.yaml    # categories: {footprint: {designators: [...]}}
  <part>.yaml           # one file per active part
```

A part file may hold `document`, `package`, `pins`, `merged_pins`, `thermal_pads`,
`voltages` (with `abs_max`), `required` (support parts with value windows and
distance limits), `connection_rules`, `thermal`, `availability`, `polarity`. Run
`python3 tools/check_components.py --help` for the axes it checks.
