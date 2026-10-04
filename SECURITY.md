# Security Policy

The toolkit edits local KiCad files and runs local processes (`kicad-cli`,
Freerouting, Python). It does not open network services, except
`instruments/ad3_mcp/server.py`, which speaks MCP over stdio only.

If you find a security issue (for example, a path traversal when a tool writes
files, or unsafe handling of a downloaded datasheet), please report it privately
to **info@nexatech.dev** instead of opening a public issue. We will acknowledge
the report within 5 working days.
