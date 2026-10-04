# KiCad Agent Toolkit

Command-line tools that let an AI agent (or a patient human) take a KiCad 10 board from
schematic to fabrication outputs without the GUI, and without trusting its own claims.

- **Routing that only moves forward.** Freerouting rounds with per-net acceptance, a maze
  router and a GPU (PyTorch) router for the last breaks, and diagnostics that say *why* a
  connection has no path and which nets form the wall.
- **Board ↔ schematic sync without F8.** `sync_board.py` adds, removes and re-assigns
  footprints and pad nets from the netlist; schematic fields are edited as text, safely.
- **A verification gate for what ERC, DRC and parity miss**: decoupling path length, pins
  without a net, IR drop on real copper, power-up sequencing, RF reference continuity,
  support-part asymmetry between identical ICs, component ratings against rail voltages.
- **Every claim is checkable.** Tools that edit a board print a plan by default, write
  with `--apply`, and leave the verdict to `kicad-cli pcb drc`.

The toolkit grew out of the design of a dense 6-layer mixed-signal board (about 370
footprints and 1,700 pads) that an agent took to a DRC-clean fabrication release. What it learned on the way is written down in
[docs/lessons-learned.md](docs/lessons-learned.md).

## Requirements

| Component | Needed for |
|---|---|
| **KiCad 10** (`kicad-cli`, and Python with the `pcbnew` module) | everything that reads or writes a board |
| Python 3.10+ with `numpy`, `PyYAML` | most tools; `PyYAML` for the component registry |
| Java 25 + [Freerouting](https://github.com/freerouting/freerouting) 2.x | Freerouting rounds |
| PyTorch (CUDA recommended) + `scipy` | the GPU router in `tools/gpuroute/` |
| openEMS, `dwfpy` | optional: `sim/`, the Analog Discovery 3 MCP server |

On Linux, use the Python that can `import pcbnew` (the system Python of the KiCad
package). For the GPU router create a venv with `--system-site-packages` so it sees
`pcbnew`, and install `torch` into it. On ARM64 Linux, KiCad 10 is available from
`ppa:kicad/kicad-10.0-releases`.

## Quick start

```sh
git clone https://github.com/nexatech-ltd/kicad-agent-toolkit.git
export KAT=$PWD/kicad-agent-toolkit/tools

cd ~/my-board                          # the directory with my-board.kicad_pro
export KAT_ROOT=$PWD                   # optional: defaults to the current directory

# 1. Netlist, then the checks ERC/DRC do not do
kicad-cli sch export netlist --format kicadsexpr -o work/my-board.net my-board.kicad_sch
python3 $KAT/check_netlist.py          # open drains without pull-ups, nets without a source, ...
python3 $KAT/verify.py                 # decoupling paths, ground islands, pins without nets, ...

# 2. The whole acceptance gate: DRC + ERC + parity + all checks, with an input fingerprint
bash $KAT/gate.sh

# 3. Routing: one Freerouting round, accepted per net, then the last breaks
FR_JAR=~/freerouting.jar bash $KAT/run_route.sh
python3 $KAT/maze_route.py --list
TORCH_PYTHON=~/venv-torch/bin/python bash $KAT/route_gpu_all.sh
```

Every tool answers `--help`. A board-editing tool prints its plan first:

```sh
python3 $KAT/stitch_gnd.py             # plan: where the stitching vias would go
python3 $KAT/stitch_gnd.py --apply     # write, then judge with DRC
```

## Configuration

The tools carry no knowledge of a particular board. They read the board's own files
(copper stack and layer types, net classes from `.kicad_pro`, clearances from
`.kicad_dru`) and an optional `kicad-agent.json` for policy: which nets are planes, which
nets the router must not touch, rail voltages and currents, and accepted findings with
their reasons.

```json
{
  "plane_nets": ["GND", "+3V3"],
  "protect_nets": ["RF_ANT"],
  "rail_vmax": {"VIN": [20.0, "USB-PD contract"]},
  "waivers": {"verify_decoupling": {"U4:VDDA": "100 nF at 2.1 mm; reference filtered separately"}}
}
```

All keys and environment variables: [docs/configuration.md](docs/configuration.md).
A full example: [examples/kicad-agent.json](examples/kicad-agent.json).

## Repository layout

| Path | Contents |
|---|---|
| [`tools/`](tools) | the tools, flat: sync, placement, routing, planes, widths, checks, fab outputs |
| [`tools/gpuroute/`](tools/gpuroute) | GPU router: torch wavefront, PathFinder negotiation, regional rip-up |
| [`tools/gpuroute/place/`](tools/gpuroute/place) | placement search with trial GPU routing in the loop |
| [`tools/util/`](tools/util) | small diagnostics and point edits |
| [`sim/`](sim) | microstrip impedance, openEMS S11 comparison |
| [`instruments/ad3_mcp/`](instruments/ad3_mcp) | MCP server for the Digilent Analog Discovery 3, for agent-driven bring-up |
| [`docs/`](docs) | method, routing pipeline, configuration, tool catalogue, lessons |

## Documentation

- [Methodology](docs/methodology.md): the working principles: KiCad is the judge, monotone
  acceptance, checks that prove they ran, stage-by-stage rules.
- [Routing pipeline](docs/routing.md): Freerouting rounds, the GPU and maze routers, how
  to read "no path", rip-up strategy, plane-net breaks, router flags.
- [Tool catalogue](docs/tools.md): every tool with a one-line description.
- [Configuration](docs/configuration.md): `kicad-agent.json` keys and environment variables.
- [Lessons learned](docs/lessons-learned.md): over a hundred failures, each with its
  symptom, cause and the rule that came out of it.

## Using it from an agent

The tools are designed for an agent working in a shell: plain arguments, plain-text
output, exit codes that mean something (`0` = accepted / no findings), and a plan-first
default for every write. A typical loop is *measure → change on a copy → DRC → accept or
roll back*. [AGENTS.md](AGENTS.md) has the short version of the rules an agent should
follow with this toolkit.

For interactive edits through a running KiCad (IPC API), the toolkit works well alongside
an MCP server for KiCad such as Konnect; this repository itself has no runtime dependency
on one.

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Please
report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Commercial use,
modification and redistribution are permitted.

Copyright 2026 [NexaTech](https://nexatech.dev).
