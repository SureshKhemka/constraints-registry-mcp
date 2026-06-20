"""Registry service layer (FR-QUERY, FR-VALIDATE, FR-MCP-4).

Pure, transport-independent implementation of the two registry operations. The
MCP server (``mcp_server``) is a thin wrapper over this. Keeping the logic here
lets the harness exercise the exact code paths the MCP tools call.

Fail-open (FR-MCP-4): ``get_constraints`` never raises and never blocks the
caller — if the index is unavailable or the query cannot be served, it returns a
proceed-able, constraint-free result. ``validate`` MAY surface an explicit error,
since it is an active check.
"""

from __future__ import annotations

from .config import RegistryConfig
from .engine.registry import EngineRegistry
from .importer import import_sources
from .model import Scope
from .query import get_constraints as _get_constraints
from .store import BundleStore
from .validate import validate as _validate


class ValidationUnavailable(Exception):
    """Raised by validate() when no servable bundle exists (FR-MCP-4 allows
    validate to surface an explicit error, unlike get_constraints)."""


class RegistryService:
    def __init__(self, config: RegistryConfig, store: BundleStore, registry: EngineRegistry) -> None:
        self.config = config
        self.store = store
        self.registry = registry

    @classmethod
    def from_config(cls, config: RegistryConfig) -> "RegistryService":
        store = BundleStore()
        bundle = import_sources(config).bundle
        store.add(bundle)
        return cls(config, store, EngineRegistry.from_config(config))

    def _resolve_bundle(self, version: str | None):
        return self.store.get(version)

    def get_constraints(self, scope: dict | None = None, version: str | None = None) -> dict:
        # FR-MCP-4: fail open. Any failure -> empty, proceed-able result.
        try:
            bundle = self._resolve_bundle(version)
            if bundle is None:
                return {
                    "available": False,
                    "reason": "registry index unavailable; proceed without constraints",
                    "bundle_id": None,
                    "constraints": [],
                }
            sc = Scope.model_validate(scope or {})
            return {
                "available": True,
                "bundle_id": bundle.bundle_id,
                "constraints": _get_constraints(bundle, sc),
            }
        except Exception as exc:  # noqa: BLE001 - guidance path must never block (FR-MCP-4)
            return {
                "available": False,
                "reason": f"query could not be served ({exc!r}); proceed without constraints",
                "bundle_id": None,
                "constraints": [],
            }

    def describe_scope(self, version: str | None = None) -> dict:
        """Return the selector vocabulary present in the bundle (discovery aid).

        Lets an agent look up valid scope values (provider/resource-type/repo tags
        and relationship layers/interactions) instead of guessing, e.g. learning
        that S3 uses the Terraform id ``aws_s3_bucket`` rather than ``s3_bucket``.
        Fails open like get_constraints (guidance, never blocks)."""
        empty = {
            "providers": [], "resource_types": [], "environments": [], "repos": [],
            "categories": [], "severities": [],
            "relationship": {"source_layers": [], "target_layers": [], "interactions": []},
            "sources": [],
        }
        try:
            bundle = self._resolve_bundle(version)
            if bundle is None:
                return {"available": False, "bundle_id": None, "constraint_count": 0, **empty}

            providers, resource_types, environments, repos = set(), set(), set(), set()
            categories, severities, sources = set(), set(), set()
            src_layers, tgt_layers, interactions = set(), set(), set()
            for ic in bundle.constraints:
                c, s = ic.constraint, ic.constraint.scope
                providers |= set(s.providers)
                resource_types |= set(s.resource_types)
                environments |= set(s.environments)
                repos |= set(s.repos)
                categories.add(c.category.value)
                severities.add(c.severity.value)
                sources.add(ic.source)
                if s.relationship:
                    r = s.relationship
                    if r.source and r.source.layer:
                        src_layers.add(r.source.layer)
                    if r.target and r.target.layer:
                        tgt_layers.add(r.target.layer)
                    if r.interaction:
                        interactions.add(r.interaction)
            return {
                "available": True,
                "bundle_id": bundle.bundle_id,
                "constraint_count": len(bundle.constraints),
                "providers": sorted(providers),
                "resource_types": sorted(resource_types),
                "environments": sorted(environments),
                "repos": sorted(repos),
                "categories": sorted(categories),
                "severities": sorted(severities),
                "relationship": {
                    "source_layers": sorted(src_layers),
                    "target_layers": sorted(tgt_layers),
                    "interactions": sorted(interactions),
                },
                "sources": sorted(sources),
            }
        except Exception as exc:  # noqa: BLE001 - discovery is guidance; never block
            return {"available": False, "bundle_id": None, "constraint_count": 0, "reason": repr(exc), **empty}

    def validate(self, artifact, scope: dict | None = None, version: str | None = None) -> dict:
        bundle = self._resolve_bundle(version)
        if bundle is None:
            raise ValidationUnavailable("no servable bundle for the requested version")
        sc = Scope.model_validate(scope or {})
        report = _validate(bundle, artifact, sc, self.registry, self.config)
        return report.to_dict()
