"""Scope semantics (FR-CONSTRAINT-1 scope, FR-QUERY-2, FR-NAMESPACE-2).

Two distinct operations live here:

* ``scopes_overlap(a, b)`` — do two *constraint* scopes intersect (could both
  apply to one artifact)? Used for precedence/conflict detection (FR-NAMESPACE-2).
* ``scope_matches(constraint_scope, query)`` — does a *query* scope select a
  constraint? Used by get_constraints/validate (FR-QUERY-1/2). Added in Inc 7.

Both honour every selector dimension in FR-CONSTRAINT-1, including
relationship-style selectors.
"""

from __future__ import annotations

from .model import Endpoint, Relationship, Scope

_ENV_WILDCARD = {"all"}
_WILDCARD_DIMS = {"environments"}


def _dim_overlap(a: list[str], b: list[str], wildcards: set[str] = frozenset()) -> bool:
    """A single attribute dimension overlaps when either side is unconstrained
    (empty = wildcard), either side names a wildcard token, or they share a value."""
    if not a or not b:
        return True
    sa, sb = set(a), set(b)
    if (wildcards & sa) or (wildcards & sb):
        return True
    return bool(sa & sb)


def _scalar_compat(a, b) -> bool:
    """None on either side is a wildcard; otherwise must be equal."""
    return a is None or b is None or a == b


def _endpoint_overlap(a: Endpoint | None, b: Endpoint | None) -> bool:
    if a is None or b is None:
        return True
    return (
        _scalar_compat(a.layer, b.layer)
        and _scalar_compat(a.component, b.component)
        and _scalar_compat(a.domain, b.domain)
        and _scalar_compat(a.different_domain, b.different_domain)
    )


def _relationship_overlap(a: Relationship, b: Relationship) -> bool:
    return (
        _endpoint_overlap(a.source, b.source)
        and _endpoint_overlap(a.target, b.target)
        and _scalar_compat(a.interaction, b.interaction)
        and _scalar_compat(a.boundary, b.boundary)
    )


def scopes_equal(a: Scope, b: Scope) -> bool:
    """True if two scopes target exactly the same selector set (FR-NAMESPACE-2).

    Used for precedence/conflict detection: two constraints "target the same
    scope" only when their selectors are equal, not merely overlapping. This
    avoids false-positive relaxation conflicts between unrelated, broadly-scoped
    constraints (e.g. a provider-only rule vs a repo-tag-only rule)."""
    return (
        set(a.providers) == set(b.providers)
        and set(a.resource_types) == set(b.resource_types)
        and set(a.environments) == set(b.environments)
        and set(a.repos) == set(b.repos)
        and a.relationship == b.relationship
    )


def scopes_overlap(a: Scope, b: Scope) -> bool:
    """True if some artifact could fall in both scopes (FR-NAMESPACE-2).

    A relationship-scoped constraint and an attribute-only constraint are treated
    as targeting different kinds of scope and never overlap.
    """
    a_rel, b_rel = a.relationship is not None, b.relationship is not None
    if a_rel != b_rel:
        return False
    if a_rel and b_rel:
        if not _relationship_overlap(a.relationship, b.relationship):  # type: ignore[arg-type]
            return False

    return (
        _dim_overlap(a.providers, b.providers)
        and _dim_overlap(a.resource_types, b.resource_types)
        and _dim_overlap(a.environments, b.environments, _ENV_WILDCARD)
        and _dim_overlap(a.repos, b.repos)
    )


# --- Query-scope matching (FR-QUERY-1/2): does a query select a constraint? -------


def _dim_match(constraint_vals: list[str], query_vals: list[str], wildcards: set[str] = frozenset()) -> bool:
    """A constraint dimension is satisfied by a query unless the query *supplies*
    a value that the constraint excludes.

    - constraint unrestricted on this dim -> matches (applies broadly)
    - constraint declares a wildcard token (e.g. environments ``all``) -> matches
    - query silent on this dim -> matches ("don't care"; the query simply did not
      narrow on it). This is what lets an agent ask about ``{providers:[aws],
      resource_types:[aws_s3_bucket]}`` and still get the data-plane S3
      constraints without having to guess the exact repo tag.
    - query supplies values -> require an intersection (else the query's value
      excludes the constraint).

    NFR-3 still holds: supplying a discriminating selector narrows the result set,
    and a non-matching value (e.g. ``providers:[gcp]``) excludes a constraint."""
    if not constraint_vals:
        return True
    if wildcards & set(constraint_vals):
        return True
    if not query_vals:
        return True
    return bool(set(constraint_vals) & set(query_vals))


def _endpoint_match(c_ep: Endpoint | None, q_ep: Endpoint | None) -> bool:
    if c_ep is None:
        return True
    if q_ep is None:
        return False
    return (
        (c_ep.layer is None or c_ep.layer == q_ep.layer)
        and (c_ep.component is None or c_ep.component == q_ep.component)
        and (c_ep.domain is None or c_ep.domain == q_ep.domain)
        and (c_ep.different_domain is None or c_ep.different_domain == q_ep.different_domain)
    )


def _relationship_match(c_rel: Relationship, q_rel: Relationship | None) -> bool:
    if q_rel is None:
        return False
    return (
        _endpoint_match(c_rel.source, q_rel.source)
        and _endpoint_match(c_rel.target, q_rel.target)
        and (c_rel.interaction is None or c_rel.interaction == q_rel.interaction)
        and (c_rel.boundary is None or c_rel.boundary == q_rel.boundary)
    )


def scope_matches(constraint_scope: Scope, query: Scope) -> bool:
    """True if a constraint is relevant to a query scope (FR-QUERY-1/2).

    Honours every selector dimension, including relationship-style selectors: a
    relationship-scoped constraint is only selected by a query that supplies a
    matching relationship."""
    if constraint_scope.relationship is not None:
        if not _relationship_match(constraint_scope.relationship, query.relationship):
            return False

    return (
        _dim_match(constraint_scope.providers, query.providers)
        and _dim_match(constraint_scope.resource_types, query.resource_types)
        and _dim_match(constraint_scope.environments, query.environments, _ENV_WILDCARD)
        and _dim_match(constraint_scope.repos, query.repos)
    )
