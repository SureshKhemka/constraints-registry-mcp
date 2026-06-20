"""VH-MCP — MCP contract, scoping, and fail-open (VH-MCP-1/2/3).

These checks drive the *real* MCP tool-dispatch path (FastMCP ``call_tool`` /
``list_tools``), so they prove the documented MCP contract end-to-end
(FR-MCP-1/2/3), scoping (NFR-3), and fail-open behaviour (FR-MCP-4). They also
fold in FR-QUERY-2 (relationship selectors), FR-QUERY-3 (response fields),
FR-VALIDATE-3 (advisory=informational) and FR-VALIDATE-4 (no artifact mutation).
"""

from __future__ import annotations

import asyncio
import copy
import json

from ...config import RegistryConfig
from ...engine.registry import EngineRegistry
from ...mcp_server import build_server
from ...service import RegistryService
from ...store import BundleStore
from ..result import CheckResult

SECTION = "VH-MCP"


def _call(server, name: str, args: dict) -> dict:
    res = asyncio.run(server.call_tool(name, args))
    if isinstance(res, tuple):  # (content, structured) in some SDK versions
        return res[1]
    # list[ContentBlock] — structured payload is the JSON text of the first block.
    return json.loads(res[0].text)


def _contract(config: RegistryConfig) -> CheckResult:
    server = build_server(config_path=str(config.base_dir / "registry.config.yaml"))
    problems: list[str] = []

    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    if "get_constraints" not in tools or "validate" not in tools:
        return CheckResult.fail(
            SECTION, "VH-MCP-1", "MCP server missing required tools",
            details=[{"tools": list(tools)}],
        )
    # FR-MCP-3: tools expose documented input schemas.
    for name in ("get_constraints", "validate"):
        if not getattr(tools[name], "inputSchema", None):
            problems.append(f"{name} has no input schema")

    # describe_scope discovery tool: exposes the real selector vocabulary so an
    # agent can avoid guessing (e.g. aws_s3_bucket vs s3_bucket).
    if "describe_scope" not in tools:
        problems.append("describe_scope tool missing")
    else:
        ds = _call(server, "describe_scope", {})
        if not (ds.get("available") and "aws_s3_bucket" in ds.get("resource_types", [])
                and {"aws", "gcp"} <= set(ds.get("providers", []))
                and "tag:data-plane" in ds.get("repos", [])
                and "synchronous" in ds.get("relationship", {}).get("interactions", [])):
            problems.append(f"describe_scope vocabulary incomplete: {ds}")

    # get_constraints contract + FR-QUERY-3 fields.
    gc = _call(server, "get_constraints", {"scope": {"providers": ["aws"], "resource_types": ["aws_s3_bucket"], "environments": ["prod"], "repos": ["tag:data-plane"]}})
    if not (gc.get("available") and isinstance(gc.get("constraints"), list) and gc.get("bundle_id")):
        problems.append("get_constraints output shape invalid")
    required_fields = {"constraint", "intent", "guidance", "severity", "deprecated", "enforced", "enforced_by"}
    for c in gc.get("constraints", []):
        missing = required_fields - set(c)
        if missing:
            problems.append(f"constraint {c.get('constraint')} missing fields {missing}")
            break
    # FR-QUERY-3: an enforced constraint advertises its engine/stage.
    enforced = [c for c in gc.get("constraints", []) if c.get("enforced")]
    if enforced and not enforced[0]["enforced_by"]:
        problems.append("enforced constraint does not advertise enforced_by")

    # FR-QUERY-2: relationship-style selectors.
    rel = _call(server, "get_constraints", {"scope": {"relationship": {"source": {"layer": "domain-service"}, "target": {"layer": "domain-service", "different_domain": True}, "interaction": "synchronous"}}})
    rel_ids = {c["constraint"] for c in rel.get("constraints", [])}
    if "architecture-guild/arch.no-sync-cross-domain" not in rel_ids:
        problems.append(f"relationship query did not select the relationship constraint: {rel_ids}")

    # validate contract + FR-VALIDATE-3 (advisory=informational) + FR-VALIDATE-4 (no mutation).
    artifact = {"resources": {"aws_s3_bucket": {"data": {"acl": "public-read"}}}}
    before = copy.deepcopy(artifact)
    vr = _call(server, "validate", {"artifact": artifact, "scope": {"providers": ["aws"], "resource_types": ["aws_s3_bucket"], "environments": ["prod"], "repos": ["tag:data-plane"]}})
    if not (isinstance(vr.get("results"), list) and "passed" in vr and vr.get("bundle_id")):
        problems.append("validate output shape invalid")
    res_by = {r["constraint"]: r for r in vr.get("results", [])}
    pub = res_by.get("platform-security/aws.s3.no-public-access")
    if not (pub and pub["verdict"] == "fail" and pub["violations"]):
        problems.append("expected hard public-access constraint to fail with violations")
    adv = res_by.get("platform-security/tagging.required")
    if not (adv and adv["kind"] == "advisory" and adv["verdict"] == "informational"):
        problems.append("advisory constraint not reported as informational")
    for r in vr.get("results", []):
        if not {"constraint", "severity", "kind", "verdict", "violations"} <= set(r):
            problems.append(f"validate result missing fields: {r.get('constraint')}")
            break
    if artifact != before:
        problems.append("validate mutated the artifact (FR-VALIDATE-4)")

    if not problems:
        return CheckResult.ok(
            SECTION, "VH-MCP-1",
            "get_constraints and validate honor their documented contracts (incl. relationship scope, advisory, no-mutation)",
        )
    return CheckResult.fail(SECTION, "VH-MCP-1", "MCP contract check failed", details=[{"problems": problems}])


def _scoping(config: RegistryConfig) -> CheckResult:
    from ...importer import import_sources

    server = build_server(config_path=str(config.base_dir / "registry.config.yaml"))

    # Catalog size = every imported constraint; a scoped query must return fewer.
    total = len(import_sources(config).bundle.constraints)

    aws = _call(server, "get_constraints", {"scope": {"providers": ["aws"], "resource_types": ["aws_s3_bucket"], "environments": ["prod"], "repos": ["tag:data-plane"]}})
    aws_ids = {c["constraint"] for c in aws["constraints"]}

    gcp = _call(server, "get_constraints", {"scope": {"providers": ["gcp"], "resource_types": ["google_storage_bucket"], "environments": ["prod"], "repos": ["tag:analytics"]}})
    gcp_ids = {c["constraint"] for c in gcp["constraints"]}

    # Don't-care semantics: an omitted dimension (no repos here) must NOT zero out
    # results — the relevant S3 constraints still come back.
    natural = _call(server, "get_constraints", {"scope": {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]}})
    natural_ids = {c["constraint"] for c in natural["constraints"]}
    omitted_dim_ok = {
        "platform-security/aws.s3.no-public-access",
        "platform-security/aws.s3.require-encryption",
    } <= natural_ids and "data-platform/tagging.required" not in natural_ids

    scoped_ok = 0 < len(aws_ids) < total
    no_gcp_in_aws = "data-platform/tagging.required" not in aws_ids
    gcp_scoped = gcp_ids == {"data-platform/tagging.required"}

    if scoped_ok and no_gcp_in_aws and gcp_scoped and omitted_dim_ok:
        return CheckResult.ok(
            SECTION, "VH-MCP-2",
            f"scoped query returns {len(aws_ids)} of {total}; omitted dims are don't-care; never the full catalog",
        )
    return CheckResult.fail(
        SECTION, "VH-MCP-2", "scoping check failed",
        details=[{"total": total, "aws": sorted(aws_ids), "gcp": sorted(gcp_ids),
                  "natural": sorted(natural_ids), "omitted_dim_ok": omitted_dim_ok}],
    )


def _fail_open(config: RegistryConfig) -> CheckResult:
    # Genuine "index unavailable" state: a service with an empty store.
    empty = RegistryService(config, BundleStore(), EngineRegistry.from_config(config))
    server = build_server(service=empty)

    try:
        out = _call(server, "get_constraints", {"scope": {"providers": ["aws"]}})
    except Exception as exc:  # noqa: BLE001 - any raise here is a fail-open violation
        return CheckResult.fail(
            SECTION, "VH-MCP-3", f"get_constraints raised under unavailable index: {exc!r}"
        )

    if out.get("available") is False and out.get("constraints") == []:
        return CheckResult.ok(
            SECTION, "VH-MCP-3",
            "get_constraints fails open under unavailable index (proceed-able, no block)",
            details=[{"reason": out.get("reason")}],
        )
    return CheckResult.fail(SECTION, "VH-MCP-3", "fail-open behavior incorrect", details=[out])


def run(config: RegistryConfig) -> list[CheckResult]:
    return [_contract(config), _scoping(config), _fail_open(config)]
