# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-27

First tagged, packaged release.

### Added
- **Engine adapters**: OPA and Conftest (Rego), plus **Checkov** (IaC scanning)
  and **Semgrep** (application source code), behind one stable `EngineAdapter`
  interface.
- **Shared SARIF normalization seam** (`adapters/sarif/`) reused by the Checkov
  and Semgrep adapters.
- **Catalog importers** for Checkov and Semgrep that turn an engine's rule
  catalog/ruleset into draft constraint stubs with license/source provenance.
- **MCP server** exposing `describe_scope`, `get_constraints`, and `validate`
  over stdio or HTTP, with fail-open `get_constraints` and optional hot reload.
- **Validation harness** and a data-driven engine-interface conformance suite
  runnable against any adapter.
- Open-source project setup: Apache-2.0 license, contributing guide, code of
  conduct, security policy, issue/PR templates, and CI.
- PyPI packaging (`constraints-registry`) and an MCP registry `server.json`.

[Unreleased]: https://github.com/SureshKhemka/constraints-registry/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SureshKhemka/constraints-registry/releases/tag/v0.1.0
