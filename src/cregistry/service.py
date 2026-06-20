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

import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import RegistryConfig, load_config
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
    def __init__(
        self,
        config: RegistryConfig,
        store: BundleStore,
        registry: EngineRegistry,
        config_path: str | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.registry = registry
        # Path to reload config from on reload(); None => reuse the in-memory
        # config object (source/policy files on disk are still re-read).
        self.config_path = config_path
        self.last_reload: dict | None = None
        # Guards atomic swap of (config, store, registry) on reload (thread-safe
        # hot-reload alongside request handling). Reads take a brief snapshot.
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: RegistryConfig) -> "RegistryService":
        store = BundleStore()
        bundle = import_sources(config).bundle
        store.add(bundle)
        return cls(config, store, EngineRegistry.from_config(config))

    @classmethod
    def from_config_path(cls, path: str | Path) -> "RegistryService":
        """Build a service that can later reload() from the given config file."""
        svc = cls.from_config(load_config(path))
        svc.config_path = str(path)
        return svc

    def _snapshot(self) -> tuple[BundleStore, EngineRegistry, RegistryConfig]:
        with self._lock:
            return self.store, self.registry, self.config

    def reload(self) -> dict:
        """Re-import from disk and atomically publish a new immutable bundle.

        A no-op if nothing changed (identical content hash). On a failed import
        (e.g. an unresolvable precedence conflict), the last-good bundle keeps
        serving and the failure is reported (NFR-2). Old bundle versions remain
        retrievable in the store (FR-VERSION-3).
        """
        cfg = load_config(self.config_path) if self.config_path else self.config
        report = import_sources(cfg)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            prev = self.store.latest()
            prev_id = prev.bundle_id if prev else None
            if report.ok:
                self.config = cfg
                self.registry = EngineRegistry.from_config(cfg)
                self.store.add(report.bundle)
                new_id = self.store.latest().bundle_id
                self.last_reload = {
                    "at": now,
                    "ok": True,
                    "changed": new_id != prev_id,
                    "bundle_id": new_id,
                    "constraint_count": len(report.bundle.constraints),
                    "schema_errors": len(report.schema_errors),
                }
            else:
                self.last_reload = {
                    "at": now,
                    "ok": False,
                    "changed": False,
                    "kept_bundle_id": prev_id,
                    "conflicts": [c.to_dict() for c in report.conflicts],
                    "schema_errors": len(report.schema_errors),
                }
        return self.last_reload

    def _resolve_bundle(self, store: BundleStore, version: str | None):
        return store.get(version)

    def get_constraints(self, scope: dict | None = None, version: str | None = None) -> dict:
        # FR-MCP-4: fail open. Any failure -> empty, proceed-able result.
        try:
            store, _, _ = self._snapshot()
            bundle = self._resolve_bundle(store, version)
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
            store, _, _ = self._snapshot()
            bundle = self._resolve_bundle(store, version)
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
        store, registry, config = self._snapshot()
        bundle = self._resolve_bundle(store, version)
        if bundle is None:
            raise ValidationUnavailable("no servable bundle for the requested version")
        sc = Scope.model_validate(scope or {})
        report = _validate(bundle, artifact, sc, registry, config)
        return report.to_dict()
