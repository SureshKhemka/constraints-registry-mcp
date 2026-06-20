"""Namespacing precedence & conflict resolution (FR-NAMESPACE-2/3).

Default policy (the only policy in V0):

* Two constraints from *different* sources that target the **same scope**
  (identical selectors, not merely overlapping) are compared. Requiring scope
  *equality* avoids false-positive relaxation conflicts between unrelated,
  broadly-scoped constraints.
* A ``hard`` constraint outranks a non-``hard`` one (severity decides the winner).
* A downstream (lower-precedence) source MAY add/strengthen but MUST NOT relax
  (override to weaker severity) a constraint from a higher-precedence source;
  attempting to is an unresolvable conflict reported as an import error
  (FR-NAMESPACE-3).

Resolution is deterministic: inputs are sorted by effective id before pairwise
comparison (NFR-1).
"""

from __future__ import annotations

from dataclasses import dataclass

from .bundle import ImportedConstraint
from .config import RegistryConfig
from .scope import scopes_equal


@dataclass(frozen=True)
class Conflict:
    """An unresolvable precedence conflict (FR-NAMESPACE-3)."""

    kind: str
    message: str
    constraints: list[str]
    sources: list[str]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "constraints": self.constraints,
            "sources": self.sources,
        }


def _record(winner: ImportedConstraint, loser: ImportedConstraint, reason: str) -> dict:
    return {
        "winner": winner.effective_id,
        "loser": loser.effective_id,
        "winner_severity": winner.constraint.severity.value,
        "loser_severity": loser.constraint.severity.value,
        "scope_relation": "same-scope",
        "reason": reason,
    }


def resolve_precedence(
    constraints: list[ImportedConstraint], config: RegistryConfig
) -> tuple[list[dict], list[Conflict]]:
    prec_of = {s.name: s.precedence for s in config.sources}
    ordered = sorted(constraints, key=lambda c: c.effective_id)

    records: list[dict] = []
    conflicts: list[Conflict] = []

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            if a.source == b.source:
                continue
            if not scopes_equal(a.constraint.scope, b.constraint.scope):
                continue

            pa, pb = prec_of.get(a.source, 0), prec_of.get(b.source, 0)
            ra, rb = a.constraint.severity.rank, b.constraint.severity.rank

            if pa == pb:
                # Equal-precedence sources: severity alone decides (hard outranks).
                if ra == rb:
                    records.append(_record(a, b, "equal-precedence, equal-severity: coexist"))
                else:
                    hi, lo = (a, b) if ra > rb else (b, a)
                    records.append(_record(hi, lo, "hard-outranks-weaker"))
                continue

            # Distinct precedence: identify higher- (hi) and lower-precedence (lo).
            hi, lo = (a, b) if pa > pb else (b, a)
            if lo.constraint.severity.rank < hi.constraint.severity.rank:
                conflicts.append(
                    Conflict(
                        kind="illegal-relaxation",
                        message=(
                            f"downstream source {lo.source!r} relaxes "
                            f"{hi.effective_id} ({hi.constraint.severity.value}) to weaker "
                            f"{lo.constraint.severity.value} over an overlapping scope"
                        ),
                        constraints=sorted([hi.effective_id, lo.effective_id]),
                        sources=sorted([hi.source, lo.source]),
                    )
                )
            elif lo.constraint.severity.rank > hi.constraint.severity.rank:
                # Downstream strengthened; the stronger (hard) constraint wins.
                records.append(_record(lo, hi, "downstream-strengthens; hard-outranks-weaker"))
            else:
                # Equal severity: the higher-precedence source wins.
                records.append(_record(hi, lo, "higher-precedence-source"))

    records.sort(key=lambda r: (r["winner"], r["loser"]))
    conflicts.sort(key=lambda c: tuple(c.constraints))
    return records, conflicts
