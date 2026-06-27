# Checkov Engine Adapter

Engine adapter for [Checkov](https://github.com/bridgecrewio/checkov) — static
analysis for IaC.  Implements `EngineAdapter` (CONTRACTS §2) and provides a
catalog importer (CONTRACTS §6).

---

## Registration

Add to your `registry.config.yaml` under `engines:`:

```yaml
engines:
  - name: checkov
    adapter: "cregistry.engine.adapters.checkov:CheckovAdapter"
    options:
      bin: checkov          # default; override if binary is not on PATH
      timeout: 60           # seconds; default 60
      min_level: warning    # note | warning | error; default warning
```

---

## Checkov version assumptions

Tested against **Checkov >= 2.3** (the version shipping `--output sarif` and
`--config-file`).  Verify with:

```bash
checkov --version
```

---

## CLI flags used by `evaluate()`

```
checkov -f <artifact_file> --output sarif --config-file <policy_path>
```

| Flag | Purpose |
|---|---|
| `-f <file>` | Scan a single artifact file (materialized from the `artifact` arg). |
| `--output sarif` | Request SARIF v2.1.0 on stdout. |
| `--config-file <path>` | Absolute path to the Checkov YAML config (the `policy` locator). |

The `policy` locator **must be a path to a Checkov YAML config file**, for
example:

```yaml
# my-s3-policy.yaml
check:
  - CKV_AWS_20
framework:
  - terraform_plan
```

The config file selects which checks (`check:`) and frameworks (`framework:`) to
run.  Do NOT add `output:` to this file; the adapter always appends
`--output sarif` on the command line, which takes precedence.

---

## Artifact materialization

| `artifact` type | Behavior |
|---|---|
| `dict` / `list` | Written to a `NamedTemporaryFile` with suffix `.json`; temp file is deleted in `finally`. |
| `str` / `os.PathLike` | Scanned directly; no temp file created. |

**Determinism note:** Checkov embeds the scanned file URI in SARIF
`physicalLocation.artifactLocation.uri`.  The adapter replaces every
occurrence of the temp-file path (full path and basename) with the canonical
placeholder `"<artifact>"` in the raw SARIF string before parsing, so two
`evaluate()` calls on the same `artifact` produce identical `to_dict()` output
(NFR-1, VH-ENGINE-2 determinism check).

---

## Errored vs fail distinction

| Condition | Return |
|---|---|
| Policy file not found | `EngineVerdict.errored` |
| Binary not on PATH | `EngineVerdict.errored` |
| Subprocess timeout | `EngineVerdict.errored` |
| `FileNotFoundError` on `subprocess.run` | `EngineVerdict.errored` |
| SARIF stdout not valid JSON | `EngineVerdict.errored` |
| Checkov exits 1 (violations found) + SARIF parses | `EngineVerdict.failed_` |
| Checkov exits 0 + SARIF parses, no results ≥ min_level | `EngineVerdict.passed_` |

Checkov exits **1** when it finds violations — that is normal and is never
treated as an engine error.  Exit code **2** (Checkov internal error) is also
not treated as an immediate error; the adapter still attempts SARIF parsing,
and only if that fails does it return `errored`.

---

## SARIF seam

All SARIF parsing is delegated to
`cregistry.engine.adapters.sarif.parse_sarif` / `compute_result` (CONTRACTS
§5).  The adapter never re-implements SARIF parsing.

`min_level` (from `options`) is passed to `compute_result` to filter
violations below the threshold before deciding pass/fail.

---

## Fixtures

Located in `_fixtures/`:

| File | Purpose |
|---|---|
| `policy.yaml` | Checkov config selecting `CKV_AWS_20` on `terraform_plan` framework. |
| `pass.json` | Terraform plan JSON: `aws_s3_bucket` with `acl = "private"`. Should pass CKV_AWS_20. |
| `fail.json` | Terraform plan JSON: `aws_s3_bucket` with `acl = "public-read"`. Should fail CKV_AWS_20. |

These fixtures are used by the conformance suite (`engine/conformance.py`).
When Checkov is not installed, the harness SKIPs the conformance cases
(it reads `getattr(adapter, "available", True)`).

---

## Catalog importer

```python
from cregistry.engine.adapters.checkov.importer import import_catalog

stubs = import_catalog("builtin")   # or e.g. "checkov/3.2.1"
for stub, provenance in stubs:
    print(stub["id"])               # e.g. "checkov/ckv-aws-20"
    print(provenance["imported_at"])
```

### Check enumeration

1. If `checkov` is on PATH, runs `checkov --list` and parses its output.
2. Falls back to the hard-coded seed set (14 checks) when Checkov is unavailable
   or `--list` output is empty.

### Stub shape

Each stub is a valid `Constraint`-schema dict (minus human TODO fields):

```yaml
id:          "checkov/ckv-aws-20"
title:       "Ensure the S3 bucket ACL is private"
intent:      "TODO: human"
category:    infrastructure        # or architectural for CKV2_* graph checks
scope:
  providers: [aws]
  resource_types: [aws_s3_bucket]
severity:    soft
enforcement:
  - engine: checkov
    policy:  "checkov/CKV_AWS_20"  # placeholder; human replaces with real path
guidance:
  dont:
    - "Ensure the S3 bucket ACL is private"
  example_compliant: "TODO: human"
owner:       imported
version:     "0.1.0"
```

### Provenance sidecar

The `Constraint` schema is `extra="forbid"` — provenance is NOT embedded in
the stub.  It is returned as a separate dict alongside each stub:

```python
{
    "source":      "builtin",               # the source_ref arg
    "imported_at": "2026-01-01T00:00:00Z",
    "license":     null,                    # Checkov = Apache-2.0; human confirms
    "check_id":    "CKV_AWS_20",
}
```

**Open item (CONTRACTS §8.2):** where provenance sidecars are persisted is not
yet specified.  The importer returns them as Python objects; the caller decides
whether to write them to a sidecar YAML/JSON file next to each stub or to a
central manifest.

---

## CONTRACTS ambiguities flagged (§8)

### §8.1 — Artifact shape
The `dict` → temp-`.json` convention (§2) fits Checkov cleanly for
CloudFormation and Terraform plan JSON.  **No novel convention needed.**
The only limitation: HCL-native Terraform files cannot be represented as a
JSON dict; users must use Terraform plan JSON (`terraform show -json`) as the
artifact format.  This is within the documented scope.

### §8.2 — Provenance persistence
`import_catalog` returns `(stub_dict, provenance_dict)` tuples.  The
`Constraint` schema has no `provenance` field, so the sidecar is not embedded.
**Decision needed:** should provenance be written to a `<stub-id>.provenance.yaml`
sidecar file, appended to a central `provenance.manifest.yaml`, or held in
memory only?  The importer is silent on persistence — it returns data; the
calling tool decides.

### §8.3 — min_level default
`"warning"` is correct for Checkov.  Most actionable Checkov violations are
emitted at level `"error"` (defaultConfiguration) or `"warning"`.  `"note"`
level checks are informational.  The default `"warning"` threshold is appropriate.

### §8.4 — SARIF path in `raw` vs `path` field
The Violation `path` field and the embedded `uri` in `raw` both come from
SARIF `physicalLocation.artifactLocation.uri`.  When a temp file is used, both
are replaced with `"<artifact>"` for determinism.  This means the `path` field
is `"<artifact>"` rather than a meaningful file path when called from
`integrity.py` (which passes a parsed dict).  Callers that need the original
file path should pass a path string as `artifact` instead of a dict.
