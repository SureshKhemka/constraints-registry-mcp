# Adapter Contracts (Phase 0 — frozen seam for parallel work)

This file is the single seam every adapter codes against. It has been reconciled against the
**existing OPA/Conftest adapters** (`src/cregistry/engine/`) so the new adapters conform to the
interface already in the codebase rather than inventing a new one. Once approved, do not change it
mid-build without re-syncing all five agents.

Spec references: `constraint-registry-v0-spec.md` — FR-ENGINE-1/2/3/4/5, FR-VALIDATE-2,
FR-INTEGRITY-1/2/3, §7 (VH-ENGINE-1/2/3, VH-INTEGRITY-1/2).

> **What changed from the draft skeleton (read this first).** The original draft assumed
> things that do not exist in this repo. Corrected here:
> 1. **Paths.** Adapters are single modules under `src/cregistry/engine/adapters/`, not a
>    top-level `adapters/<x>/` tree. New adapters become **subpackages** under that real dir.
> 2. **Binding shape.** `EnforcementBinding` carries **only** `{engine, policy}` — there is no
>    `rules`, `min_level`, or `config`. Rule/ruleset selection lives in the `policy` locator
>    (mirroring OPA, where `policy` is the `.rego` file).
> 3. **No `import_catalog` on the interface.** The `EngineAdapter` ABC has exactly
>    `name`, `can_handle`, `evaluate`. The catalog importer is a **separate module function**,
>    not an interface method.
> 4. **Normalized schema already exists** in `engine/interface.py` as `Violation` /
>    `EngineVerdict` / `Verdict`. It is **not** SARIF-shaped. There is no `AdapterResult`,
>    `Location`, `level`, `help_uri`, or `errored` field. The SARIF seam maps SARIF **onto these
>    existing types**.

---

## 1. Directory ownership (parallel-safety boundary)

Each agent writes ONLY inside its directory. Disjoint paths = safe concurrent writes. All paths
are **subpackages of the real adapter package** `src/cregistry/engine/adapters/`.

| Owner agent              | Writes only in                                        |
|--------------------------|-------------------------------------------------------|
| `sarif-normalizer`       | `src/cregistry/engine/adapters/sarif/`                |
| `checkov-adapter`        | `src/cregistry/engine/adapters/checkov/`              |
| `semgrep-adapter`        | `src/cregistry/engine/adapters/semgrep/`              |
| `adapter-eval-generator` | `tests/adapters/`, `tests/fixtures/`                  |
| `adapter-reviewer`       | (read-only — writes nothing)                          |

Existing reference adapters (**do not touch**):
`src/cregistry/engine/adapters/opa.py`, `src/cregistry/engine/adapters/conftest.py`.
Shared interface (**do not touch**): `src/cregistry/engine/interface.py`,
`src/cregistry/engine/conformance.py`, `src/cregistry/engine/registry.py`.

> Each agent's directory is a Python package: include an `__init__.py`. Config registers an
> adapter by dotted path `module:ClassName` (see §7), e.g.
> `cregistry.engine.adapters.checkov:CheckovAdapter`.

---

## 2. Engine-adapter interface (new adapters MUST implement this — verbatim from the repo)

Source of truth: `src/cregistry/engine/interface.py`. The ABC is exactly three members.

```python
class EngineAdapter(ABC):
    name: str                                              # matches binding.engine + config name

    def can_handle(self, binding: EnforcementBinding) -> bool:   # FR-ENGINE-3a
        ...

    def evaluate(self, artifact: Any, policy: str) -> EngineVerdict:  # FR-ENGINE-3b
        ...
```

Conventions every adapter follows (matching `opa.py` / `conftest.py`):

- **Constructor:** `def __init__(self, options: dict | None = None)`. Read tool config from
  `options` (e.g. `bin`, `timeout`, and any engine-specific keys). The registry passes
  `options=...` from config when present (see §7). **This is the only place to put a severity
  threshold or ruleset default** — the binding cannot carry one (see §3).
- **`can_handle`:** `return binding.engine == self.name`.
- **`available` (recommended property):** `shutil.which(self.bin) is not None`. The harness reads
  `getattr(adapter, "available", True)` to SKIP (not fail) when a binary is missing.
- **`evaluate` must NEVER raise** for an unrunnable engine/policy. Return
  `EngineVerdict.errored(name, policy, msg)` instead (NFR-2). A policy violation is a `fail`
  verdict, never an exception.

### The `artifact` argument (⚠ FLAGGED AMBIGUITY — agents must honor this convention)

`evaluate(artifact, policy)` receives `artifact` as **whatever the caller holds**. For the
existing engines and both callers (`validate._evaluate_bound` and `integrity.check_integrity`),
`artifact` is a **parsed JSON object** (`json.loads(fixture_file)` → `dict`/`list`). The artifact
is deep-copied before it reaches the adapter (FR-VALIDATE-4); adapters MUST treat it as read-only.

File-scanning engines (Checkov, Semgrep) cannot scan a Python `dict` directly, so each adapter
**materializes `artifact` to a temp file/tree, runs the engine on it, then deletes it** — the same
pattern `opa.py`/`conftest.py` use to write the dict to a `NamedTemporaryFile`. Conventions:

- **Checkov** (`checkov-adapter`): `artifact` is a parsed IaC document (e.g. a CloudFormation or
  Terraform-plan JSON object). Write it to a temp `.json` and run Checkov against that file.
- **Semgrep** (`semgrep-adapter`): source-code fixtures cannot be a bare JSON value. Use an
  envelope `{"path": "<relative/name.ext>", "content": "<source text>"}` (or a list of them);
  the adapter writes each `content` to `path` under a temp dir and scans the tree. If `artifact`
  is already a path-like string, scan it directly.

Both adapters MUST accept a plain path string for `artifact` too (scan it as-is). **Flag in your
return summary** if your engine needs an artifact shape these rules don't cover — do not silently
invent a third convention.

---

## 3. EnforcementBinding (what the adapter is told to enforce)

Source of truth: `src/cregistry/model.py`. The binding is **only**:

```python
class EnforcementBinding(_Strict):
    engine: str        # selects the adapter (== adapter.name)
    policy: str        # OPAQUE locator, resolved by the registry to an absolute path
```

There is **no** `rules`, `min_level`, or `config` field, and the schema is `extra="forbid"` —
do not add fields. Consequences for the new adapters:

- **Rule / ruleset selection comes from the `policy` locator**, exactly as OPA's `policy` is its
  `.rego` file. For Checkov, `policy` resolves to a Checkov config/`--check` file that selects the
  checks. For Semgrep, `policy` resolves to the ruleset (`.yml`/`.yaml`) passed via
  `--config <policy>`. The registry resolves the locator relative to the owning source dir via
  `config.resolved_policy_path(source, binding.policy)` and passes the **absolute path** as the
  `policy` arg to `evaluate`.
- **Severity threshold** (drop notes, etc.) is an **adapter option** (`options["min_level"]`,
  default `"warning"`), not a per-binding value. Apply it in the SARIF seam (§5).

---

## 4. Normalized result schema (already defined — do NOT redefine)

Source of truth: `src/cregistry/engine/interface.py`. Adapters return **these exact types**.

```python
class Verdict(str, Enum):
    passed = "pass"
    failed = "fail"
    error  = "error"          # engine could not run (binary missing / unparseable / timeout)

@dataclass(frozen=True)
class Violation:
    message: str                       # REQUIRED (SARIF result.message.text)
    rule: str | None = None            # engine rule id  (SARIF result.ruleId)
    resource: str | None = None        # logical resource, if the engine reports one
    path: str | None = None            # file path        (SARIF physicalLocation.artifactLocation.uri)
    raw: Any | None = None             # original engine record (opaque; never parsed by core)
    remediation: str | None = None     # adapter may set; else orchestrator fills from guidance

@dataclass(frozen=True)
class EngineVerdict:
    verdict: Verdict
    engine: str
    policy: str
    error: str | None = None
    violations: list[Violation] = field(default_factory=list)
    # constructors: EngineVerdict.passed_(engine, policy)
    #               EngineVerdict.failed_(engine, policy, violations)
    #               EngineVerdict.errored(engine, policy, message)
```

Notes for SARIF-emitting engines (since `Violation` has no `level`/`help_uri`/location object):

- SARIF `level` is **not** a first-class field. Keep it inside `raw` (store the whole SARIF
  `result`); the seam uses it only for `min_level` filtering (§5).
- SARIF location → flatten: `path` = `artifactLocation.uri`; put line/region + `helpUri` inside
  `raw` (or fold a `helpUri` into `remediation` if you like — optional).
- `resource` is optional; set it only if the engine exposes a logical resource id.

### Verdict + severity rules (do not let adapters improvise)

- `evaluate` runs the engine, parses SARIF, keeps results whose `level ≥ min_level`.
- `verdict = fail` iff that filtered set is non-empty; else `pass`.
- `verdict = error` (NOT a fail) iff the tool crashed / binary missing / output unparseable /
  timed out. Callers use `error` for fail-open (FR-MCP-4) and integrity reporting; a crash is
  never a policy decision.
- Adapters do **not** read or derive the constraint's `severity` (hard/soft/advisory). That is
  layer 2 (`validate.py`) and is added by the orchestrator. Keep the boundary
  (interface.py docstring: "an adapter carries no constraint-level concepts").

SARIF `level` → keep: `error`, `warning`, `note`; **drop** `none`.
`min_level` ordering: `note < warning < error` (default threshold `warning`).

---

## 5. The SARIF seam (owned by `sarif-normalizer`; frozen signatures)

Checkov and Semgrep both emit SARIF, so SARIF parsing lives **once** in
`src/cregistry/engine/adapters/sarif/`. Engine adapters import these and MUST NOT re-implement
SARIF parsing. The functions produce the **existing** `Violation` / `EngineVerdict` types from §4.

```python
# src/cregistry/engine/adapters/sarif/__init__.py
# sarif-normalizer replaces the bodies; these signatures are FROZEN.
from cregistry.engine.interface import Violation, EngineVerdict, Verdict

def parse_sarif(sarif_json: dict, engine: str) -> list[Violation]:
    """Flatten every SARIF run.result (level != 'none') into Violation objects.
    Stores the original SARIF result in Violation.raw (including its level)."""
    ...

def compute_result(
    violations: list[Violation],
    policy: str,
    engine: str,
    *,
    min_level: str = "warning",
) -> EngineVerdict:
    """Filter `violations` to those whose raw SARIF level >= min_level, then:
    non-empty -> EngineVerdict.failed_(engine, policy, kept); else passed_(engine, policy)."""
    ...
```

Engine adapter flow (Checkov/Semgrep `evaluate`):

1. Guard: policy path exists, binary available → else `EngineVerdict.errored(...)`.
2. Materialize `artifact` (§2), run engine with `--output/-f sarif` (or `--sarif`), capture stdout.
3. `try: sarif = json.loads(out)` → on failure `EngineVerdict.errored(...)`.
4. `violations = parse_sarif(sarif, self.name)`.
5. `return compute_result(violations, policy, self.name, min_level=self.min_level)`.

`min_level` comes from the adapter's `options` (§3). Sort violations deterministically
(`key=lambda v: (v.rule or "", v.path or "", v.message)`) for NFR-1, as the existing adapters do.

> Until `sarif-normalizer` lands, engine adapters code against these two signatures (import them).
> They do not block on the implementation. If the normalizer needs to add a keyword-only arg, it
> may — it must not change the two names, their positional params, or the return types.

---

## 6. Catalog importer (the "import path" — a module function, NOT an interface method)

The engine "import path" each engine agent owns turns that engine's **rule catalog** into
registry **constraint stubs**. This is a standalone function in the adapter's package (e.g.
`cregistry.engine.adapters.checkov.importer:import_catalog`), invoked by tooling — it is **not**
part of the `EngineAdapter` ABC and is unrelated to `cregistry.importer` (which imports authored
constraints from sources).

A stub is a partial `Constraint` (see `model.py`, FR-CONSTRAINT-1). Fill what is deterministic;
leave human-authored fields as TODO stubs. Required Constraint fields and their import values:

```yaml
id:        "<engine>/<rule-id-slug>"          # e.g. "checkov/ckv-aws-20"
title:     "<engine rule title>"
intent:    "TODO: human"                       # min_length 1 — placeholder, human enriches
category:  infrastructure | organizational | architectural   # checkov→infrastructure, semgrep→architectural (best-effort)
scope:     { providers: [...], resource_types: [...] }   # best-effort from rule metadata; omit unknowns
severity:  soft                                # conservative; NEVER "hard" on import
enforcement:
  - engine: "<engine>"
    policy: "<locator the human will point at the ruleset/check>"   # only {engine, policy} exist
guidance:
  dont: ["<rule short description>"]
  example_compliant: "TODO: human"             # min_length 1 — placeholder
owner:     "imported"
version:   "0.1.0"
```

- `EnforcementBinding` is `{engine, policy}` only — **do not** emit `rules`/`min_level` keys
  (schema is `extra="forbid"`; they'd fail validation).
- The `Constraint` schema has **no `provenance` field** (it's `extra="forbid"`). Carry license /
  source / imported-at as a **sidecar** the importer returns alongside each stub (e.g. a
  `(stub_dict, provenance_dict)` pair or a separate manifest), NOT inside the constraint YAML.
  **Semgrep MUST capture `license`** for each imported rule in that sidecar; default `null`
  elsewhere. Flag this in your summary so we decide where provenance is persisted.
- Output stubs as dicts/YAML that round-trip through `Constraint.model_validate(...)` (minus the
  human TODO fields, which a human fills before the stub validates with non-placeholder content).

---

## 7. Integration seams (how the orchestrator wires the adapters — read-only context)

You don't edit these, but your adapter must slot into them cleanly:

- **Config registration** (`registry.config.yaml`, loaded by `engine/registry.py`): a new engine
  is one line under `engines:`:
  ```yaml
  - name: checkov
    adapter: "cregistry.engine.adapters.checkov:CheckovAdapter"
    options: { min_level: warning }   # optional; passed to __init__(options=...)
  ```
  `_load_adapter` does `cls(options=options) if options else cls()` and asserts
  `isinstance(instance, EngineAdapter)`. So the class MUST subclass `EngineAdapter` and accept
  `options`.
- **Selection:** `EngineRegistry.for_binding(binding)` returns the first adapter whose
  `can_handle` is true. `required_engines(constraints)` collects `binding.engine` values.
- **Conformance suite** (`engine/conformance.py`, VH-ENGINE-2): data-driven via
  `ConformanceCase(name, policy, artifact, expected: Verdict)`. `run_conformance(adapter, cases)`
  checks `can_handle` (own vs foreign), missing-policy→`error`, and per-case
  verdict + determinism (`v1.to_dict() == v2.to_dict()`) + violation count. **Your adapter must
  pass this suite unchanged** — supply cases, add no harness code (FR-ENGINE-2). Determinism means
  two evaluations of the same `(artifact, policy)` produce identical `to_dict()` (hence the stable
  violation sort, and don't leak temp paths into `message`/`rule`).
- **Validation orchestrator** (`validate.py`, layer 2): calls `adapter.evaluate(artifact, policy)`
  per binding, combines bindings (`error` > `fail` > `pass`), and fills `remediation` from
  constraint guidance when the adapter left it `None`.
- **Integrity / fixture cross-check** (`integrity.py`, VH-INTEGRITY-1/2): for each constraint with
  fixtures, it does `artifact = json.loads(fixture_file.read_text())` then
  `adapter.evaluate(artifact, policy_path)` and asserts `pass` fixture → `Verdict.passed`, `fail`
  fixture → `Verdict.failed`; `Verdict.error` is reported as an integrity error. **This is why §2's
  artifact-materialization convention matters**: fixtures are JSON files; your adapter receives the
  parsed object and must scan it.

---

## 8. Open items to flag back (don't silently resolve)

When you return, explicitly call out any of these you hit:

1. **Artifact shape** for your engine if §2's convention doesn't fit (esp. Semgrep source
   fixtures — confirm the `{path, content}` envelope works end-to-end through `integrity.py`,
   which JSON-parses the fixture before calling you).
2. **Where provenance/license is persisted** for imported stubs (§6) — the `Constraint` schema
   has no field for it.
3. **`min_level` default** if `warning` is wrong for your engine's typical SARIF levels.
4. Any place you felt forced to re-implement SARIF parsing instead of using §5.
