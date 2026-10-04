# Rules for agents using this toolkit

Short, operational. The reasoning behind each rule is in
[docs/methodology.md](docs/methodology.md) and [docs/lessons-learned.md](docs/lessons-learned.md).

1. **KiCad is the judge.** After any write to a board, run
   `kicad-cli pcb drc --schematic-parity --refill-zones --severity-all` and read the JSON.
   Your own model of the board is a hypothesis; DRC is the fact.
2. **Plan first, then `--apply`.** Every board-editing tool prints its plan by default.
   Read the plan before writing.
3. **Work on a copy, accept monotonically.** Route and repair on a copy in `$KAT_WORK`
   (point child processes at it with `BRD=`). Keep the result only if unconnected items
   went down and errors did not go up. Accept per net when a round is mixed.
4. **A check that did not run reports zero.** Before quoting "0 findings", confirm the
   check read a non-empty input (netlist, board, registry) and exited with a real status.
   `gate.sh` prints `skipped` for checks it could not run; do not report those as passed.
5. **Quote the gate only with its fingerprint.** `gate.sh` writes `work/gate/inputs.sha`;
   a log from before the last edit is not evidence.
6. **Project facts go into `kicad-agent.json`, not into code.** Plane nets, protected nets,
   rail budgets, accepted findings with numeric reasons.
7. **Some checks need a human with the GUI.** Update PCB from Schematic (F8) in the project
   manager finds pads without nets and footprint changes that no CLI path reports. Ask for it
   at milestones.
8. **Stitch ground last.** Stitching vias placed during routing become walls.
9. **Measure before you widen the search.** When a connection has no path, read the cell
   count (`maze_route.py`, `reach.py`): a small number means a locked pad (fix placement),
   a large one means a blocked corridor (rip the named wall nets).
10. **Back up before transfer.** When copying a result from a trial copy to the live board,
    compare against the snapshot the copy was made from; never overwrite blindly.
