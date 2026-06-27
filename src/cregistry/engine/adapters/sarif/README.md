# SARIF Normalization Layer

Single ingestion point for all SARIF v2.1.0 output in the Constraint Registry.
Checkov, Semgrep, and any future SARIF-emitting engine call these two functions
instead of re-implementing SARIF parsing.

## Public API (signatures frozen — CONTRACTS §5)

```python
from cregistry.engine.adapters.sarif import parse_sarif, compute_result, get_sarif_level
```

### `parse_sarif(sarif_json: dict, engine: str) -> list[Violation]`

Flattens every `result` in every `run` of a SARIF document into
`Violation` objects.  Field mapping:

| SARIF field | Violation field | Notes |
|---|---|---|
| `result.message.text` | `message` | Falls back to `ruleId` then `""` |
| `result.ruleId` | `rule` | |
| First `physicalLocation.artifactLocation.uri` | `path` | `None` when absent |
| `rule.helpUri` / `rule.help.uri` | `remediation` | Optional; `None` when absent |
| Whole result dict (with resolved level) | `raw` | Opaque; never parsed by core |

`Violation.raw` is a shallow copy of the original SARIF result dict with the
**resolved** `"level"` key embedded, so `get_sarif_level(v.raw)` is always a
simple dict lookup.

Never raises.  Returns `[]` on malformed / non-dict input.

### `compute_result(violations, policy, engine, *, min_level="warning") -> EngineVerdict`

Filters `violations` to those whose SARIF level satisfies `>= min_level`, then:

- Non-empty filtered set → `EngineVerdict.failed_(engine, policy, kept)`
- Empty filtered set → `EngineVerdict.passed_(engine, policy)`

Violations are sorted deterministically by `(rule or "", path or "", message)`
before being stored in the verdict (NFR-1).

Never raises.  Returns `EngineVerdict.errored(...)` on unexpected internal error.

### `get_sarif_level(raw: Any) -> str`

Returns the SARIF level string stored in a `Violation.raw` dict.
Falls back to `"warning"` when the key is absent or the value is not a
recognised level.

## Level ordering

```
note (0) < warning (1) < error (2)
```

`"none"` results are **dropped during parse** and never become `Violation`
objects.  When the `level` field is absent from a result, the adapter first
checks the rule's `defaultConfiguration.level` in `run.tool.driver.rules`; if
that is also absent, it defaults to `"warning"` per SARIF §3.27.10.

Unknown level strings (e.g. `"critical"`) are normalised to `"warning"`.

## errored vs fail distinction

| Situation | Verdict |
|---|---|
| At least one violation survives `min_level` filter | `fail` |
| All violations filtered out (or no results) | `pass` |
| Engine binary missing / policy not found / output unparseable | `error` |
| `compute_result` hits an unexpected internal exception | `error` |

`error` is NOT a policy decision.  Callers use it for fail-open behaviour
(FR-MCP-4) and integrity reporting.  A crash must never be surfaced as a
constraint `fail`.

## Engine adapter usage pattern (CONTRACTS §5)

```python
# Inside CheckovAdapter.evaluate / SemgrepAdapter.evaluate:
from cregistry.engine.adapters.sarif import compute_result, parse_sarif

try:
    sarif = json.loads(proc.stdout)
except json.JSONDecodeError as exc:
    return EngineVerdict.errored(self.name, policy, f"unparseable SARIF: {exc}")

violations = parse_sarif(sarif, self.name)
return compute_result(violations, policy, self.name, min_level=self.min_level)
```

## Smoke check

```
uv run python src/cregistry/engine/adapters/sarif/_smoke.py
```

Exercises the three bundled fixtures and a hand-written inline blob.
Formal tests live in `tests/adapters/` (eval agent's responsibility).

## Fixtures (`_fixtures/`)

| File | Engine style | Key properties tested |
|---|---|---|
| `checkov_sample.json` | Checkov 3.x | Explicit `error`; `warning` via `defaultConfiguration` fallback; `note` via fallback |
| `semgrep_sample.json` | Semgrep OSS 1.x | `error` and `warning` kept; `none` dropped |
| `malformed_sample.json` | (synthetic) | null results, string results, bad locations, unknown level, missing driver rules |
