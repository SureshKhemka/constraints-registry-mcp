"""Shared pytest configuration and path constants for the adapter eval suite.

All paths are derived from installed package locations so they remain valid
regardless of the working directory when pytest is invoked.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repo layout constants — derived from this file's location so they stay
# correct when uv run pytest is invoked from any directory.
# ---------------------------------------------------------------------------

#: Absolute path to the repository root.
REPO_ROOT = Path(__file__).parent.parent

#: Absolute path to the tests/fixtures directory.
TESTS_FIXTURE_DIR = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Adapter fixture directories — derived from installed package locations.
# ---------------------------------------------------------------------------

import cregistry.engine.adapters.sarif as _sarif_pkg
import cregistry.engine.adapters.checkov as _ck_pkg
import cregistry.engine.adapters.semgrep as _sg_pkg

#: SARIF normalizer sample fixtures.
SARIF_FIXTURE_DIR: Path = Path(_sarif_pkg.__file__).parent / "_fixtures"

#: Checkov adapter pass/fail/policy fixtures.
CHECKOV_FIXTURE_DIR: Path = Path(_ck_pkg.__file__).parent / "_fixtures"

#: Semgrep adapter pass/fail/rule fixtures.
SEMGREP_FIXTURE_DIR: Path = Path(_sg_pkg.__file__).parent / "_fixtures"

# ---------------------------------------------------------------------------
# Binary availability — evaluated at collection time so marks work.
# ---------------------------------------------------------------------------

CHECKOV_MISSING: bool = shutil.which("checkov") is None
SEMGREP_MISSING: bool = shutil.which("semgrep") is None
OPA_MISSING: bool = shutil.which("opa") is None
CONFTEST_MISSING: bool = shutil.which("conftest") is None

# Convenience marks used by individual test modules.
requires_checkov = pytest.mark.skipif(
    CHECKOV_MISSING, reason="checkov binary not on PATH — skipping (VH-INTEGRITY-1)"
)
requires_semgrep = pytest.mark.skipif(
    SEMGREP_MISSING, reason="semgrep binary not on PATH — skipping (VH-INTEGRITY-1)"
)
requires_opa = pytest.mark.skipif(
    OPA_MISSING, reason="opa binary not on PATH"
)
requires_conftest_bin = pytest.mark.skipif(
    CONFTEST_MISSING, reason="conftest binary not on PATH"
)
