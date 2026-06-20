# Constraint Registry — V0

A single, queryable source of engineering constraints (infrastructure,
organizational, architectural) that coding agents consult at code-generation
time, exposed over an MCP server. It does not enforce constraints itself; it
delegates validation to existing enforcement engines (e.g. Conftest/OPA).

See `constraint-registry-v0-spec.md` for the authoritative requirements and
`TRACEABILITY.md` for requirement→component→test mapping.

## Quick start

```bash
uv sync
# Run the validation harness (Section 7) against the bundled sample sources:
uv run cregistry-harness
```

The harness emits machine-readable JSON and exits non-zero on any failure
(VH-OUTPUT-1), running end-to-end against the self-contained sample sources under
`sources/` (VH-OUTPUT-2).

## Running the MCP server

```bash
uv run cregistry-mcp                 # stdio (default; each tool spawns its own)
uv run cregistry-mcp --http --port 8765   # one shared HTTP server for all tools
```

See `docs/RUNNING.md` for per-tool setup (Claude Code / Cursor / Codex),
stop/restart, and macOS launchd auto-start, and `docs/MCP_CONTRACT.md` for the
tool contracts.
