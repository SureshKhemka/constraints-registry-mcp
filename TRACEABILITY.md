# Requirement Traceability

Living map of every FR-*, NFR-*, and VH-* requirement → component(s) that satisfy
it → VH-* check that proves it. Status legend: ✅ implemented + tested · 🚧 in
progress · ⬜ planned.

Increment status: **All increments (1–9) complete.** Full harness: **18 pass,
0 skip, 0 fail, exit 0.** Every FR/NFR/VH requirement is implemented and proven.
Run: `uv run cregistry-harness`.

> Two real engines pass through the *same* engine-interface conformance suite:
> the OPA reference adapter (`VH-ENGINE-1/2`) and the Conftest adapter
> (`VH-ENGINE-2:second-adapter`), the latter validated with zero new harness code
> — concretely demonstrating FR-ENGINE-2.

## Functional Requirements

| Req | Component(s) | Proven by | Status |
|---|---|---|---|
| FR-CONSTRAINT-1 | `model.py` | VH-SCHEMA-1 | ✅ |
| FR-CONSTRAINT-2 | `model.py`, `loader.py`, `importer.py` | VH-SCHEMA-1, VH-IMPORT-2 | ✅ |
| FR-CONSTRAINT-3 | `model.py` (binding = locator only), `integrity.py` | VH-INTEGRITY-2 | ✅ |
| FR-SOURCE-1 | `config.py`, `loader.py` | VH-OUTPUT-2 | ✅ |
| FR-SOURCE-2 | `importer.py`, `bundle.py` | VH-SCHEMA-1, VH-IMPORT-1 | ✅ |
| FR-SOURCE-3 | `importer.py`, `bundle.py` | VH-IMPORT-1 | ✅ |
| FR-SOURCE-4 | `importer.py`, `bundle.py` | VH-NAMESPACE-1 | ✅ |
| FR-NAMESPACE-1 | `importer.py`, `bundle.py` | VH-NAMESPACE-1 | ✅ |
| FR-NAMESPACE-2 | `precedence.py`, `scope.py` | VH-NAMESPACE-2 | ✅ |
| FR-NAMESPACE-3 | `precedence.py`, `importer.py` | VH-NAMESPACE-2 | ✅ |
| FR-VERSION-1 | `model.py` | VH-SCHEMA-1 | ✅ |
| FR-VERSION-2 | `bundle.py`, `store.py` | VH-IMPORT-1, VH-VERSION-1 | ✅ |
| FR-VERSION-3 | `store.py`, `service.py`, `mcp_server.py` | VH-VERSION-1 | ✅ |
| FR-VERSION-4 | `model.py`, `store.py`, query | VH-VERSION-2 | ✅ (query echo confirmed Inc 8) |
| FR-ENGINE-1 | `engine/interface.py` | VH-ENGINE-1/2 | ✅ |
| FR-ENGINE-2 | `engine/registry.py`, `adapters/conftest.py`, `docs/ADDING_AN_ENGINE.md` | VH-ENGINE-2 (+ second-adapter) | ✅ |
| FR-ENGINE-3 | `engine/interface.py` | VH-ENGINE-1/2 | ✅ |
| FR-ENGINE-4 | `engine/adapters/opa.py`, `integrity.py` | VH-ENGINE-1/3, VH-INTEGRITY-1 | ✅ |
| FR-ENGINE-5 | `engine/registry.py` + config | VH-ENGINE-2 | ✅ |
| FR-VALIDATE-1 | `validate.py`, `query.py` | VH-MCP-1 | ✅ |
| FR-VALIDATE-2 | `validate.py` (report schema) | VH-MCP-1, VH-INTEGRITY-1 | ✅ |
| FR-VALIDATE-3 | `validate.py` (advisory=informational) | VH-ENGINE-3, VH-MCP-1 | ✅ |
| FR-VALIDATE-4 | `validate.py` (deep-copy, no mutation) | VH-MCP-1 | ✅ |
| FR-QUERY-1 | `query.py`, `scope.py` | VH-MCP-2 | ✅ |
| FR-QUERY-2 | `scope.py` (incl relationship) | VH-MCP-1/2 | ✅ |
| FR-QUERY-3 | `query.py` (constraint_view) | VH-MCP-1 | ✅ |
| FR-MCP-1 | `mcp_server.py` | VH-MCP-1 | ✅ |
| FR-MCP-2 | `mcp_server.py` (get_constraints, validate; + describe_scope discovery aid) | VH-MCP-1 | ✅ |
| FR-MCP-3 | `mcp_server.py` + `docs/MCP_CONTRACT.md` | VH-MCP-1 | ✅ |
| FR-MCP-4 | `service.py`, `mcp_server.py` (fail-open) | VH-MCP-3 | ✅ |
| FR-INTEGRITY-1 | `integrity.py` | VH-INTEGRITY-1 | ✅ |
| FR-INTEGRITY-2 | `integrity.py` | VH-INTEGRITY-2 | ✅ |
| FR-INTEGRITY-3 | `integrity.py` | VH-INTEGRITY-1/2 | ✅ |

## Non-Functional Requirements

| Req | Component(s) | Proven by | Status |
|---|---|---|---|
| NFR-1 Determinism | `importer.py`, `validate.py`, `engine/adapters/opa.py` | VH-IMPORT-1, VH-ENGINE-2 | ✅ |
| NFR-2 Isolation of failure | `loader.py`, `registry.py`, `service.py` | VH-IMPORT-2, VH-MCP-3 | ✅ |
| NFR-3 Scoping efficiency | `query.py`, `scope.py` | VH-MCP-2 | ✅ |
| NFR-4 Observability | `harness/`, `importer.py`, `integrity.py` | VH-OUTPUT-1 | ✅ |
| NFR-5 Extensibility boundary | `engine/registry.py` | VH-ENGINE-2 | ✅ |

## Validation Harness (Section 7)

| Req | Component(s) | Status |
|---|---|---|
| VH-SCHEMA-1 | `harness/checks/schema.py` | ✅ |
| VH-IMPORT-1 | `harness/checks/import_.py` | ✅ |
| VH-IMPORT-2 | `harness/checks/import_.py` | ✅ |
| VH-NAMESPACE-1 | `harness/checks/namespace.py` | ✅ |
| VH-NAMESPACE-2 | `harness/checks/namespace.py` | ✅ |
| VH-VERSION-1 | `harness/checks/version.py` | ✅ |
| VH-VERSION-2 | `harness/checks/version.py` | ✅ |
| VH-ENGINE-1 | `harness/checks/engine.py` | ✅ |
| VH-ENGINE-2 | `harness/checks/engine.py` (conformance suite) | ✅ |
| VH-ENGINE-3 | `harness/checks/engine.py` | ✅ |
| VH-INTEGRITY-1 | `harness/checks/integrity.py` | ✅ |
| VH-INTEGRITY-2 | `harness/checks/integrity.py` | ✅ |
| VH-MCP-1 | `harness/checks/mcp.py` | ✅ |
| VH-MCP-2 | `harness/checks/mcp.py` | ✅ |
| VH-MCP-3 | `harness/checks/mcp.py` | ✅ |
| VH-OUTPUT-1 | `harness/run.py` | ✅ |
| VH-OUTPUT-2 | `sources/` + `harness/run.py` | ✅ |

## Non-Goals (Section 4) — must remain unimplemented

N1 policy generation · N2 static rules-file artifacts · N3 runtime backstops ·
N4 web UI · N5 auth/multi-tenant · N6 auto-remediation. None implemented.
