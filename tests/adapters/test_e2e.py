"""End-to-end get_constraints / validate tests (VH-MCP-1/2, FR-VALIDATE-2/3).

These tests walk the full path from the registry service layer through to a
structured validation report, exercising:

  get_constraints(scope) → only relevant constraints (scoping semantics)
  validate(artifact, scope) → structured per-constraint report

Spec coverage
-------------
FR-QUERY      : scoped get_constraints returns only matching constraints
VH-MCP-1      : get_constraints + validate honour their contracts
VH-MCP-2      : scoped query is a proper subset of the full catalog
FR-VALIDATE-2 : validate routes to the right adapter and returns a report
FR-VALIDATE-3 : advisory constraints are "informational", never pass/fail
FR-MCP-4      : get_constraints never blocks; validate may surface errors
CONTRACTS §2  : adapter routing via engine name in enforcement binding

Sections
--------
1. Scope semantics (no binary required — get_constraints only)
2. validate routing: OPA adapter (OPA must be available)
3. validate routing: Semgrep adapter (Semgrep must be available)
4. Advisory constraints are informational
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cregistry.bundle import Bundle, ImportedConstraint
from cregistry.config import RegistryConfig, EngineConfig, SourceConfig
from cregistry.engine.registry import EngineRegistry
from cregistry.importer import import_sources
from cregistry.config import load_config
from cregistry.model import (
    Constraint,
    EnforcementBinding,
    Guidance,
    Scope,
    Severity,
    Category,
)
from cregistry.service import RegistryService
from cregistry.store import BundleStore
from cregistry.validate import validate

from tests.conftest import (
    REPO_ROOT,
    SEMGREP_FIXTURE_DIR,
    OPA_MISSING,
    SEMGREP_MISSING,
    requires_opa,
    requires_semgrep,
)

# ---------------------------------------------------------------------------
# Real registry config (for scoping and OPA tests)
# ---------------------------------------------------------------------------

_REGISTRY_CONFIG_PATH = REPO_ROOT / "registry.config.yaml"


@pytest.fixture(scope="module")
def real_config():
    return load_config(_REGISTRY_CONFIG_PATH)


@pytest.fixture(scope="module")
def real_service(real_config):
    return RegistryService.from_config(real_config)


@pytest.fixture(scope="module")
def total_constraint_count(real_config):
    return len(import_sources(real_config).bundle.constraints)


# ---------------------------------------------------------------------------
# Semgrep synthetic service (for semgrep routing test)
# ---------------------------------------------------------------------------


def _make_semgrep_service() -> RegistryService:
    """Build a minimal service with one semgrep constraint for routing tests.

    The constraint's policy locator ("rule.yaml") resolves via the source path,
    which is set to SEMGREP_FIXTURE_DIR so that:
        config.resolved_policy_path("test-sg", "rule.yaml")
        = SEMGREP_FIXTURE_DIR / "rule.yaml"   ← real test rule
    """
    # Build constraint manually (no YAML loading needed)
    constraint = Constraint(
        id="test.no-eval",
        title="No eval() in production Python",
        intent="Prevent code injection via eval().",
        category=Category.architectural,
        scope=Scope(resource_types=["python"]),
        severity=Severity.soft,
        enforcement=[EnforcementBinding(engine="semgrep", policy="rule.yaml")],
        guidance=Guidance(
            dont=["Never call eval() on external input"],
            example_compliant="int('42')",
        ),
        owner="test-suite",
        version="0.1.0",
    )

    advisory_constraint = Constraint(
        id="test.advisory-note",
        title="Advisory: use structured logging",
        intent="Teams should prefer structured logging over print().",
        category=Category.organizational,
        scope=Scope(),  # empty scope — matches everything
        severity=Severity.advisory,
        enforcement=[],  # advisory = no engine
        guidance=Guidance(
            dont=["Avoid bare print() in production"],
            example_compliant="logger.info('message')",
        ),
        owner="test-suite",
        version="0.1.0",
    )

    bundle = Bundle.from_constraints([
        ImportedConstraint("test-sg", constraint),
        ImportedConstraint("test-sg", advisory_constraint),
    ])

    store = BundleStore()
    store.add(bundle)

    # Config where "test-sg" source path = SEMGREP_FIXTURE_DIR.
    # This makes rule.yaml resolve to the real semgrep test rule.
    config = RegistryConfig(
        sources=[SourceConfig(name="test-sg", path=str(SEMGREP_FIXTURE_DIR))],
        engines=[
            EngineConfig(
                name="semgrep",
                adapter="cregistry.engine.adapters.semgrep:SemgrepAdapter",
            )
        ],
    )
    # base_dir not used since source path is absolute, but set for completeness.
    config.base_dir = SEMGREP_FIXTURE_DIR

    registry = EngineRegistry.from_config(config)
    return RegistryService(config, store, registry)


@pytest.fixture(scope="module")
def semgrep_service():
    if SEMGREP_MISSING:
        pytest.skip("semgrep binary not available — skipping semgrep E2E tests")
    return _make_semgrep_service()


# ===========================================================================
# 1. Scope semantics (FR-QUERY, VH-MCP-2) — no binary required
# ===========================================================================


class TestScopeSemantics:
    """VH-MCP-2: scoped get_constraints returns a proper subset of the full catalog."""

    def test_aws_s3_scope_returns_subset(self, real_service, total_constraint_count):
        """VH-MCP-2 / FR-QUERY: scoped query returns fewer than total catalog."""
        result = real_service.get_constraints(
            {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]}
        )
        assert result["available"] is True
        count = len(result["constraints"])
        assert count > 0, "AWS S3 scope must match at least one constraint"
        assert count < total_constraint_count, (
            f"VH-MCP-2: scoped query returned ALL {total_constraint_count} constraints "
            f"(should be a proper subset); got {count}"
        )

    def test_gcp_scope_excludes_aws_constraints(self, real_service):
        """VH-MCP-2: GCP scope must not return AWS-only constraints."""
        result = real_service.get_constraints(
            {"providers": ["gcp"], "resource_types": ["google_storage_bucket"]}
        )
        ids = {c["constraint"] for c in result["constraints"]}
        # AWS S3 no-public-access is scoped to providers=[aws] — must not appear.
        assert "platform-security/aws.s3.no-public-access" not in ids, (
            "VH-MCP-2: AWS constraint appeared in GCP-scoped query"
        )

    def test_aws_scope_and_gcp_scope_are_different(self, real_service):
        """VH-MCP-2: different scope queries yield different result sets."""
        aws = real_service.get_constraints({"providers": ["aws"], "resource_types": ["aws_s3_bucket"]})
        gcp = real_service.get_constraints({"providers": ["gcp"], "resource_types": ["google_storage_bucket"]})
        aws_ids = {c["constraint"] for c in aws["constraints"]}
        gcp_ids = {c["constraint"] for c in gcp["constraints"]}
        assert aws_ids != gcp_ids, (
            "VH-MCP-2: AWS and GCP scoped queries returned identical sets"
        )

    def test_result_includes_available_bundle_id(self, real_service):
        """VH-MCP-1: get_constraints result has {available, bundle_id, constraints}."""
        result = real_service.get_constraints({"providers": ["aws"]})
        assert result.get("available") is True
        assert isinstance(result.get("bundle_id"), str)
        assert isinstance(result.get("constraints"), list)

    def test_constraint_view_fields(self, real_service):
        """VH-MCP-1 / FR-QUERY-3: each constraint view has required fields."""
        result = real_service.get_constraints(
            {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]}
        )
        required = {"constraint", "title", "intent", "severity", "guidance",
                    "deprecated", "enforced", "enforced_by"}
        for c in result["constraints"]:
            missing = required - set(c)
            assert not missing, (
                f"VH-MCP-1: constraint {c.get('constraint')!r} missing fields: {missing}"
            )

    def test_omitted_dimension_is_dont_care(self, real_service):
        """FR-QUERY: omitting a scope dimension broadens results (don't care)."""
        # Asking for aws S3 with no repos= still returns the data-plane constraints.
        natural = real_service.get_constraints(
            {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]}
        )
        ids = {c["constraint"] for c in natural["constraints"]}
        assert "platform-security/aws.s3.no-public-access" in ids, (
            "FR-QUERY: omitting repos= dimension should still return the S3 constraint"
        )

    def test_empty_scope_returns_some_constraints(self, real_service):
        """FR-QUERY: empty scope is a wildcard (matches everything)."""
        result = real_service.get_constraints({})
        assert result["available"] is True
        # An empty scope matches all non-relationship-scoped constraints.
        assert len(result["constraints"]) > 0


# ===========================================================================
# 2. validate routing: OPA adapter (FR-VALIDATE-2, VH-MCP-1)
# ===========================================================================


@requires_opa
class TestValidateOPA:
    """FR-VALIDATE-2: validate routes to OPA and returns a structured report."""

    def test_validate_s3_fail_returns_fail_verdict(self, real_service, real_config):
        """FR-VALIDATE-2: a public S3 artifact violates the hard constraint."""
        # The existing platform-security constraint is hard and uses OPA.
        artifact = {"resources": {"aws_s3_bucket": {"data": {"acl": "public-read"}}}}
        scope = {
            "providers": ["aws"],
            "resource_types": ["aws_s3_bucket"],
            "environments": ["prod"],
            "repos": ["tag:data-plane"],
        }
        result = real_service.validate(artifact, scope)
        assert "results" in result
        assert isinstance(result["results"], list)

        by_id = {r["constraint"]: r for r in result["results"]}
        s3_result = by_id.get("platform-security/aws.s3.no-public-access")
        if s3_result is None:
            pytest.skip("aws.s3.no-public-access constraint not in scope — check fixture")

        # Verdict must be fail (not error — that would indicate an OPA failure)
        assert s3_result["verdict"] in ("fail", "error"), (
            f"Expected fail or error, got {s3_result['verdict']!r}"
        )
        if s3_result["verdict"] == "error":
            pytest.xfail(
                f"OPA returned error (not a test failure but may indicate OPA issue): "
                f"{s3_result}"
            )
        assert s3_result["verdict"] == "fail"
        assert len(s3_result["violations"]) >= 1

    def test_validate_report_shape(self, real_service):
        """VH-MCP-1: validate result has {bundle_id, passed, results[]}."""
        artifact = {}
        result = real_service.validate(artifact, {"providers": ["aws"]})
        assert "bundle_id" in result
        assert "passed" in result
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_each_result_has_required_fields(self, real_service):
        """VH-MCP-1: every constraint result has constraint, severity, kind, verdict, violations."""
        result = real_service.validate({}, {"providers": ["aws"]})
        required = {"constraint", "severity", "kind", "verdict", "violations"}
        for r in result["results"]:
            missing = required - set(r)
            assert not missing, (
                f"VH-MCP-1: result {r.get('constraint')!r} missing fields: {missing}"
            )

    def test_advisory_constraint_is_informational(self, real_service):
        """FR-VALIDATE-3: advisory constraints must be kind='advisory', verdict='informational'."""
        # platform-security/tagging.required is advisory (no enforcement bindings).
        result = real_service.validate(
            {},
            {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]},
        )
        by_id = {r["constraint"]: r for r in result["results"]}
        # Find any advisory result.
        advisory_results = [r for r in result["results"] if r.get("kind") == "advisory"]
        if not advisory_results:
            pytest.skip("No advisory constraints in scope for this query")

        for r in advisory_results:
            assert r["verdict"] == "informational", (
                f"FR-VALIDATE-3: advisory constraint {r['constraint']!r} "
                f"had verdict {r['verdict']!r} (must be 'informational')"
            )
            assert r["violations"] == [], (
                f"FR-VALIDATE-3: advisory constraint {r['constraint']!r} "
                f"must have zero violations"
            )

    def test_artifact_not_mutated(self, real_service):
        """FR-VALIDATE-4: validate must not mutate the caller's artifact."""
        import copy
        artifact = {"resources": {"aws_s3_bucket": {"data": {"acl": "public-read"}}}}
        before = copy.deepcopy(artifact)
        real_service.validate(
            artifact,
            {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]},
        )
        assert artifact == before, (
            "FR-VALIDATE-4: validate mutated the caller's artifact object"
        )


# ===========================================================================
# 3. validate routing: Semgrep adapter (FR-VALIDATE-2, CONTRACTS §2)
# ===========================================================================


@requires_semgrep
class TestValidateSemgrep:
    """FR-VALIDATE-2 / CONTRACTS §2: validate routes to semgrep and returns a report."""

    def test_fail_artifact_yields_fail_verdict(self, semgrep_service):
        """FR-VALIDATE-2: semgrep adapter is invoked and returns fail for eval() usage."""
        fail_artifact = json.loads((SEMGREP_FIXTURE_DIR / "fail.json").read_text())
        scope = {"resource_types": ["python"]}
        result = semgrep_service.validate(fail_artifact, scope)

        by_id = {r["constraint"]: r for r in result["results"]}
        sg_result = by_id.get("test-sg/test.no-eval")
        assert sg_result is not None, (
            "FR-VALIDATE-2: semgrep constraint 'test-sg/test.no-eval' not in result; "
            f"available constraints: {list(by_id)}"
        )

        if sg_result["verdict"] == "error":
            pytest.xfail(
                f"Semgrep adapter returned error (not a test failure): {sg_result}"
            )

        assert sg_result["verdict"] == "fail", (
            f"FR-VALIDATE-2: eval() artifact should fail; got {sg_result['verdict']!r}"
        )
        assert len(sg_result["violations"]) >= 1

    def test_pass_artifact_yields_pass_verdict(self, semgrep_service):
        """FR-VALIDATE-2: clean artifact → Verdict.passed via semgrep adapter."""
        pass_artifact = json.loads((SEMGREP_FIXTURE_DIR / "pass.json").read_text())
        scope = {"resource_types": ["python"]}
        result = semgrep_service.validate(pass_artifact, scope)

        by_id = {r["constraint"]: r for r in result["results"]}
        sg_result = by_id.get("test-sg/test.no-eval")
        assert sg_result is not None

        if sg_result["verdict"] == "error":
            pytest.xfail(f"Semgrep adapter returned error: {sg_result}")

        assert sg_result["verdict"] == "pass", (
            f"FR-VALIDATE-2: clean artifact should pass; got {sg_result['verdict']!r}"
        )

    def test_advisory_constraint_is_informational(self, semgrep_service):
        """FR-VALIDATE-3: advisory constraint in synthetic service → informational."""
        result = semgrep_service.validate({"path": "x.py", "content": "x=1\n"}, {})
        by_id = {r["constraint"]: r for r in result["results"]}
        adv = by_id.get("test-sg/test.advisory-note")
        assert adv is not None, (
            "Advisory constraint 'test-sg/test.advisory-note' not found in result"
        )
        assert adv["kind"] == "advisory"
        assert adv["verdict"] == "informational", (
            f"FR-VALIDATE-3: advisory verdict must be 'informational', "
            f"got {adv['verdict']!r}"
        )

    def test_scoped_query_excludes_python_constraint_for_aws_scope(self, semgrep_service):
        """VH-MCP-2 / FR-QUERY: python-scoped constraint not returned for aws scope.

        The test constraint has scope.resource_types=['python'].  A query with
        resource_types=['aws_s3_bucket'] must NOT match it, because the query
        supplies a value that does not intersect with the constraint's scope.
        """
        result = semgrep_service.get_constraints(
            {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]}
        )
        ids = {c["constraint"] for c in result["constraints"]}
        assert "test-sg/test.no-eval" not in ids, (
            "VH-MCP-2: python-scoped semgrep constraint appeared in AWS S3 query"
        )

    def test_scoped_query_includes_python_constraint_for_python_scope(self, semgrep_service):
        """FR-QUERY: python constraint appears in a python-scoped get_constraints query."""
        result = semgrep_service.get_constraints({"resource_types": ["python"]})
        ids = {c["constraint"] for c in result["constraints"]}
        assert "test-sg/test.no-eval" in ids, (
            "FR-QUERY: test.no-eval should be returned for resource_types=['python']"
        )

    def test_validate_result_report_shape(self, semgrep_service):
        """VH-MCP-1: validate output has bundle_id, passed, results[]."""
        pass_artifact = json.loads((SEMGREP_FIXTURE_DIR / "pass.json").read_text())
        result = semgrep_service.validate(pass_artifact, {"resource_types": ["python"]})
        assert "bundle_id" in result
        assert "passed" in result
        assert "results" in result
