"""SARIF normalizer unit tests — no binary required, always run.

Spec coverage
-------------
CONTRACTS §5   : parse_sarif / compute_result / get_sarif_level frozen signatures
CONTRACTS §4   : Violation shape (message, rule, path, raw, remediation)
FR-ENGINE-3b   : evaluate returns well-formed EngineVerdict
VH-ENGINE-2    : engine-interface conformance (SARIF seam layer)
VH-OUTPUT-2    : self-contained fixtures; no external repos needed

Tests are organised by behaviour cluster so the coverage map is unambiguous.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# Package under test — SARIF seam (CONTRACTS §5)
from cregistry.engine.adapters.sarif import (
    compute_result,
    get_sarif_level,
    parse_sarif,
)
from cregistry.engine.interface import EngineVerdict, Verdict, Violation

# Fixture files shipped by the sarif-normalizer agent (VH-OUTPUT-2).
from tests.conftest import SARIF_FIXTURE_DIR


def _load(name: str) -> dict:
    """Load a SARIF fixture JSON from the normalizer's _fixtures/ directory."""
    return json.loads((SARIF_FIXTURE_DIR / name).read_text())


# ===========================================================================
# Helpers / inline blobs
# ===========================================================================

# Minimal SARIF with two runs:
#   run 0 — one error result with helpUri remediation + one note result (no helpUri)
#   run 1 — empty (contributes nothing)
_INLINE_TWO_RUN: dict = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {
                "driver": {
                    "name": "inline-test",
                    "rules": [
                        {
                            "id": "RULE_ERR",
                            "helpUri": "https://example.com/RULE_ERR",
                            "defaultConfiguration": {"level": "error"},
                        }
                    ],
                }
            },
            "results": [
                {
                    "ruleId": "RULE_ERR",
                    "level": "error",
                    "message": {"text": "bad thing happened"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/foo.py"}
                            }
                        }
                    ],
                },
                {
                    "ruleId": "RULE_NOTE",
                    "level": "note",
                    "message": {"text": "just a note"},
                },
            ],
        },
        {
            "tool": {"driver": {"name": "inline-test", "rules": []}},
            "results": [],
        },
    ],
}

# SARIF with a result whose level is absent — must fall back to
# defaultConfiguration.level from the rule map.
_DEFAULT_CONFIG_FALLBACK: dict = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {
                "driver": {
                    "name": "fallback-test",
                    "rules": [
                        {
                            "id": "RULE_DC",
                            "defaultConfiguration": {"level": "error"},
                        }
                    ],
                }
            },
            "results": [
                {
                    # No "level" key — must fall back to defaultConfiguration.
                    "ruleId": "RULE_DC",
                    "message": {"text": "fallback level test"},
                }
            ],
        }
    ],
}

# SARIF where the only result has level="none" — must produce zero violations.
_NONE_LEVEL_ONLY: dict = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "none-test", "rules": []}},
            "results": [
                {
                    "ruleId": "DROP_ME",
                    "level": "none",
                    "message": {"text": "suppressed"},
                }
            ],
        }
    ],
}

# Multi-run SARIF to verify violations are gathered from ALL runs.
_MULTI_RUN: dict = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "e1", "rules": []}},
            "results": [
                {"ruleId": "R1", "level": "error", "message": {"text": "run-one-error"}}
            ],
        },
        {
            "tool": {"driver": {"name": "e2", "rules": []}},
            "results": [
                {"ruleId": "R2", "level": "warning", "message": {"text": "run-two-warning"}}
            ],
        },
    ],
}


# ===========================================================================
# 1. Checkov sample fixture (VH-OUTPUT-2, CONTRACTS §5)
# ===========================================================================

class TestCheckovSample:
    """CONTRACTS §5: parse_sarif on the bundled Checkov SARIF fixture."""

    def setup_method(self):
        self.sarif = _load("checkov_sample.json")
        self.violations = parse_sarif(self.sarif, "checkov")

    # CONTRACTS §5 — three results survive (no level=none in this fixture)
    def test_violation_count(self):
        # FR-ENGINE-3b: all three results (error, warning-fallback, note-fallback)
        assert len(self.violations) == 3

    def test_explicit_error_level(self):
        # CKV_AWS_20 carries an explicit level="error"
        v = next(v for v in self.violations if v.rule == "CKV_AWS_20")
        assert get_sarif_level(v.raw) == "error"

    def test_defaultconfiguration_fallback_warning(self):
        # CKV_AWS_18 has no result-level; rule defaultConfiguration.level = "warning"
        v = next(v for v in self.violations if v.rule == "CKV_AWS_18")
        assert get_sarif_level(v.raw) == "warning"

    def test_defaultconfiguration_fallback_note(self):
        # CKV_AWS_21 has no result-level; rule defaultConfiguration.level = "note"
        v = next(v for v in self.violations if v.rule == "CKV_AWS_21")
        assert get_sarif_level(v.raw) == "note"

    def test_path_extracted(self):
        # All results share the same physicalLocation URI
        assert all(v.path == "infra/main.tf" for v in self.violations)

    def test_remediation_from_help_uri(self):
        # CKV_AWS_20 and CKV_AWS_18 have help.uri → remediation must be populated
        # CONTRACTS §5: "remediation may be pre-filled from helpUri"
        for rule_id in ("CKV_AWS_20", "CKV_AWS_18"):
            v = next(v for v in self.violations if v.rule == rule_id)
            assert v.remediation is not None, (
                f"{rule_id}: remediation should be set from help.uri"
            )

    def test_raw_is_shallow_copy_with_level(self):
        # CONTRACTS §5: "Violation.raw is a SHALLOW COPY with an injected level key"
        # do NOT assert raw is original; DO assert raw is a dict with a level key
        for v in self.violations:
            assert isinstance(v.raw, dict), "raw must be a dict"
            assert "level" in v.raw, "raw must have injected level key"

    def test_min_level_warning_filters_note(self):
        # compute_result with min_level="warning" keeps error+warning, drops note
        result = compute_result(self.violations, "checkov-policy.yaml", "checkov", min_level="warning")
        assert result.verdict is Verdict.failed
        rules_kept = {v.rule for v in result.violations}
        assert "CKV_AWS_21" not in rules_kept, "note-level violation should be filtered out"
        assert len(result.violations) == 2

    def test_min_level_error_keeps_only_error(self):
        # min_level=error → only CKV_AWS_20 survives
        result = compute_result(self.violations, "checkov-policy.yaml", "checkov", min_level="error")
        assert len(result.violations) == 1
        assert result.violations[0].rule == "CKV_AWS_20"

    def test_min_level_note_keeps_all(self):
        # min_level=note → all three kept → verdict fail
        result = compute_result(self.violations, "checkov-policy.yaml", "checkov", min_level="note")
        assert len(result.violations) == 3

    def test_deterministic_sort_order(self):
        # CONTRACTS §5 / NFR-1: violations sorted by (rule, path, message)
        # CKV_AWS_18 < CKV_AWS_20 alphabetically
        result = compute_result(self.violations, "p", "checkov", min_level="warning")
        assert result.violations[0].rule == "CKV_AWS_18"
        assert result.violations[1].rule == "CKV_AWS_20"


# ===========================================================================
# 2. Semgrep sample fixture (VH-OUTPUT-2, CONTRACTS §5)
# ===========================================================================

class TestSemgrepSample:
    """CONTRACTS §5: parse_sarif on the bundled Semgrep SARIF fixture."""

    def setup_method(self):
        self.sarif = _load("semgrep_sample.json")
        self.violations = parse_sarif(self.sarif, "semgrep")

    def test_none_level_dropped(self):
        # CONTRACTS §4: "SARIF level='none' → drop; never becomes a Violation"
        # The fixture has 3 results; the level=none one must be dropped.
        assert len(self.violations) == 2

    def test_no_none_in_levels(self):
        levels = [get_sarif_level(v.raw) for v in self.violations]
        assert "none" not in levels

    def test_error_and_warning_present(self):
        levels = set(get_sarif_level(v.raw) for v in self.violations)
        assert "error" in levels
        assert "warning" in levels

    def test_min_level_error_keeps_one(self):
        result = compute_result(self.violations, "semgrep-rules.yml", "semgrep", min_level="error")
        assert len(result.violations) == 1
        assert "exec" in result.violations[0].rule

    def test_min_level_warning_default_keeps_both(self):
        result = compute_result(self.violations, "semgrep-rules.yml", "semgrep")
        assert len(result.violations) == 2


# ===========================================================================
# 3. Malformed sample fixture (VH-OUTPUT-2, CONTRACTS §5)
# ===========================================================================

class TestMalformedSample:
    """CONTRACTS §5: parse_sarif never raises on malformed inputs."""

    def setup_method(self):
        sarif = _load("malformed_sample.json")
        self.violations = parse_sarif(sarif, "broken-engine")

    def test_no_raise_on_malformed(self):
        # Implicit: if setup_method ran, no exception was raised.
        assert isinstance(self.violations, list)

    def test_surviving_violation_count(self):
        # From the fixture comments:
        # null/string/int results → skipped; DROP_ME (none) → dropped
        # STR_MSG, BAD_LOC, MISSING_PL, UNKNOWN_LEVEL, VALID_001 → 5 survive
        assert len(self.violations) == 5, (
            f"Expected 5 surviving violations, got {len(self.violations)}: "
            f"{[v.rule for v in self.violations]}"
        )

    def test_none_level_not_in_survivors(self):
        for v in self.violations:
            assert get_sarif_level(v.raw) != "none"

    def test_unknown_level_defaulted_to_warning(self):
        ul = next((v for v in self.violations if v.rule == "UNKNOWN_LEVEL"), None)
        assert ul is not None, "UNKNOWN_LEVEL violation should survive"
        assert get_sarif_level(ul.raw) == "warning"

    def test_string_message_parsed(self):
        sm = next((v for v in self.violations if v.rule == "STR_MSG"), None)
        assert sm is not None
        assert sm.message == "direct string message"

    def test_bad_locations_path_is_none(self):
        # BAD_LOC has locations="not-a-list" → path must be None
        bl = next((v for v in self.violations if v.rule == "BAD_LOC"), None)
        assert bl is not None
        assert bl.path is None

    def test_missing_physical_location_path_is_none(self):
        mp = next((v for v in self.violations if v.rule == "MISSING_PL"), None)
        assert mp is not None
        assert mp.path is None

    def test_valid_result_survives_malformed_siblings(self):
        v = next((v for v in self.violations if v.rule == "VALID_001"), None)
        assert v is not None
        assert v.path == "infra/bad.tf"

    def test_compute_result_on_malformed_does_not_raise(self):
        result = compute_result(self.violations, "any-policy", "broken-engine")
        assert result.verdict is Verdict.failed


# ===========================================================================
# 4. Degenerate inputs — fully invalid sarif_json types (CONTRACTS §5)
# ===========================================================================

class TestDegenerateInputs:
    """parse_sarif must return [] (not raise) for any non-dict or structurally
    invalid input — CONTRACTS §5, NFR-2."""

    @pytest.mark.parametrize("bad_input,label", [
        (None, "None"),
        ([], "empty-list"),
        ("not a dict", "string"),
        ({}, "empty-dict"),
        ({"runs": "not a list"}, "runs-not-a-list"),
        ({"runs": [{"tool": {}, "results": None}]}, "results-none"),
        (42, "integer"),
    ])
    def test_no_raise_returns_empty(self, bad_input, label):
        # CONTRACTS §5: "Any non-dict value is treated as malformed → returns []"
        result = parse_sarif(bad_input, "x")  # type: ignore[arg-type]
        assert result == [], f"Expected [] for {label}, got {result}"


# ===========================================================================
# 5. defaultConfiguration level fallback (CONTRACTS §5 / FR-ENGINE-3b)
# ===========================================================================

class TestDefaultConfigurationFallback:
    """Level resolution priority: result.level → defaultConfiguration.level → 'warning'."""

    def test_absent_level_uses_defaultconfiguration(self):
        viols = parse_sarif(_DEFAULT_CONFIG_FALLBACK, "fallback-test")
        assert len(viols) == 1
        assert get_sarif_level(viols[0].raw) == "error"

    def test_absent_level_absent_rule_defaults_to_warning(self):
        # No rule in rule_map and no level field → must default to "warning"
        sarif = {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "t", "rules": []}},
                    "results": [
                        {
                            "ruleId": "NO_RULE",
                            # No "level" key
                            "message": {"text": "defaults to warning"},
                        }
                    ],
                }
            ],
        }
        viols = parse_sarif(sarif, "t")
        assert len(viols) == 1
        assert get_sarif_level(viols[0].raw) == "warning"


# ===========================================================================
# 6. level="none" dropping (CONTRACTS §4)
# ===========================================================================

class TestNoneLevelDropping:
    """CONTRACTS §4: 'SARIF level=none → drop; never becomes a Violation'."""

    def test_none_only_produces_zero_violations(self):
        viols = parse_sarif(_NONE_LEVEL_ONLY, "none-test")
        assert viols == []

    def test_none_among_valid_drops_none_keeps_rest(self):
        sarif = {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "t", "rules": []}},
                    "results": [
                        {"ruleId": "KEEP", "level": "warning", "message": {"text": "keep"}},
                        {"ruleId": "DROP", "level": "none", "message": {"text": "drop"}},
                    ],
                }
            ],
        }
        viols = parse_sarif(sarif, "t")
        assert len(viols) == 1
        assert viols[0].rule == "KEEP"


# ===========================================================================
# 7. compute_result verdict + min_level semantics (CONTRACTS §4 §5)
# ===========================================================================

class TestComputeResult:
    """compute_result: empty → pass; non-empty above threshold → fail."""

    def test_empty_violations_yields_pass(self):
        result = compute_result([], "policy", "engine")
        assert result.verdict is Verdict.passed
        assert result.violations == []

    def test_non_empty_above_threshold_yields_fail(self):
        viols = [Violation(message="bad", rule="R1", raw={"level": "warning"})]
        result = compute_result(viols, "policy", "engine", min_level="warning")
        assert result.verdict is Verdict.failed
        assert len(result.violations) == 1

    def test_below_threshold_yields_pass(self):
        # note violation below warning threshold
        viols = [Violation(message="note", rule="R1", raw={"level": "note"})]
        result = compute_result(viols, "policy", "engine", min_level="warning")
        assert result.verdict is Verdict.passed
        assert result.violations == []

    def test_unknown_min_level_falls_back_to_warning(self):
        # CONTRACTS §5: "Unknown values fall back to 'warning' with a warning log"
        viols = [Violation(message="err", rule="R1", raw={"level": "error"})]
        result = compute_result(viols, "p", "e", min_level="critical")
        # "critical" is unknown → treated as "warning" → error >= warning → fail
        assert result.verdict is Verdict.failed

    def test_non_list_violations_treated_as_empty(self):
        # Defensive: non-list input should not raise
        result = compute_result("not-a-list", "p", "e")  # type: ignore[arg-type]
        assert result.verdict is Verdict.passed

    def test_engine_and_policy_propagated(self):
        result = compute_result([], "my-policy", "my-engine")
        assert result.engine == "my-engine"
        assert result.policy == "my-policy"

    def test_verdict_enum_type(self):
        result = compute_result([], "p", "e")
        assert isinstance(result.verdict, Verdict)


# ===========================================================================
# 8. Determinism (NFR-1, CONTRACTS §7)
# ===========================================================================

class TestDeterminism:
    """Two calls on the same input must produce identical to_dict() output."""

    def test_parse_sarif_deterministic(self):
        viols1 = parse_sarif(_INLINE_TWO_RUN, "inline-test")
        viols2 = parse_sarif(_INLINE_TWO_RUN, "inline-test")
        assert [v.to_dict() for v in viols1] == [v.to_dict() for v in viols2]

    def test_compute_result_deterministic(self):
        # FR-VALIDATE-4 / NFR-1: identical inputs → identical output
        viols = parse_sarif(_INLINE_TWO_RUN, "inline-test")
        r1 = compute_result(viols, "p", "e", min_level="note")
        r2 = compute_result(viols, "p", "e", min_level="note")
        assert r1.to_dict() == r2.to_dict()

    def test_checkov_fixture_deterministic(self):
        sarif = _load("checkov_sample.json")
        v1 = compute_result(parse_sarif(sarif, "checkov"), "p", "checkov")
        v2 = compute_result(parse_sarif(sarif, "checkov"), "p", "checkov")
        assert v1.to_dict() == v2.to_dict()


# ===========================================================================
# 9. get_sarif_level helper (CONTRACTS §5)
# ===========================================================================

class TestGetSarifLevel:
    """get_sarif_level: trivial lookup after parse_sarif embeds the level."""

    @pytest.mark.parametrize("level", ["error", "warning", "note"])
    def test_known_levels(self, level):
        raw = {"level": level}
        assert get_sarif_level(raw) == level

    def test_non_dict_raw_defaults_to_warning(self):
        assert get_sarif_level(None) == "warning"
        assert get_sarif_level("string") == "warning"
        assert get_sarif_level(42) == "warning"

    def test_missing_level_key_defaults_to_warning(self):
        assert get_sarif_level({}) == "warning"

    def test_unknown_level_value_defaults_to_warning(self):
        assert get_sarif_level({"level": "critical"}) == "warning"
        assert get_sarif_level({"level": "none"}) == "warning"

    def test_level_embedded_by_parse_sarif(self):
        # After parse_sarif, raw["level"] should equal the resolved level.
        viols = parse_sarif(_INLINE_TWO_RUN, "inline-test")
        err_viol = next(v for v in viols if v.rule == "RULE_ERR")
        note_viol = next(v for v in viols if v.rule == "RULE_NOTE")
        assert get_sarif_level(err_viol.raw) == "error"
        assert get_sarif_level(note_viol.raw) == "note"


# ===========================================================================
# 10. raw is shallow copy + remediation from helpUri (CONTRACTS §5)
# ===========================================================================

class TestRawAndRemediation:
    """CONTRACTS §5: Violation.raw is a SHALLOW COPY with an injected level key.
    Do NOT assert 'raw is original'; DO assert raw != original and has level."""

    def test_raw_is_not_original_result(self):
        # We build a result dict, parse it, then check raw is a copy not the original.
        result_dict = {
            "ruleId": "TEST",
            "level": "error",
            "message": {"text": "test"},
        }
        sarif = {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "t", "rules": []}},
                    "results": [result_dict],
                }
            ],
        }
        viols = parse_sarif(sarif, "t")
        assert len(viols) == 1
        # CONTRACTS §5 note: raw is a SHALLOW COPY — do not assert 'raw is result_dict'
        assert isinstance(viols[0].raw, dict)
        assert viols[0].raw is not result_dict  # it IS a new object (shallow copy)
        assert "level" in viols[0].raw

    def test_remediation_from_helpuri_top_level(self):
        # helpUri directly on rule → remediation set
        viols = parse_sarif(_INLINE_TWO_RUN, "inline-test")
        err_viol = next(v for v in viols if v.rule == "RULE_ERR")
        assert err_viol.remediation == "https://example.com/RULE_ERR"

    def test_remediation_from_help_object_uri(self):
        # help.uri (nested object) also sets remediation — checkov_sample uses this
        sarif = _load("checkov_sample.json")
        viols = parse_sarif(sarif, "checkov")
        aws20 = next(v for v in viols if v.rule == "CKV_AWS_20")
        assert aws20.remediation is not None
        assert "bridgecrew" in aws20.remediation  # from help.uri in fixture

    def test_remediation_none_when_no_help(self):
        # RULE_NOTE in inline blob has no helpUri → remediation should be None
        viols = parse_sarif(_INLINE_TWO_RUN, "inline-test")
        note_viol = next(v for v in viols if v.rule == "RULE_NOTE")
        assert note_viol.remediation is None


# ===========================================================================
# 11. Multi-run SARIF (CONTRACTS §5)
# ===========================================================================

class TestMultiRun:
    """Violations are gathered from ALL runs in a multi-run SARIF document."""

    def test_violations_from_all_runs(self):
        viols = parse_sarif(_MULTI_RUN, "multi-engine")
        assert len(viols) == 2
        rules = {v.rule for v in viols}
        assert "R1" in rules
        assert "R2" in rules

    def test_empty_run_contributes_nothing(self):
        # _INLINE_TWO_RUN has run[1] with empty results — total is still 2
        viols = parse_sarif(_INLINE_TWO_RUN, "inline-test")
        assert len(viols) == 2
