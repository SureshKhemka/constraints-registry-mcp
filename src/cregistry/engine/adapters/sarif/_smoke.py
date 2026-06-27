"""Inline sanity checks for parse_sarif + compute_result.

Run from the repo root:
    uv run python src/cregistry/engine/adapters/sarif/_smoke.py

Or as a one-liner (repo root, uv-managed venv):
    uv run python -c "import sys; sys.path.insert(0, 'src'); \
        from cregistry.engine.adapters.sarif._smoke import run_all; run_all()"

These checks cover the three bundled fixtures and a hand-written inline blob.
They are NOT a replacement for the eval agent's full test suite in tests/.
"""

from __future__ import annotations

import json
import pathlib

_FIXTURE_DIR = pathlib.Path(__file__).parent / "_fixtures"


def _load(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text())


def run_all() -> None:
    # Import here so the file is importable even before cregistry is installed
    # (smoke is run from inside the src/ tree via uv run).
    from cregistry.engine.adapters.sarif import compute_result, get_sarif_level, parse_sarif
    from cregistry.engine.interface import Verdict

    # ------------------------------------------------------------------
    # Inline blob: 1 error result + 1 note result + 1 empty run
    # ------------------------------------------------------------------
    inline = {
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
                "results": [],  # empty run — should contribute no violations
            },
        ],
    }

    viols = parse_sarif(inline, "inline-test")
    assert len(viols) == 2, f"inline: expected 2 violations, got {len(viols)}"
    assert all(v.raw is not None for v in viols), "inline: raw must be set"

    levels = {v.rule: get_sarif_level(v.raw) for v in viols}
    assert levels.get("RULE_ERR") == "error", f"inline: RULE_ERR level wrong: {levels}"
    assert levels.get("RULE_NOTE") == "note", f"inline: RULE_NOTE level wrong: {levels}"

    # remediation from helpUri (RULE_ERR has helpUri)
    err_viol = next(v for v in viols if v.rule == "RULE_ERR")
    assert err_viol.remediation == "https://example.com/RULE_ERR", (
        f"inline: remediation not set from helpUri: {err_viol.remediation}"
    )

    # min_level=warning → only error kept (note dropped)
    result_w = compute_result(viols, "some/policy.rego", "inline-test", min_level="warning")
    assert result_w.verdict == Verdict.failed, f"inline/warning: expected fail, got {result_w.verdict}"
    assert len(result_w.violations) == 1, (
        f"inline/warning: expected 1 violation, got {len(result_w.violations)}"
    )
    assert result_w.violations[0].rule == "RULE_ERR"

    # min_level=note → both kept
    result_n = compute_result(viols, "some/policy.rego", "inline-test", min_level="note")
    assert result_n.verdict == Verdict.failed, f"inline/note: expected fail"
    assert len(result_n.violations) == 2, (
        f"inline/note: expected 2 violations, got {len(result_n.violations)}"
    )

    # min_level=error → only error kept (same as warning here)
    result_e = compute_result(viols, "some/policy.rego", "inline-test", min_level="error")
    assert result_e.verdict == Verdict.failed
    assert len(result_e.violations) == 1

    # No violations at all → passed
    result_pass = compute_result([], "some/policy.rego", "inline-test")
    assert result_pass.verdict == Verdict.passed, f"empty: expected pass, got {result_pass.verdict}"

    print("[PASS] inline blob checks")

    # ------------------------------------------------------------------
    # Checkov fixture
    # Three results: CKV_AWS_20=error, CKV_AWS_18=warning (defaultConfig
    # fallback), CKV_AWS_21=note (defaultConfig fallback).
    # ------------------------------------------------------------------
    checkov = _load("checkov_sample.json")
    cv = parse_sarif(checkov, "checkov")
    assert len(cv) == 3, f"checkov: expected 3 violations, got {len(cv)}"

    cv_levels = {v.rule: get_sarif_level(v.raw) for v in cv}
    assert cv_levels["CKV_AWS_20"] == "error", f"checkov: CKV_AWS_20 level wrong: {cv_levels}"
    assert cv_levels["CKV_AWS_18"] == "warning", f"checkov: CKV_AWS_18 should fall back to warning"
    assert cv_levels["CKV_AWS_21"] == "note", f"checkov: CKV_AWS_21 should fall back to note"

    # paths populated
    assert all(v.path == "infra/main.tf" for v in cv), "checkov: path extraction failed"

    # remediation populated from rule help.uri
    for v in cv:
        if v.rule in ("CKV_AWS_20", "CKV_AWS_18"):
            assert v.remediation is not None, f"checkov: {v.rule} remediation should be set"

    # min_level=warning → error + warning (2 violations)
    cr_w = compute_result(cv, "checkov-policy.yaml", "checkov", min_level="warning")
    assert cr_w.verdict == Verdict.failed
    assert len(cr_w.violations) == 2, f"checkov/warning: expected 2, got {len(cr_w.violations)}"

    # min_level=error → only CKV_AWS_20 (1 violation)
    cr_e = compute_result(cv, "checkov-policy.yaml", "checkov", min_level="error")
    assert len(cr_e.violations) == 1
    assert cr_e.violations[0].rule == "CKV_AWS_20"

    # determinism: sort is (rule or "", path or "", message)
    assert cr_w.violations[0].rule == "CKV_AWS_18", (
        f"checkov: sort order wrong: {[v.rule for v in cr_w.violations]}"
    )

    print("[PASS] checkov fixture checks")

    # ------------------------------------------------------------------
    # Semgrep fixture
    # 3 results: exec-detected=error, useless-ifmain=warning, and one
    # level=none (must be dropped).
    # ------------------------------------------------------------------
    semgrep = _load("semgrep_sample.json")
    sv = parse_sarif(semgrep, "semgrep")
    assert len(sv) == 2, f"semgrep: expected 2 violations (none dropped), got {len(sv)}"

    s_levels = [get_sarif_level(v.raw) for v in sv]
    assert "none" not in s_levels, f"semgrep: level=none result was not dropped"
    assert "error" in s_levels
    assert "warning" in s_levels

    # min_level=error → only exec-detected kept
    sr_e = compute_result(sv, "semgrep-rules.yml", "semgrep", min_level="error")
    assert len(sr_e.violations) == 1
    assert "exec" in sr_e.violations[0].rule

    # min_level=warning → both kept (default)
    sr_w = compute_result(sv, "semgrep-rules.yml", "semgrep")
    assert len(sr_w.violations) == 2

    print("[PASS] semgrep fixture checks")

    # ------------------------------------------------------------------
    # Malformed fixture — must not raise; recoverable results survive
    # ------------------------------------------------------------------
    malformed = _load("malformed_sample.json")
    mv = parse_sarif(malformed, "broken-engine")

    # null / string / int results are skipped; level=none is dropped.
    # Surviving: STR_MSG(error), BAD_LOC(error), MISSING_PL(warning),
    #            UNKNOWN_LEVEL→warning, VALID_001(error) = 5 violations.
    assert len(mv) == 5, f"malformed: expected 5 surviving violations, got {len(mv)}"

    # none of the levels should be "none"
    for v in mv:
        lv = get_sarif_level(v.raw)
        assert lv != "none", f"malformed: level=none survived in {v.rule}"

    # UNKNOWN_LEVEL → defaulted to warning
    ul = next(v for v in mv if v.rule == "UNKNOWN_LEVEL")
    assert get_sarif_level(ul.raw) == "warning", f"malformed: UNKNOWN_LEVEL should be warning"

    # STR_MSG has a string message, not a dict
    sm = next(v for v in mv if v.rule == "STR_MSG")
    assert sm.message == "direct string message", f"malformed: STR_MSG message wrong: {sm.message!r}"

    # BAD_LOC has a non-list locations — path must be None
    bl = next(v for v in mv if v.rule == "BAD_LOC")
    assert bl.path is None, f"malformed: BAD_LOC path should be None, got {bl.path!r}"

    # compute_result must not raise on malformed violations list
    mr = compute_result(mv, "any-policy", "broken-engine")
    assert mr.verdict == Verdict.failed

    # Non-list violations input → no crash, treated as empty
    mr2 = compute_result("not-a-list", "any-policy", "broken-engine")  # type: ignore[arg-type]
    assert mr2.verdict == Verdict.passed

    print("[PASS] malformed fixture checks")

    # ------------------------------------------------------------------
    # Fully invalid sarif_json types → empty list, no raise
    # ------------------------------------------------------------------
    assert parse_sarif(None, "x") == []  # type: ignore[arg-type]
    assert parse_sarif([], "x") == []  # type: ignore[arg-type]
    assert parse_sarif("not a dict", "x") == []  # type: ignore[arg-type]
    assert parse_sarif({}, "x") == []
    assert parse_sarif({"runs": "not a list"}, "x") == []

    print("[PASS] degenerate input checks")
    print("\nAll smoke checks passed.")


if __name__ == "__main__":
    run_all()
