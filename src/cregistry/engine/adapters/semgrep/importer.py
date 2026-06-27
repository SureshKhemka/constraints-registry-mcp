"""Semgrep catalog importer (CONTRACTS §6).

Turns a Semgrep YAML ruleset into registry constraint stubs.  Each stub is a
``dict`` that round-trips through ``Constraint.model_validate`` (minus the two
human-TODO placeholder fields).  A parallel provenance ``dict`` carries the
license and source metadata that the ``Constraint`` schema (``extra="forbid"``)
cannot hold.

Public API::

    stubs = import_catalog(source_ref)                        # skip unknown-license rules
    stubs = import_catalog(source_ref, allow_unknown_license=True)

Each element of the returned list is a ``(constraint_stub: dict, provenance: dict)``
namedtuple.

License policy (CONTRACTS §6):
- If a rule carries ``metadata.license`` it is used verbatim.
- If no license is found the provenance records ``license: null`` (unknown).
- Rules with unknown license are **skipped by default**.  Pass
  ``allow_unknown_license=True`` to include them (e.g., for audit/review purposes).
  Never bundle Semgrep's proprietary registry rules into this repo.

Scope derivation (best-effort):
- ``languages`` list → ``scope.resource_types``.
- ``paths.include`` / ``paths.exclude`` are not mapped to ``Scope`` (no field for
  them); they are stored in provenance for human reference.

Category: always ``"architectural"`` (best-effort per CONTRACTS §6 for Semgrep).
Severity: always ``"soft"`` (conservative; never ``"hard"`` on import).
"""

from __future__ import annotations

import re
import datetime
from pathlib import Path
from typing import NamedTuple

import yaml  # PyYAML, already in project deps

from ....model import Constraint, EnforcementBinding, Guidance, Scope


__all__ = ["import_catalog", "CatalogStub"]


class CatalogStub(NamedTuple):
    """A constraint stub together with its out-of-schema provenance metadata."""

    constraint: dict
    """A dict that round-trips through ``Constraint.model_validate``."""

    provenance: dict
    """Sidecar carrying license, source, and import metadata (CONTRACTS §6).
    NOT inside the constraint dict — ``Constraint`` has ``extra="forbid"``."""


def import_catalog(
    source_ref: str,
    *,
    allow_unknown_license: bool = False,
) -> list[CatalogStub]:
    """Parse a Semgrep ruleset YAML and return constraint stubs.

    Parameters
    ----------
    source_ref:
        Absolute or relative path to a Semgrep ``*.yaml`` / ``*.yml`` ruleset.
        Only local files are supported; fetching from the Semgrep registry
        requires the caller to download the YAML first (see README for guidance
        on license compliance when doing so).
    allow_unknown_license:
        If ``False`` (default), rules whose license cannot be determined are
        silently skipped and not included in the returned list.  If ``True``,
        they are included with ``provenance["license"] = null``.

    Returns
    -------
    list[CatalogStub]
        One ``CatalogStub`` per imported rule.  Skipped rules (unknown license
        when ``allow_unknown_license=False``) are absent from this list.

    Raises
    ------
    FileNotFoundError
        If *source_ref* does not exist on disk.
    ValueError
        If the YAML cannot be parsed or has no ``rules`` list.
    """
    path = Path(source_ref)
    if not path.exists():
        raise FileNotFoundError(f"Semgrep ruleset not found: {source_ref!r}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(f"Ruleset YAML must be a mapping, got {type(raw).__name__}: {source_ref!r}")

    rules = raw.get("rules")
    if not isinstance(rules, list):
        raise ValueError(f"Ruleset has no 'rules' list: {source_ref!r}")

    # Top-level license field (some rulesets carry it here rather than per-rule).
    toplevel_license: str | None = (
        raw.get("metadata", {}).get("license")
        if isinstance(raw.get("metadata"), dict)
        else raw.get("license")
    )
    imported_at = datetime.datetime.utcnow().isoformat() + "Z"

    stubs: list[CatalogStub] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        stub = _rule_to_stub(rule, source_ref, toplevel_license, imported_at)
        if stub is None:
            continue  # malformed rule

        license_val = stub.provenance.get("license")
        if license_val is None and not allow_unknown_license:
            # Unknown license — skip per policy.
            continue

        stubs.append(stub)

    return stubs


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rule_to_stub(
    rule: dict,
    source_ref: str,
    toplevel_license: str | None,
    imported_at: str,
) -> CatalogStub | None:
    """Convert a single rule dict into a ``CatalogStub``.

    Returns ``None`` if the rule lacks a required ``id`` field.
    """
    rule_id: str | None = rule.get("id")
    if not rule_id or not isinstance(rule_id, str):
        return None

    meta: dict = rule.get("metadata") or {}

    # ---- license (REQUIRED provenance field for Semgrep imports) ----
    license_val: str | None = (
        meta.get("license")
        or (toplevel_license if toplevel_license else None)
    )

    # ---- title ----
    # Semgrep rules have a ``message`` field (the human-readable finding text).
    # Use it as title; fall back to the rule id.
    message: str = rule.get("message", "").strip()
    title: str = (message[:120] if message else rule_id)

    # ---- languages → scope.resource_types ----
    languages: list[str] = rule.get("languages") or []
    if not isinstance(languages, list):
        languages = [str(languages)]
    resource_types = [str(lang) for lang in languages if lang]

    # ---- guidance.dont: use the short message (or rule id) ----
    dont_entry = message if message else rule_id

    # ---- constraint stub dict ----
    constraint_stub: dict = {
        "id": f"semgrep/{_slugify(rule_id)}",
        "title": title or rule_id,
        "intent": "TODO: human",
        "category": "architectural",
        "scope": {
            "resource_types": resource_types,
        },
        "severity": "soft",
        "enforcement": [
            {
                "engine": "semgrep",
                "policy": source_ref,
            }
        ],
        "guidance": {
            "dont": [dont_entry],
            "example_compliant": "TODO: human",
        },
        "owner": "imported",
        "version": "0.1.0",
    }

    # Validate the stub round-trips through Constraint (minus TODO placeholders).
    # This catches schema mismatches at import time rather than at run time.
    try:
        Constraint.model_validate(constraint_stub)
    except Exception as exc:
        # Defensive: if validation fails log and skip rather than propagate.
        import logging
        logging.getLogger(__name__).warning(
            "import_catalog: stub for rule %r failed model_validate: %s", rule_id, exc
        )
        return None

    # ---- provenance sidecar (NOT inside the constraint dict) ----
    provenance: dict = {
        "engine": "semgrep",
        "source": source_ref,
        "rule_id": rule_id,
        "license": license_val,      # None → unknown; importer skips by default
        "severity_hint": rule.get("severity"),
        "languages": languages,
        "paths_include": _get_paths(rule, "include"),
        "paths_exclude": _get_paths(rule, "exclude"),
        "metadata": dict(meta),
        "imported_at": imported_at,
    }

    return CatalogStub(constraint=constraint_stub, provenance=provenance)


def _slugify(text: str) -> str:
    """Lowercase, replace non-alphanumeric runs with hyphens, strip edges."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "unknown"


def _get_paths(rule: dict, key: str) -> list[str]:
    paths_block = rule.get("paths")
    if not isinstance(paths_block, dict):
        return []
    val = paths_block.get(key, [])
    if isinstance(val, list):
        return [str(v) for v in val]
    return []
