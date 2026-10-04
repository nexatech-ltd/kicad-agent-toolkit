# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-04

First public release. The tools were extracted from a private board project,
generalized and translated to English.

### Added
- 180 tools in `tools/`, `tools/util/`, `tools/gpuroute/`, `tools/gpuroute/place/`, `sim/`,
  `instruments/ad3_mcp/`: schematic/board sync, placement, Freerouting round trip with
  per-net acceptance, maze router, GPU router, plane stitching, track widening,
  verification gate, fabrication outputs.
- `tools/project.py` and `kicad-agent.json`: project policy moved out of the code.
  Net-class clearances and fine-pitch footprints are read from the board's `.kicad_dru`.
- `tools/paths.py` / `tools/env.sh`: the board is found from `BRD`, `BOARD`, the config
  or the only `*.kicad_pcb` in the project directory; no file names are hard-coded.
- `tools/place_geom.py`: footprint geometry helpers; the board outline comes from Edge.Cuts.
- Every command-line tool answers `--help`.
- Documentation: methodology, routing pipeline, configuration, tool catalogue, lessons learned.

### Changed
- Environment variables use the `KAT_` prefix.
- Router layer costs follow the layer type in the board header: inner layers of type
  `power` (planes) cost 3.0, other inner layers 1.8. Previously the costs were keyed by
  user layer names. Override with `KAT_LAYER_COST`.
- `BRD` takes precedence over `BOARD`, so a trial copy handed to a child process is
  always the board that gets edited.
- `gate.sh` reports a check it could not run as `skipped`, not `passed`.

### Fixed
- `check_power_sequence.py` and `check_uniformity.py` always exited with status 0, so the
  gate showed them as passed even when they had findings.
- `tools/util/silk_edit.py` collected silkscreen obstacles inside the via loop: only the
  last footprint's silkscreen was considered, once per via.
- `instruments/ad3_mcp/server.py` wrote CSV captures with blank rows on Windows.

### Verified
On a copy of the original 6-layer board, the published tools reproduce the original
tools: identical Specctra DSN export, identical GPU-router copper (15,670 tracks and vias,
no difference) after re-routing ripped nets, the same verdicts from `verify.py`,
`check_track_on_pad.py`, `check_ir_drop.py`, `audit_widths.py`, `stitch_gnd.py`,
`clean_dangling.py`, `dedup_vias.py` and `fix_plane_pads.py`, and a full `gate.sh` run.

[0.1.0]: https://github.com/nexatech-ltd/kicad-agent-toolkit/releases/tag/v0.1.0
