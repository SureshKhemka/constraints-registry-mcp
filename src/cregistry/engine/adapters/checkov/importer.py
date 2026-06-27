"""Checkov catalog importer (CONTRACTS §6).

Turns Checkov's built-in check catalog into registry constraint STUBS.
This is a standalone module function — it is NOT part of the ``EngineAdapter``
ABC and is unrelated to ``cregistry.importer`` (which imports authored
constraints from source repos).

Usage
-----
::

    from cregistry.engine.adapters.checkov.importer import import_catalog

    stubs = import_catalog("builtin")
    for stub, provenance in stubs:
        # stub  → dict that round-trips through Constraint.model_validate
        # provenance → sidecar dict (source, imported_at, license, check_id)
        print(stub["id"], provenance["imported_at"])

Design notes (CONTRACTS §6)
----------------------------
* ``policy`` in ``EnforcementBinding`` is the **only** engine-binding field
  (``extra="forbid"``).  Stubs carry a placeholder locator of the form
  ``"checkov/<CHECK_ID>"`` so a human can replace it with the real config path.
* ``Constraint`` has **no** provenance field.  License / source / imported_at
  are returned as a **sidecar dict** alongside each stub — never inside the
  constraint dict.
* ``severity`` is always ``"soft"`` (CONTRACTS §6: never ``"hard"`` on import).
* ``category``:
  - ``CKV2_*`` checks are frequently graph / cross-resource → ``"architectural"``
  - all others → ``"infrastructure"``
* ``scope``: best-effort from the check metadata (provider + resource_types).
* ``intent`` / ``example_compliant``: ``"TODO: human"`` placeholder (min_length=1
  satisfied; human enriches before publishing).

Idempotency and determinism
---------------------------
``import_catalog`` is idempotent: the same Checkov version produces the same
stubs in the same order (sorted by stub id).  The ``source_ref`` recorded in
provenance disambiguates catalog versions.

When Checkov is not installed the function returns the hard-coded seed set so
that the importer is usable (and testable) without the binary.

``checkov --list`` output parsing
----------------------------------
Checkov prints one block per check::

    Check: CKV_AWS_20
        ID: CKV_AWS_20
        File(s): ...
        Supported resources: aws_s3_bucket
        Description: Ensure the S3 bucket ACL is private
        Guide: https://...

The parser extracts ID and Description (title) and Supported resources.  Any
unparseable block is skipped silently; the seed set is used if the entire
parse yields zero checks.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
from typing import Any

# ---------------------------------------------------------------------------
# Seed catalog — used when checkov is not installed or --list parse fails.
# Covers a representative sample of AWS, Azure, Kubernetes checks including
# both single-resource (CKV_*) and graph/cross-resource (CKV2_*) checks.
# ---------------------------------------------------------------------------

_SEED_CHECKS: list[dict[str, Any]] = [
    # --- AWS S3 ---
    {
        "id": "CKV_AWS_20",
        "title": "Ensure the S3 bucket ACL is private",
        "resource_types": ["aws_s3_bucket"],
        "providers": ["aws"],
        "is_graph": False,
    },
    {
        "id": "CKV_AWS_18",
        "title": "Ensure the S3 bucket has access logging enabled",
        "resource_types": ["aws_s3_bucket"],
        "providers": ["aws"],
        "is_graph": False,
    },
    {
        "id": "CKV_AWS_21",
        "title": "Ensure the S3 bucket has versioning enabled",
        "resource_types": ["aws_s3_bucket"],
        "providers": ["aws"],
        "is_graph": False,
    },
    {
        "id": "CKV2_AWS_6",
        "title": "Ensure that S3 bucket has a Public Access block",
        "resource_types": ["aws_s3_bucket"],
        "providers": ["aws"],
        "is_graph": False,  # single-resource despite CKV2_ prefix
    },
    {
        "id": "CKV2_AWS_61",
        "title": "Ensure that an S3 bucket has a lifecycle configuration",
        "resource_types": ["aws_s3_bucket"],
        "providers": ["aws"],
        "is_graph": True,  # cross-resource: bucket + lifecycle policy
    },
    # --- AWS IAM ---
    {
        "id": "CKV_AWS_40",
        "title": "Ensure IAM policies are attached only to groups or roles",
        "resource_types": ["aws_iam_user_policy"],
        "providers": ["aws"],
        "is_graph": False,
    },
    {
        "id": "CKV_AWS_1",
        "title": "Ensure IAM policies that allow full administrative privileges are not created",
        "resource_types": ["aws_iam_policy"],
        "providers": ["aws"],
        "is_graph": False,
    },
    # --- AWS network ---
    {
        "id": "CKV_AWS_2",
        "title": "Ensure ALB protocol is HTTPS",
        "resource_types": ["aws_alb_listener", "aws_lb_listener"],
        "providers": ["aws"],
        "is_graph": False,
    },
    {
        "id": "CKV_AWS_25",
        "title": "Ensure no security groups allow ingress from 0.0.0.0:0 to port 3389",
        "resource_types": ["aws_security_group"],
        "providers": ["aws"],
        "is_graph": False,
    },
    # --- Azure storage ---
    {
        "id": "CKV_AZURE_3",
        "title": "Ensure that 'Enable infrastructure encryption' for each storage account is set to 'enabled'",
        "resource_types": ["azurerm_storage_account"],
        "providers": ["azurerm"],
        "is_graph": False,
    },
    # --- Kubernetes ---
    {
        "id": "CKV_K8S_21",
        "title": "Do not admit containers wishing to share the host network namespace",
        "resource_types": ["kubernetes_pod", "kubernetes_deployment"],
        "providers": ["kubernetes"],
        "is_graph": False,
    },
    # --- GCP ---
    {
        "id": "CKV_GCP_29",
        "title": "Ensure that Cloud Storage buckets have uniform bucket-level access enabled",
        "resource_types": ["google_storage_bucket"],
        "providers": ["google"],
        "is_graph": False,
    },
    # --- cross-resource graph checks (CKV2_) ---
    {
        "id": "CKV2_AWS_12",
        "title": "Ensure the default security group of every VPC restricts all traffic",
        "resource_types": ["aws_vpc", "aws_default_security_group"],
        "providers": ["aws"],
        "is_graph": True,
    },
    {
        "id": "CKV2_AZURE_1",
        "title": "Ensure that Azure resources are using the latest TLS protocol version",
        "resource_types": ["azurerm_app_service"],
        "providers": ["azurerm"],
        "is_graph": True,
    },
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_catalog(source_ref: str) -> list[tuple[dict, dict]]:
    """Enumerate Checkov checks and return ``(stub_dict, provenance_dict)`` pairs.

    Parameters
    ----------
    source_ref:
        Recorded in the sidecar provenance.  Pass the Checkov version string
        (e.g. ``"checkov/3.2.1"``) or the sentinel ``"builtin"`` when using
        the seed set directly.

    Returns
    -------
    list[tuple[dict, dict]]
        Each tuple is ``(constraint_stub, provenance_sidecar)``.
        The stub round-trips through ``Constraint.model_validate`` (minus the
        human TODO fields, which validate as-is because ``min_length=1``).
        The provenance sidecar is NOT embedded in the stub (schema forbids it).

    Notes
    -----
    * Output is sorted by ``stub["id"]`` for determinism.
    * Duplicate check IDs are deduplicated (first occurrence wins).
    * The importer is idempotent: same Checkov version → same stubs.
    """
    imported_at = datetime.datetime.utcnow().isoformat() + "Z"
    provenance_base = {
        "source": source_ref,
        "imported_at": imported_at,
        # Checkov is Apache-2.0; individual check rules inherit that license.
        # Populated as null so callers can override per-check if needed.
        "license": None,
    }

    raw_checks = _enumerate_checks()

    results: list[tuple[dict, dict]] = []
    seen_ids: set[str] = set()

    for check in raw_checks:
        check_id: str = check.get("id", "")
        if not check_id or check_id in seen_ids:
            continue
        seen_ids.add(check_id)

        stub = _make_stub(check)
        provenance = {**provenance_base, "check_id": check_id}
        results.append((stub, provenance))

    # Deterministic ordering: same catalog ⇒ same stubs in same order.
    results.sort(key=lambda t: t[0]["id"])
    return results


# ---------------------------------------------------------------------------
# Internal: check enumeration
# ---------------------------------------------------------------------------

def _enumerate_checks() -> list[dict[str, Any]]:
    """Return raw check metadata dicts.

    Tries ``checkov --list`` first; falls back to the seed set on any failure.
    """
    checkov_bin = shutil.which("checkov")
    if checkov_bin is None:
        return _SEED_CHECKS

    try:
        proc = subprocess.run(
            [checkov_bin, "--list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return _SEED_CHECKS

    if proc.returncode == 0 and proc.stdout.strip():
        parsed = _parse_list_output(proc.stdout)
        if parsed:
            return parsed

    return _SEED_CHECKS


def _parse_list_output(output: str) -> list[dict[str, Any]]:
    """Parse ``checkov --list`` text output into raw check dicts.

    Handles the documented Checkov --list format::

        Check: CKV_AWS_20
            ID: CKV_AWS_20
            File(s): /path/to/check.py
            Supported resources: aws_s3_bucket
            Description: Ensure the S3 bucket ACL is private
            Guide: https://docs.bridgecrew.io/...

    Unknown lines are skipped.  Returns [] if no checks could be parsed.
    """
    checks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("Check:"):
            if current is not None and current.get("id"):
                checks.append(current)
            check_id = line.split(":", 1)[1].strip()
            current = {
                "id": check_id,
                "title": "",
                "resource_types": [],
                "providers": [],
                "is_graph": check_id.startswith("CKV2_"),
            }
        elif current is not None:
            if line.startswith("ID:"):
                # Confirm/override ID from the block header.
                current["id"] = line.split(":", 1)[1].strip()
            elif line.startswith("Description:"):
                current["title"] = line.split(":", 1)[1].strip()
            elif line.startswith("Supported resources:"):
                resources_str = line.split(":", 1)[1].strip()
                current["resource_types"] = [
                    r.strip() for r in resources_str.split(",") if r.strip()
                ]
                current["providers"] = _infer_providers(current["resource_types"])
            # "Guide:", "File(s):" → skip

    if current is not None and current.get("id"):
        checks.append(current)

    return checks


def _infer_providers(resource_types: list[str]) -> list[str]:
    """Best-effort provider inference from resource-type prefixes."""
    _PREFIX_MAP = {
        "aws": "aws",
        "azurerm": "azurerm",
        "google": "google",
        "kubernetes": "kubernetes",
        "helm": "kubernetes",
        "github": "github",
    }
    providers: set[str] = set()
    for rt in resource_types:
        prefix = rt.split("_")[0] if "_" in rt else rt
        mapped = _PREFIX_MAP.get(prefix)
        if mapped:
            providers.add(mapped)
    return sorted(providers)


# ---------------------------------------------------------------------------
# Internal: stub construction
# ---------------------------------------------------------------------------

def _make_stub(check: dict[str, Any]) -> dict:
    """Build a Constraint stub dict from a raw check entry.

    The returned dict is valid input to ``Constraint.model_validate`` once the
    human fills in ``intent`` and ``guidance.example_compliant`` (both are set
    to the placeholder ``"TODO: human"`` which satisfies ``min_length=1``).

    Provenance (source, imported_at, license) is NOT embedded here — the caller
    includes it in the sidecar (CONTRACTS §6: Constraint is ``extra="forbid"``).
    """
    check_id: str = check["id"]
    title: str = check.get("title") or check_id
    providers: list[str] = check.get("providers") or []
    resource_types: list[str] = check.get("resource_types") or []
    is_graph: bool = check.get("is_graph", False)

    # Stub ID: "checkov/<slug>" where slug = lowercase check_id, _ → -
    slug = check_id.lower().replace("_", "-")
    stub_id = f"checkov/{slug}"

    # Category:
    #   graph / cross-resource checks (CKV2_* and is_graph=True) → architectural
    #   single-resource infrastructure checks → infrastructure
    category = "architectural" if is_graph else "infrastructure"

    # Scope: best-effort from check metadata; omit empty lists.
    scope: dict[str, Any] = {}
    if providers:
        scope["providers"] = providers
    if resource_types:
        scope["resource_types"] = resource_types

    # Policy locator placeholder.  The human replaces this with the real
    # path to a Checkov config YAML that selects this check.
    policy_locator = f"checkov/{check_id}"

    return {
        "id": stub_id,
        "title": title,
        "intent": "TODO: human",
        "category": category,
        "scope": scope,
        "severity": "soft",
        "enforcement": [
            {"engine": "checkov", "policy": policy_locator}
        ],
        "guidance": {
            "dont": [title],
            "example_compliant": "TODO: human",
        },
        "owner": "imported",
        "version": "0.1.0",
    }
