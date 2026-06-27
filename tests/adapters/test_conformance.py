"""Engine-interface conformance suite (VH-ENGINE-2, FR-ENGINE-2).

A single parametrized suite that drives ``run_conformance`` (from
``engine/conformance.py``) for every registered adapter.  New engines plug in
by adding one entry to ADAPTER_PARAMS below — no new harness logic required
(FR-ENGINE-2).

Spec coverage
-------------
VH-ENGINE-2   : reusable conformance suite, same logic for any adapter
FR-ENGINE-2   : new engine validated by existing suite (no new harness code)
FR-ENGINE-3a  : can_handle accepts own engine, rejects foreign engine
FR-ENGINE-3b  : evaluate returns structured pass/fail verdict + determinism
NFR-1         : determinism (to_dict() stable across two evaluations)
NFR-2         : missing policy → EngineVerdict.error, never an exception
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cregistry.engine.conformance import ConformanceCase, run_conformance
from cregistry.engine.interface import Verdict
from cregistry.engine.adapters.checkov import CheckovAdapter
from cregistry.engine.adapters.semgrep import SemgrepAdapter
from cregistry.engine.adapters.opa import OpaAdapter
from cregistry.engine.adapters.conftest import ConftestAdapter

from tests.conftest import (
    CHECKOV_FIXTURE_DIR,
    SEMGREP_FIXTURE_DIR,
    CHECKOV_MISSING,
    SEMGREP_MISSING,
    OPA_MISSING,
    CONFTEST_MISSING,
    REPO_ROOT,
)

# ---------------------------------------------------------------------------
# OPA / Conftest fixture paths (shared Rego policy + JSON fixtures)
# ---------------------------------------------------------------------------

_S3_POLICY = REPO_ROOT / "sources" / "platform-security" / "policies" / "s3_public.rego"
_S3_PASS_ARTIFACT = json.loads(
    (REPO_ROOT / "sources" / "platform-security" / "fixtures" / "s3_private.json").read_text()
)
_S3_FAIL_ARTIFACT = json.loads(
    (REPO_ROOT / "sources" / "platform-security" / "fixtures" / "s3_public.json").read_text()
)

# ---------------------------------------------------------------------------
# Semgrep fixture paths
# ---------------------------------------------------------------------------

_SEMGREP_POLICY = SEMGREP_FIXTURE_DIR / "rule.yaml"
_SEMGREP_PASS_ARTIFACT = json.loads((SEMGREP_FIXTURE_DIR / "pass.json").read_text())
_SEMGREP_FAIL_ARTIFACT = json.loads((SEMGREP_FIXTURE_DIR / "fail.json").read_text())

# ---------------------------------------------------------------------------
# Checkov fixture paths
# ---------------------------------------------------------------------------

_CHECKOV_POLICY = CHECKOV_FIXTURE_DIR / "policy.yaml"
_CHECKOV_PASS_ARTIFACT = json.loads((CHECKOV_FIXTURE_DIR / "pass.json").read_text())
_CHECKOV_FAIL_ARTIFACT = json.loads((CHECKOV_FIXTURE_DIR / "fail.json").read_text())

# ---------------------------------------------------------------------------
# ConformanceCase builders (called lazily so paths are not resolved on import)
# ---------------------------------------------------------------------------


def _opa_cases() -> list[ConformanceCase]:
    return [
        ConformanceCase(
            "s3-public-access:pass",
            str(_S3_POLICY),
            _S3_PASS_ARTIFACT,
            Verdict.passed,
        ),
        ConformanceCase(
            "s3-public-access:fail",
            str(_S3_POLICY),
            _S3_FAIL_ARTIFACT,
            Verdict.failed,
        ),
    ]


def _conftest_cases() -> list[ConformanceCase]:
    # Conftest uses the same Rego policies as OPA (same deny/violation convention).
    return _opa_cases()


def _semgrep_cases() -> list[ConformanceCase]:
    return [
        ConformanceCase(
            "no-eval:pass",
            str(_SEMGREP_POLICY),
            _SEMGREP_PASS_ARTIFACT,
            Verdict.passed,
        ),
        ConformanceCase(
            "no-eval:fail",
            str(_SEMGREP_POLICY),
            _SEMGREP_FAIL_ARTIFACT,
            Verdict.failed,
        ),
    ]


def _checkov_cases() -> list[ConformanceCase]:
    return [
        ConformanceCase(
            "s3-acl-private:pass",
            str(_CHECKOV_POLICY),
            _CHECKOV_PASS_ARTIFACT,
            Verdict.passed,
        ),
        ConformanceCase(
            "s3-acl-private:fail",
            str(_CHECKOV_POLICY),
            _CHECKOV_FAIL_ARTIFACT,
            Verdict.failed,
        ),
    ]


# ---------------------------------------------------------------------------
# Parametrize table — add one row per new engine (FR-ENGINE-2).
# Each row: (adapter_class, cases_fn, binary_missing_flag, adapter_name)
# ---------------------------------------------------------------------------

ADAPTER_PARAMS = [
    pytest.param(
        OpaAdapter,
        _opa_cases,
        OPA_MISSING,
        "opa",
        id="opa",
        marks=pytest.mark.skipif(OPA_MISSING, reason="opa binary not on PATH"),
    ),
    pytest.param(
        ConftestAdapter,
        _conftest_cases,
        CONFTEST_MISSING,
        "conftest",
        id="conftest",
        marks=pytest.mark.skipif(CONFTEST_MISSING, reason="conftest binary not on PATH"),
    ),
    pytest.param(
        SemgrepAdapter,
        _semgrep_cases,
        SEMGREP_MISSING,
        "semgrep",
        id="semgrep",
        marks=pytest.mark.skipif(SEMGREP_MISSING, reason="semgrep binary not on PATH"),
    ),
    pytest.param(
        CheckovAdapter,
        _checkov_cases,
        CHECKOV_MISSING,
        "checkov",
        id="checkov",
        marks=pytest.mark.skipif(CHECKOV_MISSING, reason="checkov binary not on PATH"),
    ),
]


# ---------------------------------------------------------------------------
# The conformance test itself — one test body, all adapters (FR-ENGINE-2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter_cls,cases_fn,bin_missing,adapter_name", ADAPTER_PARAMS)
def test_engine_conformance(adapter_cls, cases_fn, bin_missing, adapter_name):
    """VH-ENGINE-2 / FR-ENGINE-2: run_conformance against a real adapter instance.

    Checks performed by run_conformance (CONTRACTS §7 / engine/conformance.py):
      - can_handle: accepts own engine, rejects a foreign engine name  (FR-ENGINE-3a)
      - missing_policy_error: evaluate on non-existent policy → error verdict, no raise (NFR-2)
      - per-case verdict + violation count (FR-ENGINE-3b)
      - determinism: two evaluations of same (artifact, policy) → identical to_dict() (NFR-1)
    """
    adapter = adapter_cls()

    # Guard: if the binary is missing, the parametrize mark should have skipped us;
    # this is a defensive double-check so we never silently pass due to an adapter
    # that silently returns 'pass' when the binary is absent.
    if not getattr(adapter, "available", True):
        pytest.skip(f"{adapter_name} binary not available at runtime")

    cases = cases_fn()
    results = run_conformance(adapter, cases)

    failed = [r for r in results if not r["ok"]]
    assert not failed, (
        f"VH-ENGINE-2: conformance failures for adapter '{adapter_name}':\n"
        + "\n".join(str(r) for r in failed)
    )

    # Sanity: must have at least the fixed checks + one per case
    # (can_handle=1, missing_policy=1, cases=len(cases))
    expected_min = 2 + len(cases)
    assert len(results) >= expected_min, (
        f"Expected at least {expected_min} result dicts from run_conformance, "
        f"got {len(results)}"
    )


# ---------------------------------------------------------------------------
# Standalone can_handle / missing_policy checks (decoupled from the full suite)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter_cls,adapter_name", [
    pytest.param(CheckovAdapter, "checkov", id="checkov"),
    pytest.param(SemgrepAdapter, "semgrep", id="semgrep"),
])
def test_can_handle_own_and_foreign(adapter_cls, adapter_name):
    """FR-ENGINE-3a: can_handle keys off engine name; no binary required."""
    from cregistry.model import EnforcementBinding

    adapter = adapter_cls()
    own = adapter.can_handle(EnforcementBinding(engine=adapter_name, policy="x"))
    foreign = adapter.can_handle(EnforcementBinding(engine="__not_this_engine__", policy="x"))
    assert own is True, f"{adapter_name}: can_handle(own) should be True"
    assert foreign is False, f"{adapter_name}: can_handle(foreign) should be False"


@pytest.mark.parametrize("adapter_cls,adapter_name", [
    pytest.param(CheckovAdapter, "checkov", id="checkov"),
    pytest.param(SemgrepAdapter, "semgrep", id="semgrep"),
])
def test_missing_policy_returns_error_not_raise(adapter_cls, adapter_name):
    """NFR-2: evaluate with a missing policy must return error verdict, never raise."""
    adapter = adapter_cls()
    try:
        result = adapter.evaluate({}, "/no/such/policy/__conformance_missing__.yaml")
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"VH-ENGINE-2 / NFR-2: {adapter_name}.evaluate raised instead of "
            f"returning errored: {exc!r}"
        )
    assert result.verdict is Verdict.error, (
        f"NFR-2: expected Verdict.error for missing policy, got {result.verdict!r}"
    )
    assert result.error is not None, "errored verdict must carry an error message"
