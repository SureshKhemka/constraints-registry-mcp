# Constraint Registry

A single, queryable source of engineering **constraints** (infrastructure,
organizational, architectural) that coding agents (Claude Code, Cursor, Codex, …)
consult at code-generation time, exposed over an **MCP server**. It does **not**
enforce constraints itself — it provides guidance to agents and delegates
deterministic validation to existing enforcement engines (OPA / Conftest).

Constraints are authored in source repos, aggregated into an immutable, versioned
**bundle**, and served over MCP so an agent can:

1. `describe_scope` — discover the valid selector vocabulary,
2. `get_constraints` — fetch the rules relevant to what it's building, and
3. `validate` — check a candidate artifact against the bound enforcement engines.

> Authoritative requirements: `constraint-registry-v0-spec.md`.
> Requirement → component → test mapping: `TRACEABILITY.md`.

---

## Contents
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Running the MCP server](#running-the-mcp-server)
- [Integrating with coding agents](#integrating-with-coding-agents)
- [Authoring constraints](#authoring-constraints)
- [Hot reload](#hot-reload-no-restart-on-constraint-changes)
- [Validation harness](#validation-harness)
- [Adding an enforcement engine](#adding-an-enforcement-engine)
- [Repository layout](#repository-layout)

---

## Features

- **Three constraint categories** — infrastructure, organizational, architectural,
  including **relationship-style** selectors (e.g. "no synchronous calls across
  domain boundaries") and **advisory** (no-enforcement) constraints.
- **Multi-source aggregation** — import from many source repos; ids are namespaced
  per source; deterministic, content-hashed, immutable **versioned bundles**.
- **Precedence & anti-drift** — a configurable default policy (hard outranks
  weaker; a downstream source may not relax a higher-precedence rule on the same
  scope); fixture cross-checks keep guidance and enforcement from drifting.
- **Pluggable engines** — a stable adapter interface; ships a real **OPA** adapter
  and a real **Conftest** adapter. Adding an engine = one adapter + one config
  line (see [Adding an enforcement engine](#adding-an-enforcement-engine)).
- **MCP server** — three tools (`describe_scope`, `get_constraints`, `validate`)
  over **stdio** or a shared **HTTP** endpoint. `get_constraints` **fails open**
  so an agent is never blocked.
- **Hot reload** — the server can periodically re-import so constraint changes are
  picked up **without a restart**.
- **Validation harness** — proves the registry and constraint set are internally
  consistent; machine-readable JSON, non-zero exit on failure.

---

## Prerequisites

| Tool | Required? | Notes |
|---|---|---|
| **Python ≥ 3.11** | yes | the package targets 3.11+ |
| **[uv](https://docs.astral.sh/uv/)** | yes | manages the venv and runs entry points |
| **[OPA](https://www.openpolicyagent.org/docs/latest/#running-opa)** (`opa`) | for `validate` / fixture cross-checks | the reference enforcement engine |
| **[Conftest](https://www.conftest.dev/install/)** (`conftest`) | optional | second engine; its harness check SKIPs if absent |

Install the engines on macOS:
```bash
brew install opa conftest
```
The registry and the `get_constraints`/`describe_scope` guidance work without any
engine; an engine is only needed to run `validate` and the fixture cross-checks.

---

## Quick start

```bash
git clone https://github.com/SureshKhemka/constraints-registry-mcp.git
cd constraints-registry-mcp

uv sync                      # create the venv + install deps

uv run cregistry-harness     # run the validation harness against the bundled samples
```

The harness emits machine-readable JSON and **exits non-zero on any failure**. A
green run looks like:

```json
{ "passed": true, "summary": { "pass": 21, "fail": 0, "skip": 0, "total": 21 }, "checks": [ ... ] }
```

(`skip` is used only when an optional engine like `conftest` is not installed.)

---

## Running the MCP server

Two transports — pick based on how you want tools to connect.

```bash
# stdio (default): each tool launches its own copy; nothing to manage
uv run cregistry-mcp

# one shared HTTP server every tool connects to (recommended for multiple tools)
uv run cregistry-mcp --http --port 8765 --reload-interval 60
```

Flags: `--transport {stdio,http,sse}`, `--http` (shorthand), `--host`
(default `127.0.0.1`), `--port` (default `8765`), `--config`
(or `$CREGISTRY_CONFIG`), `--reload-interval SECONDS` (`0` = off).

Manage the shared HTTP server:
```bash
lsof -ti tcp:8765 | xargs kill     # stop
# restart = stop + start
```

Full operational guide (stop/restart, macOS launchd auto-start, the
repo-sync/decoupling pattern): **`docs/RUNNING.md`**.
Tool input/output contracts: **`docs/MCP_CONTRACT.md`**.

---

## Integrating with coding agents

The server exposes three tools: `describe_scope`, `get_constraints`, `validate`.

### Claude Code

```bash
# shared HTTP server (start it first, see above), available in every project:
claude mcp add --scope user --transport http constraint-registry http://127.0.0.1:8765/mcp

# OR stdio (no separate server to run; Claude launches it):
claude mcp add constraint-registry -- uv run --directory "$(pwd)" cregistry-mcp

claude mcp list   # should show: constraint-registry ... ✔ Connected
```

### Cursor (`~/.cursor/mcp.json`)

```json
{ "mcpServers": { "constraint-registry": { "url": "http://127.0.0.1:8765/mcp" } } }
```

### Codex / other stdio-only tools

Configure an MCP server with `command: uv`, `args: ["run","--directory","/abs/path/to/repo","cregistry-mcp"]`.

### Make the agent actually consult it

Agents auto-discover the tools, but to get them to consult the registry *before*
generating code, add an instruction to your project (or `~/.claude/CLAUDE.md`):

> Before writing AWS/infra code, call the constraint-registry MCP: `describe_scope`
> to learn valid selector values, then `get_constraints` with the right scope, and
> comply with every `hard` constraint as a non-negotiable downstream gate.
> Optionally `validate` the result.

---

## Authoring constraints

A **source** is a directory with `constraints/*.yaml` (one constraint per file)
and, optionally, `policies/` (engine policies) and `fixtures/` (sample artifacts).
Register sources and engines in `registry.config.yaml`:

```yaml
sources:
  - { name: platform-security, path: sources/platform-security, precedence: 100 }
  - { name: data-platform,     path: sources/data-platform,     precedence: 50  }
engines:
  - { name: opa,      adapter: "cregistry.engine.adapters.opa:OpaAdapter" }
  - { name: conftest, adapter: "cregistry.engine.adapters.conftest:ConftestAdapter" }
precedence_policy: default
```

A constraint (see `sources/platform-security/constraints/aws-s3-no-public-access.yaml`):

```yaml
id: aws.s3.no-public-access
title: "S3 buckets must not be publicly accessible"
intent: "Public buckets are the top source of data-exposure incidents."
category: infrastructure          # infrastructure | organizational | architectural
scope:
  providers: [aws]
  resource_types: [aws_s3_bucket] # Terraform resource ids (NOT "s3_bucket")
  environments: [all]
  repos: ["tag:data-plane"]
severity: hard                    # hard | soft | advisory
enforcement:                      # omit for an advisory (guidance-only) constraint
  - { engine: opa, policy: policies/s3_public.rego }
guidance:
  do:   ["Attach an aws_s3_bucket_public_access_block with all four flags true"]
  dont: ["Never set acl = 'public-read' or 'public-read-write'"]
  example_compliant: |
    {"resources": {"aws_s3_bucket": {"data": {"acl": "private", "public_access_block": true}}}}
owner: platform-security
version: 1.0.0
fixtures:                         # optional; cross-checked against the engine
  pass: fixtures/s3_private.json
  fail: fixtures/s3_public.json
```

Scoping notes (matters when agents query):
- `resource_types` use the target tooling's identifiers (Terraform: `aws_s3_bucket`).
  Call `describe_scope` to discover the exact vocabulary present.
- A query that **omits** a dimension matches broadly; a value that **contradicts**
  a constraint's selector excludes it. Relationship-scoped constraints are only
  returned for queries that supply a matching relationship.
- After authoring, run `uv run cregistry-harness` to validate schema, precedence,
  and fixtures.

---

## Hot reload (no restart on constraint changes)

Run the server with `--reload-interval N` and it re-imports from disk every `N`
seconds, publishing a new immutable bundle when content changes:

```bash
uv run cregistry-mcp --http --port 8765 --reload-interval 60
```

- No-op when nothing changed; previous bundle versions stay pinnable by id.
- A failed re-import (e.g. an unresolvable precedence conflict) **keeps the
  last-good bundle serving** and logs the reason — the server never goes dark.
- The server reads from the configured source paths, so wire your teams'
  constraint repos to sync/pull into those paths (a separate ops job, e.g. a cron
  `git pull` or CI publish). Code/dependency changes still need a restart.

---

## Validation harness

`uv run cregistry-harness` runs end-to-end against the bundled, self-contained
sample sources and proves: schema conformance, deterministic import, malformed-
constraint isolation, namespacing & precedence, versioning & deprecation, engine-
interface conformance (incl. a reusable suite any adapter can be run against),
fixture cross-checks / broken-binding detection, the MCP contract / scoping /
fail-open, and hot-reload behavior. It prints structured JSON and returns a
non-zero exit on any failure — suitable for CI.

```bash
uv run cregistry-harness            # human-readable JSON to stdout, exit 0/1
uv run cregistry-harness --config path/to/registry.config.yaml
```

---

## Adding an enforcement engine

Implement the `EngineAdapter` interface in a new module under
`src/cregistry/engine/adapters/`, add one line under `engines:` in
`registry.config.yaml`, and validate it against the existing conformance suite —
no changes to the schema, importer, MCP server, or harness. Full walkthrough:
**`docs/ADDING_AN_ENGINE.md`**.

---

## Repository layout

```
src/cregistry/
  model.py            constraint schema (Pydantic)
  loader.py           load + per-field schema validation
  config.py           registry config (sources, engines)
  importer.py         import → aggregate → bundle
  precedence.py       namespacing precedence / conflict resolution
  scope.py            scope matching (query + conflict)
  bundle.py store.py  immutable versioned bundles + store
  query.py validate.py  scoped queries + artifact validation
  integrity.py        fixture cross-check / anti-drift
  service.py          transport-independent service (+ hot reload)
  mcp_server.py       MCP server (stdio / http) + CLI
  engine/             stable engine interface, registry, adapters (opa, conftest)
  harness/            the validation harness (checks/*)
sources/              bundled sample source repos (constraints, policies, fixtures)
scenarios/            self-contained fixtures for harness edge cases
docs/                 RUNNING.md, MCP_CONTRACT.md, ADDING_AN_ENGINE.md
deploy/               launchd template for auto-starting the HTTP server
```
