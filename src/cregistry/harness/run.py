"""Harness entrypoint (Section 7, VH-OUTPUT-1/2).

Runs every registered VH-* check against the configured, self-contained sample
sources, emits a machine-readable JSON result (NFR-4), and exits non-zero if any
check fails. Skipped checks (unavailable preconditions) do not fail the run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable

from ..config import RegistryConfig, load_config
from .checks import engine, import_, integrity, mcp, namespace, output, reload, schema, version
from .result import CheckResult, Status

# Ordered registry of checks. Each entry runs a VH-* section against the config.
# New sections are appended here as increments land.
CHECKS: list[Callable[[RegistryConfig], list[CheckResult]]] = [
    schema.run,
    import_.run,
    namespace.run,
    version.run,
    engine.run,
    integrity.run,
    mcp.run,
    output.run,
    reload.run,
]


def run_all(config: RegistryConfig) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results.extend(check(config))
        except Exception as exc:  # noqa: BLE001 - a crashing check must not abort the harness (NFR-2)
            results.append(
                CheckResult.fail(
                    section=getattr(check, "SECTION", check.__module__),
                    check=check.__module__,
                    message=f"check raised an unexpected exception: {exc!r}",
                )
            )
    return results


def build_report(results: list[CheckResult]) -> dict:
    counts = {s.value: 0 for s in Status}
    for r in results:
        counts[r.status.value] += 1
    passed = counts[Status.failed.value] == 0
    return {
        "passed": passed,
        "summary": {**counts, "total": len(results)},
        "checks": [r.to_dict() for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Constraint Registry validation harness")
    parser.add_argument(
        "--config",
        default=os.environ.get("CREGISTRY_CONFIG", "registry.config.yaml"),
        help="path to registry config (default: registry.config.yaml)",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    results = run_all(config)
    report = build_report(results)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
