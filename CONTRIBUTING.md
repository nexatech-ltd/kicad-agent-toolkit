# Contributing

Thanks for your interest in improving the KiCad Agent Toolkit.

## Ground rules

- **DRC is the judge.** A change to a tool that edits a board must leave the
  acceptance path intact: the tool writes, `kicad-cli pcb drc` decides. Do not add
  "success" output that a reviewer cannot reproduce with DRC/ERC/parity.
- **A check that did not run must not report zero.** If a check depends on an
  input (netlist, board, registry), fail loudly when the input is missing or empty.
- **No project data.** Never commit boards, schematics, netlists, fab outputs or
  datasheets. Project-specific values belong in the project's `kicad-agent.json`,
  not in code (see [docs/configuration.md](docs/configuration.md)).
- **Plain scripts, few dependencies.** Tools are standalone scripts run with the
  Python that ships with KiCad (for `pcbnew`) or a venv with `torch` for the GPU
  router. Do not add third-party dependencies without discussion.
- **English only** in code, comments, messages and docs.

## Workflow

1. Fork and create a topic branch.
2. Keep the diff minimal; explain the *why* in comments where the reason is not obvious.
3. Run the syntax checks:
   ```sh
   python -m compileall -q tools sim instruments
   for f in tools/*.sh tools/util/*.sh; do bash -n "$f"; done
   ```
4. If the tool touches a board, run it on a copy of a real KiCad 10 project and
   attach the DRC result (before/after) to the pull request.
5. Open a pull request with a short description of the problem and the evidence.

## Reporting bugs

Open an issue with the KiCad version, OS, the exact command, and the output.
If possible, attach a minimal board that reproduces the problem.

By contributing you agree that your contributions are licensed under the
Apache License 2.0.
