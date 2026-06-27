"""Fail-open test for get_constraints (FR-MCP-4 / VH-MCP-3).

When the bundle index is unavailable (empty BundleStore), ``get_constraints``
must:
  - NOT raise
  - Return ``{"available": False, "constraints": []}``
  so the calling agent can proceed unblocked.

``validate`` is allowed to surface an explicit error (FR-MCP-4: guidance path
never blocks; enforcement path may surface errors).

Spec coverage
-------------
FR-MCP-4  : get_constraints fails open; validate may error
VH-MCP-3  : fail-open behaviour verified with an empty/unavailable index
"""

from __future__ import annotations

import pytest

from cregistry.config import RegistryConfig
from cregistry.engine.registry import EngineRegistry
from cregistry.service import RegistryService, ValidationUnavailable
from cregistry.store import BundleStore


# ---------------------------------------------------------------------------
# Fixture: a service backed by an EMPTY BundleStore (no bundles loaded).
# This is the most direct simulation of "index unavailable" per the existing
# harness check in ``checks/mcp.py`` _fail_open().
# ---------------------------------------------------------------------------


@pytest.fixture()
def empty_service():
    """RegistryService with zero bundles — simulates an unavailable index."""
    config = RegistryConfig()  # no sources, no engines
    store = BundleStore()      # empty — latest() returns None
    registry = EngineRegistry.from_config(config)
    return RegistryService(config, store, registry)


# ===========================================================================
# FR-MCP-4 / VH-MCP-3
# ===========================================================================


def test_get_constraints_does_not_raise_when_index_unavailable(empty_service):
    """FR-MCP-4: get_constraints must NEVER raise for any reason."""
    try:
        result = empty_service.get_constraints({"providers": ["aws"]})
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"FR-MCP-4 / VH-MCP-3: get_constraints raised when index was "
            f"unavailable: {exc!r}"
        )
    # If we reach here, the call completed without raising.
    assert result is not None


def test_get_constraints_returns_available_false(empty_service):
    """FR-MCP-4: result must have available=False (not True) when no index."""
    result = empty_service.get_constraints({"providers": ["aws"]})
    assert result.get("available") is False, (
        f"VH-MCP-3: expected available=False, got {result.get('available')!r}. "
        f"Full result: {result}"
    )


def test_get_constraints_returns_empty_constraints_list(empty_service):
    """FR-MCP-4: empty constraints list so the caller can proceed unblocked."""
    result = empty_service.get_constraints({"providers": ["aws"]})
    assert result.get("constraints") == [], (
        f"VH-MCP-3: expected constraints=[], got {result.get('constraints')!r}"
    )


def test_get_constraints_none_scope_does_not_raise(empty_service):
    """FR-MCP-4: None scope (not just empty dict) must also degrade gracefully."""
    try:
        result = empty_service.get_constraints(None)
    except Exception as exc:
        pytest.fail(f"FR-MCP-4: get_constraints(None) raised: {exc!r}")
    assert result.get("available") is False


def test_get_constraints_malformed_scope_does_not_raise(empty_service):
    """FR-MCP-4: even a malformed scope dict must not crash the service."""
    try:
        result = empty_service.get_constraints({"__bad_key__": "unknown"})
    except Exception as exc:
        pytest.fail(
            f"FR-MCP-4: get_constraints with malformed scope raised: {exc!r}"
        )
    # Either it degraded with available=False, or if scope validation is lenient,
    # it still returned a valid (empty) result.
    assert isinstance(result, dict)
    assert "available" in result


def test_validate_raises_validation_unavailable(empty_service):
    """FR-MCP-4: validate MAY surface an explicit error (not silently swallowed).

    The service raises ValidationUnavailable when no bundle is loaded.
    The MCP handler catches it and returns an error dict — but the service
    itself is allowed to raise.
    """
    with pytest.raises(ValidationUnavailable):
        empty_service.validate({"resources": {}}, {"providers": ["aws"]})


def test_get_constraints_returns_reason_field(empty_service):
    """FR-MCP-4: the response should explain WHY the index is unavailable."""
    result = empty_service.get_constraints({"providers": ["aws"]})
    # Either "reason" field or some explanation is present.
    has_reason = bool(result.get("reason"))
    # Not a hard requirement from the spec, but the service currently sets it.
    # Use xfail if it disappears so we notice without blocking CI.
    if not has_reason:
        pytest.xfail(
            "FR-MCP-4: service did not include a 'reason' field in fail-open response"
        )
