# Semgrep Engine Adapter

Integrates [Semgrep](https://semgrep.dev/) into the Constraint Registry as a
source-code enforcement engine.  Semgrep checks application **source code**
(50+ languages) with YAML rules — this is what lets the registry constrain the
code coding-agents actually write, not just infrastructure artefacts.

---

## Registration

Add one line to `registry.config.yaml`:

```yaml
engines:
  - name: semgrep
    adapter: "cregistry.engine.adapters.semgrep:SemgrepAdapter"
    options:
      bin: semgrep          # default; override if semgrep is not on PATH
      timeout: 60           # seconds; increase for large repos
      min_level: warning    # note | warning | error
```

---

## Version assumptions

Tested against **Semgrep ≥ 1.0**.  The adapter uses the legacy top-level command
form:

```
semgrep --config <ruleset.yaml> --sarif --metrics=off <target>
```

On Semgrep 1.x this is an alias for `semgrep scan ...`; both forms are
supported.  If your installation only exposes `semgrep scan`, set:

```yaml
options:
  bin: "semgrep scan"   # NOT supported — use a wrapper script instead
```

The correct workaround for a scan-only binary is a thin shell wrapper:

```sh
#!/bin/sh
exec semgrep scan "$@"
```

SARIF is written to **stdout**; stderr carries progress and warnings.

---

## Exact CLI flags emitted by the adapter

```
semgrep --config <abs-path-to-policy.yaml> --sarif --metrics=off <target>
```

| Flag | Purpose |
|------|---------|
| `--config <policy>` | Ruleset file (or `p/ruleset-name` registry slug) |
| `--sarif` | Output SARIF 2.1.0 JSON to stdout |
| `--metrics=off` | Disable telemetry; keeps invocations air-gapped |
| `<target>` | `.` when scanning a materialised temp tree; absolute path for path-string artefacts |

The subprocess is invoked with `cwd=<temp_dir>` when the artefact was
materialised, so Semgrep emits **relative URIs** in SARIF (e.g. `safe.py` not
`/tmp/cregistry_semgrep_abc123/safe.py`).  A belt-and-suspenders path normaliser
strips any remaining absolute prefix before handing SARIF to the shared
`parse_sarif` / `compute_result` seam.

---

## Artefact convention (CONTRACTS §2)

The adapter accepts three artefact shapes:

| Shape | Behaviour |
|-------|-----------|
| `"path/to/dir"` (string) | Scan that path directly; no temp tree |
| `{"path": "rel/file.py", "content": "..."}` | Write to temp tree, scan `"."` |
| `[{"path": ..., "content": ...}, ...]` | Write all to same temp tree, scan `"."` |

Fixture JSON files (for `integrity.py`) must use the dict or list-of-dicts form
so the artefact is a valid JSON value after `json.loads`.

---

## Determinism guarantee (NFR-1, CONTRACTS §7)

Two calls to `evaluate(artifact, policy)` with the same inputs produce identical
`to_dict()` output because:

1. The subprocess runs with `cwd=<temp_dir>` and target `"."`, so Semgrep writes
   relative URIs in SARIF locations.
2. `_strip_temp_prefix` is applied to the SARIF before `parse_sarif` — any
   remaining absolute temp-dir prefixes are relativised.
3. `compute_result` sorts violations stably by `(rule, path, message)`.
4. Temp-dir names never appear in `Violation.message` or `Violation.rule`.

---

## Catalog importer and license policy

```python
from cregistry.engine.adapters.semgrep.importer import import_catalog

stubs = import_catalog("/path/to/ruleset.yaml")
# stubs is list[CatalogStub(constraint=dict, provenance=dict)]
```

Each `CatalogStub` is a named tuple:

- `constraint` — dict that round-trips through `Constraint.model_validate`.
- `provenance` — sidecar with `license`, `source`, `rule_id`, `imported_at`, etc.
  The `Constraint` schema has `extra="forbid"` so provenance is never inside the
  constraint dict.

### License policy (CONTRACTS §6)

Semgrep's published registry rules (`p/*`) are licensed under a **restrictive
Semgrep-proprietary license**.  The importer enforces the following policy:

- Each rule's license is read from `rule.metadata.license` (or a top-level
  `metadata.license` / `license` field on the YAML document).
- If no license is found, `provenance["license"]` is `null` (unknown).
- Rules with **unknown license are skipped by default**.
- Pass `allow_unknown_license=True` to include them (for auditing/review only).
- **Never** call `import_catalog` on a Semgrep registry URL or bundle without
  confirming your entitlement to the ruleset's license.

```python
# Include all rules, even those without a license field (audit mode):
stubs = import_catalog("/path/to/ruleset.yaml", allow_unknown_license=True)
```

### Writing custom rules (permissive license)

The safest path is to maintain your own Semgrep YAML rules with an explicit open
license (`MIT`, `Apache-2.0`, etc.) in the metadata:

```yaml
rules:
  - id: no-eval
    patterns:
      - pattern: eval(...)
    message: "eval() must not appear in production code"
    languages: [python]
    severity: WARNING
    metadata:
      license: MIT
```

---

## Test fixtures (`_fixtures/`)

| File | Purpose |
|------|---------|
| `rule.yaml` | Minimal rule: detects `eval(...)` in Python |
| `pass.json` | Artefact envelope that passes the rule (no `eval` call) |
| `fail.json` | Artefact envelope that fails the rule (`eval(user_input)`) |

Run a conformance smoke-test (requires `semgrep` on PATH):

```python
from pathlib import Path
from cregistry.engine.adapters.semgrep import SemgrepAdapter
from cregistry.engine.conformance import ConformanceCase, run_conformance
from cregistry.engine.interface import Verdict
import json

fixtures = Path(__file__).parent / "_fixtures"
rule = str(fixtures / "rule.yaml")
adapter = SemgrepAdapter()
cases = [
    ConformanceCase("pass", rule, json.loads((fixtures / "pass.json").read_text()), Verdict.passed),
    ConformanceCase("fail", rule, json.loads((fixtures / "fail.json").read_text()), Verdict.failed),
]
results = run_conformance(adapter, cases)
for r in results:
    print(r)
```

---

## Binary availability and graceful degradation

When `semgrep` is not installed the adapter's `available` property returns
`False`.  The conformance harness reads `getattr(adapter, "available", True)` and
**skips** (rather than fails) engine checks for unavailable adapters.  Any call
to `evaluate` also returns `EngineVerdict.errored` immediately rather than
raising, so the overall harness continues.
