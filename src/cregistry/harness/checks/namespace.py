"""VH-NAMESPACE — namespacing & precedence (VH-NAMESPACE-1, VH-NAMESPACE-2).

VH-NAMESPACE-1: colliding ids from different sources are disambiguated by
namespace (FR-NAMESPACE-1).
VH-NAMESPACE-2: the default precedence policy — a hard constraint outranks a
weaker one, a lower-precedence source cannot relax a higher-precedence one, and
unresolvable conflicts are reported as errors (FR-NAMESPACE-2/3).
"""

from __future__ import annotations

from collections import defaultdict

from ...config import RegistryConfig, SourceConfig
from ...importer import import_sources
from ..result import CheckResult

SECTION = "VH-NAMESPACE"


def _scenario_config(base_dir, name: str, up_prec: int, down_prec: int) -> RegistryConfig:
    scen = base_dir / "scenarios" / name
    return RegistryConfig(
        sources=[
            SourceConfig(name="upstream", path=str(scen / "upstream"), precedence=up_prec),
            SourceConfig(name="downstream", path=str(scen / "downstream"), precedence=down_prec),
        ]
    )


def _collision(config: RegistryConfig) -> CheckResult:
    report = import_sources(config)
    constraints = report.bundle.constraints

    # Every effective id must be source-namespaced (FR-NAMESPACE-1, FR-SOURCE-4).
    bad_namespacing = [
        c.effective_id for c in constraints if c.effective_id != f"{c.source}/{c.constraint.id}"
    ]

    # Find a bare id shared by >1 source and prove both coexist.
    by_bare: dict[str, list[str]] = defaultdict(list)
    for c in constraints:
        by_bare[c.constraint.id].append(c.effective_id)
    colliding = {bare: eids for bare, eids in by_bare.items() if len({e.split("/", 1)[0] for e in eids}) > 1}

    effective_ids = {c.effective_id for c in constraints}
    unique = len(effective_ids) == len(constraints)

    if not bad_namespacing and unique and colliding:
        return CheckResult.ok(
            SECTION,
            "VH-NAMESPACE-1",
            f"colliding bare id(s) coexist under distinct namespaces: {colliding}",
        )
    return CheckResult.fail(
        SECTION,
        "VH-NAMESPACE-1",
        "namespacing/collision check failed",
        details=[
            {
                "bad_namespacing": bad_namespacing,
                "effective_ids_unique": unique,
                "colliding": colliding,
            }
        ],
    )


def _precedence(config: RegistryConfig) -> CheckResult:
    # (a) Positive: downstream strengthens to hard -> hard outranks, no conflict.
    ok_report = import_sources(_scenario_config(config.base_dir, "precedence-ok", 100, 50))
    hard_wins = any(
        r["winner"] == "downstream/team.s3-rule" and "hard-outranks" in r["reason"]
        for r in ok_report.bundle.precedence
    )
    positive_ok = ok_report.ok and hard_wins

    # (b) Negative: downstream relaxes an upstream hard rule -> unresolvable error.
    relax_report = import_sources(_scenario_config(config.base_dir, "precedence-relax", 100, 50))
    relax_conflict = next(
        (c for c in relax_report.conflicts if c.kind == "illegal-relaxation"), None
    )
    negative_ok = (not relax_report.ok) and relax_conflict is not None and set(
        relax_conflict.constraints
    ) == {"upstream/base.s3-hard", "downstream/team.s3-soft"}

    # (c) Guard: broadly-scoped constraints that merely overlap (not identical
    # scope) must NOT be flagged as a relaxation conflict (FR-NAMESPACE-2).
    nf_dir = config.base_dir / "scenarios" / "no-false-conflict"
    nf_cfg = RegistryConfig(
        sources=[
            SourceConfig(name="security", path=str(nf_dir / "security"), precedence=100),
            SourceConfig(name="org", path=str(nf_dir / "org"), precedence=50),
        ]
    )
    nofalse = import_sources(nf_cfg)
    no_false_positive = nofalse.ok and not nofalse.conflicts and len(nofalse.bundle.constraints) == 2

    if positive_ok and negative_ok and no_false_positive:
        return CheckResult.ok(
            SECTION,
            "VH-NAMESPACE-2",
            "hard outranks weaker; illegal same-scope relaxation errors; unrelated overlap does not",
            details=[
                {"precedence_records": ok_report.bundle.precedence},
                {"conflict": relax_conflict.to_dict()},
            ],
        )
    return CheckResult.fail(
        SECTION,
        "VH-NAMESPACE-2",
        "precedence policy check failed",
        details=[
            {
                "positive_ok": positive_ok,
                "hard_wins": hard_wins,
                "ok_records": ok_report.bundle.precedence,
                "negative_ok": negative_ok,
                "relax_conflicts": [c.to_dict() for c in relax_report.conflicts],
                "no_false_positive": no_false_positive,
                "nofalse_conflicts": [c.to_dict() for c in nofalse.conflicts],
            }
        ],
    )


def run(config: RegistryConfig) -> list[CheckResult]:
    return [_collision(config), _precedence(config)]
