"""MCP server (FR-MCP-1/2/3/4).

Exposes the registry as an MCP server over stdio with two tools whose
input/output schemas form a stable, documented contract (FR-MCP-3):

* ``get_constraints(scope, version?)`` -> scoped constraints (FR-QUERY).
  Fails open (FR-MCP-4): on any failure returns ``available: false`` with an
  empty constraint list so the calling agent can proceed unblocked.
* ``validate(artifact, scope, version?)`` -> validation report (FR-VALIDATE).
  May surface an explicit error, since it is an active check.

The MCP tools are thin wrappers over ``RegistryService`` (see ``service.py``).
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .service import RegistryService, ValidationUnavailable


def build_server(service: RegistryService | None = None, config_path: str | None = None) -> FastMCP:
    if service is None:
        config_path = config_path or os.environ.get("CREGISTRY_CONFIG", "registry.config.yaml")
        service = RegistryService.from_config(load_config(config_path))

    mcp = FastMCP("constraint-registry")

    @mcp.tool(
        description=(
            "Return engineering constraints relevant to a scope. Inputs: scope "
            "(providers, resource_types, environments, repos, relationship), "
            "optional version (bundle id; defaults to latest). resource_types use "
            "Terraform resource identifiers, e.g. 'aws_s3_bucket' (NOT 's3_bucket'); "
            "repos are tags like 'tag:data-plane'. If unsure of valid values, call "
            "describe_scope first, or simply omit a dimension (omitted dimensions "
            "are 'don't care' and broaden the match rather than excluding). Output: "
            "{available, bundle_id, constraints[]}. Fails open: on any error returns "
            "available=false with an empty constraints list so you can proceed."
        )
    )
    def get_constraints(scope: dict[str, Any] | None = None, version: str | None = None) -> dict:
        return service.get_constraints(scope, version)

    @mcp.tool(
        description=(
            "Discover the selector vocabulary present in the registry so you can "
            "build a correct scope instead of guessing. Output lists the distinct "
            "providers, resource_types (Terraform resource identifiers, e.g. "
            "'aws_s3_bucket' not 's3_bucket'), environments, repos (tags like "
            "'tag:data-plane'), categories, severities, sources, and relationship "
            "layers/interactions. Call this first if unsure of valid scope values."
        )
    )
    def describe_scope(version: str | None = None) -> dict:
        return service.describe_scope(version)

    @mcp.tool(
        description=(
            "Validate a candidate artifact against in-scope constraints by "
            "delegating to enforcement engines. Inputs: artifact (object), scope, "
            "optional version. Output: {bundle_id, passed, results[]} where each "
            "result has constraint, severity, kind, verdict, violations, guidance."
        )
    )
    def validate(
        artifact: dict[str, Any], scope: dict[str, Any] | None = None, version: str | None = None
    ) -> dict:
        try:
            return service.validate(artifact, scope, version)
        except ValidationUnavailable as exc:
            return {"error": str(exc), "bundle_id": None, "passed": False, "results": []}

    return mcp


def _resolve_transport(http: bool, transport: str) -> str:
    """Map CLI flags to a FastMCP transport name."""
    if http or transport == "http":
        return "streamable-http"
    return transport  # "stdio" or "sse"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Constraint Registry MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="transport to serve on (default: stdio, spawned per-tool)",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="shorthand for --transport http: one shared server every tool connects to",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host for http/sse (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="bind port for http/sse (default: 8765)")
    parser.add_argument(
        "--config",
        default=os.environ.get("CREGISTRY_CONFIG", "registry.config.yaml"),
        help="path to registry config (default: $CREGISTRY_CONFIG or registry.config.yaml)",
    )
    args = parser.parse_args(argv)

    transport = _resolve_transport(args.http, args.transport)
    server = build_server(config_path=args.config)
    if transport != "stdio":
        server.settings.host = args.host
        server.settings.port = args.port
    server.run(transport=transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
