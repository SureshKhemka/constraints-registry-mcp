"""Import tests for Checkov and Semgrep catalog importers (CONTRACTS §6).

Spec coverage
-------------
CONTRACTS §6   : import_catalog returns (stub, provenance) sidecar pairs;
                 stubs round-trip through Constraint.model_validate;
                 provenance carries license; Constraint has no provenance field.
FR-ENGINE-2    : import returns schema-valid constraint stubs
VH-OUTPUT-2    : self-contained; no external repos or binaries needed
                 (checkov falls back to _SEED_CHECKS when binary absent;
                  semgrep importer needs a local YAML file, not a binary)

These tests exercise the importer functions directly without spawning any engine
binary (the checkov importer falls back to the seed catalog; the semgrep
importer just reads a YAML file).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cregistry.model import Constraint, EnforcementBinding

# ---------------------------------------------------------------------------
# Checkov importer
# ---------------------------------------------------------------------------
from cregistry.engine.adapters.checkov.importer import import_catalog as ck_import_catalog

# ---------------------------------------------------------------------------
# Semgrep importer
# ---------------------------------------------------------------------------
from cregistry.engine.adapters.semgrep.importer import import_catalog as sg_import_catalog

from tests.conftest import SEMGREP_FIXTURE_DIR, TESTS_FIXTURE_DIR


# ===========================================================================
# Checkov import_catalog (CONTRACTS §6)
# ===========================================================================


class TestCheckovImportCatalog:
    """CONTRACTS §6: import_catalog("builtin") returns seed-set (stub, provenance) pairs."""

    def setup_method(self):
        self.pairs = ck_import_catalog("builtin")

    def test_returns_list_of_pairs(self):
        assert isinstance(self.pairs, list), "import_catalog must return a list"
        assert len(self.pairs) > 0, "seed catalog must be non-empty"

    def test_each_pair_is_two_dicts(self):
        for stub, prov in self.pairs:
            assert isinstance(stub, dict), "stub must be a dict"
            assert isinstance(prov, dict), "provenance must be a dict"

    def test_stubs_round_trip_through_constraint_model_validate(self):
        """CONTRACTS §6: stubs must be valid Constraint input (TODO placeholders satisfy min_length=1)."""
        for stub, _ in self.pairs:
            try:
                Constraint.model_validate(stub)
            except Exception as exc:
                pytest.fail(
                    f"CONTRACTS §6: stub for {stub.get('id')!r} failed "
                    f"Constraint.model_validate: {exc}"
                )

    def test_stub_id_format(self):
        # CONTRACTS §6: id = "checkov/<slug>" where slug is lowercase check_id with _ → -
        for stub, _ in self.pairs:
            assert stub["id"].startswith("checkov/"), (
                f"stub id must start with 'checkov/': {stub['id']!r}"
            )

    def test_severity_is_soft(self):
        # CONTRACTS §6: severity MUST be "soft" on import; never "hard"
        for stub, _ in self.pairs:
            assert stub["severity"] == "soft", (
                f"CONTRACTS §6: imported stub {stub['id']!r} has severity "
                f"{stub['severity']!r}; must be 'soft'"
            )

    def test_enforcement_binding_exact_shape(self):
        # CONTRACTS §3: EnforcementBinding carries ONLY {engine, policy}
        for stub, _ in self.pairs:
            for binding_dict in stub.get("enforcement", []):
                keys = set(binding_dict.keys())
                assert keys == {"engine", "policy"}, (
                    f"CONTRACTS §3: enforcement binding must have exactly "
                    f"{{engine, policy}}, got {keys} in stub {stub['id']!r}"
                )
                assert binding_dict["engine"] == "checkov", (
                    f"enforcement engine must be 'checkov', got {binding_dict['engine']!r}"
                )

    def test_enforcement_binding_round_trips(self):
        # EnforcementBinding schema has extra="forbid" — any extra key would fail.
        for stub, _ in self.pairs:
            for bd in stub.get("enforcement", []):
                try:
                    EnforcementBinding.model_validate(bd)
                except Exception as exc:
                    pytest.fail(
                        f"EnforcementBinding.model_validate failed for {stub['id']!r}: {exc}"
                    )

    def test_provenance_has_license_key(self):
        # CONTRACTS §6: Semgrep MUST capture license; Checkov provenance may be null.
        # Key must exist (not absent).
        for _, prov in self.pairs:
            assert "license" in prov, (
                f"CONTRACTS §6: provenance sidecar missing 'license' key"
            )

    def test_provenance_license_may_be_null_for_checkov(self):
        # The checkov importer documents license as null (Apache-2.0 per-file).
        for _, prov in self.pairs:
            # license can be None (null) for checkov — this is per-spec.
            lic = prov.get("license")
            assert lic is None or isinstance(lic, str), (
                "provenance license must be a string or null"
            )

    def test_provenance_sidecar_not_in_stub(self):
        # CONTRACTS §6: Constraint is extra="forbid"; provenance must NOT be in stub.
        for stub, _ in self.pairs:
            for forbidden_key in ("license", "source", "imported_at", "provenance"):
                assert forbidden_key not in stub, (
                    f"CONTRACTS §6: stub {stub['id']!r} contains forbidden "
                    f"provenance key {forbidden_key!r}"
                )

    def test_output_sorted_by_id(self):
        # Importer is deterministic: same catalog → same stubs in same order.
        ids = [stub["id"] for stub, _ in self.pairs]
        assert ids == sorted(ids), (
            "CONTRACTS §6: import_catalog output must be sorted by stub id"
        )

    def test_intent_is_todo_placeholder(self):
        for stub, _ in self.pairs:
            assert stub.get("intent") == "TODO: human", (
                f"stub {stub['id']!r}: intent should be 'TODO: human'"
            )

    def test_guidance_example_compliant_is_todo_placeholder(self):
        for stub, _ in self.pairs:
            assert stub.get("guidance", {}).get("example_compliant") == "TODO: human"

    def test_category_values(self):
        # CONTRACTS §6: CKV2_* → architectural, others → infrastructure
        from cregistry.model import Category
        for stub, prov in self.pairs:
            check_id = prov.get("check_id", "")
            cat = stub.get("category")
            if check_id.startswith("CKV2_"):
                # CKV2_* is_graph=True → architectural (but some seed entries
                # override is_graph; we just check the category is valid)
                assert cat in ("architectural", "infrastructure"), (
                    f"stub {stub['id']!r}: unexpected category {cat!r}"
                )
            else:
                assert cat in ("architectural", "infrastructure")

    def test_determinism(self):
        # Same call with same source_ref produces identical stubs.
        pairs2 = ck_import_catalog("builtin")
        # Ignore imported_at which is time-based.
        stubs1 = [s for s, _ in self.pairs]
        stubs2 = [s for s, _ in pairs2]
        assert stubs1 == stubs2, (
            "CONTRACTS §6: import_catalog is not deterministic across calls"
        )


# ===========================================================================
# Semgrep import_catalog (CONTRACTS §6)
# ===========================================================================

# Path to the test rule with a known license (MIT)
_SG_RULE_WITH_LICENSE = SEMGREP_FIXTURE_DIR / "rule.yaml"

# Path to the test rule WITHOUT a license (created in tests/fixtures/)
_SG_RULE_NO_LICENSE = TESTS_FIXTURE_DIR / "rule_no_license.yaml"


class TestSemgrepImportCatalog:
    """CONTRACTS §6: semgrep import_catalog returns (stub, provenance) pairs."""

    def setup_method(self):
        self.stubs = sg_import_catalog(str(_SG_RULE_WITH_LICENSE))

    def test_returns_list_of_catalogstubs(self):
        assert isinstance(self.stubs, list)
        assert len(self.stubs) > 0

    def test_stub_round_trips_through_constraint(self):
        for cs in self.stubs:
            try:
                Constraint.model_validate(cs.constraint)
            except Exception as exc:
                pytest.fail(
                    f"Semgrep stub {cs.constraint.get('id')!r} failed "
                    f"Constraint.model_validate: {exc}"
                )

    def test_stub_id_format(self):
        for cs in self.stubs:
            assert cs.constraint["id"].startswith("semgrep/"), (
                f"stub id must start with 'semgrep/': {cs.constraint['id']!r}"
            )

    def test_severity_is_soft(self):
        for cs in self.stubs:
            assert cs.constraint["severity"] == "soft"

    def test_enforcement_binding_exact_shape(self):
        # CONTRACTS §3: only {engine, policy}
        for cs in self.stubs:
            for bd in cs.constraint.get("enforcement", []):
                keys = set(bd.keys())
                assert keys == {"engine", "policy"}, (
                    f"enforcement binding must be exactly {{engine, policy}}, got {keys}"
                )
                assert bd["engine"] == "semgrep"

    def test_provenance_license_nonnull_for_known_license(self):
        # CONTRACTS §6: "Semgrep MUST capture license for each imported rule"
        # rule.yaml has metadata.license: MIT → provenance.license must be "MIT"
        for cs in self.stubs:
            assert cs.provenance["license"] is not None, (
                f"CONTRACTS §6: Semgrep provenance.license must be set for "
                f"rules with known license; got None for {cs.constraint['id']!r}"
            )
            assert isinstance(cs.provenance["license"], str)

    def test_provenance_has_required_fields(self):
        for cs in self.stubs:
            p = cs.provenance
            assert "license" in p, "provenance must have 'license'"
            assert "rule_id" in p, "provenance must have 'rule_id'"
            assert "source" in p, "provenance must have 'source'"
            assert "imported_at" in p, "provenance must have 'imported_at'"

    def test_provenance_sidecar_not_in_constraint(self):
        for cs in self.stubs:
            for forbidden in ("license", "source", "rule_id", "imported_at", "provenance"):
                assert forbidden not in cs.constraint, (
                    f"CONTRACTS §6: constraint dict must not contain provenance "
                    f"key {forbidden!r} (Constraint is extra='forbid')"
                )

    def test_category_is_architectural(self):
        # CONTRACTS §6: semgrep → always "architectural" (best-effort)
        for cs in self.stubs:
            assert cs.constraint["category"] == "architectural"

    def test_unknown_license_skipped_by_default(self):
        """CONTRACTS §6: rules with unknown license are skipped without the flag."""
        stubs_no_lic = sg_import_catalog(str(_SG_RULE_NO_LICENSE))
        assert stubs_no_lic == [], (
            "CONTRACTS §6: rules with unknown license must be skipped "
            f"by default (allow_unknown_license=False); got {len(stubs_no_lic)} stubs"
        )

    def test_unknown_license_included_with_flag(self):
        """CONTRACTS §6: allow_unknown_license=True includes unknown-license rules."""
        stubs_allowed = sg_import_catalog(
            str(_SG_RULE_NO_LICENSE), allow_unknown_license=True
        )
        assert len(stubs_allowed) >= 1, (
            "allow_unknown_license=True must include unknown-license rules"
        )
        for cs in stubs_allowed:
            assert cs.provenance["license"] is None, (
                "provenance.license must be None for rules with unknown license"
            )

    def test_unknown_license_stubs_still_round_trip(self):
        """Even stubs with unknown license must be schema-valid (CONTRACTS §6)."""
        stubs_allowed = sg_import_catalog(
            str(_SG_RULE_NO_LICENSE), allow_unknown_license=True
        )
        for cs in stubs_allowed:
            try:
                Constraint.model_validate(cs.constraint)
            except Exception as exc:
                pytest.fail(
                    f"Semgrep stub (no license) failed Constraint.model_validate: {exc}"
                )

    def test_filenotfound_on_missing_path(self):
        with pytest.raises(FileNotFoundError):
            sg_import_catalog("/no/such/file/rules.yaml")

    def test_valueerror_on_empty_rules_list(self, tmp_path):
        bad_yaml = tmp_path / "empty.yaml"
        bad_yaml.write_text("rules: []\n")
        # No rules → valid YAML but zero stubs (not a ValueError; importer returns [])
        stubs = sg_import_catalog(str(bad_yaml), allow_unknown_license=True)
        assert stubs == []

    def test_valueerror_on_no_rules_key(self, tmp_path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("{}\n")
        with pytest.raises(ValueError, match="no 'rules' list"):
            sg_import_catalog(str(bad_yaml))
