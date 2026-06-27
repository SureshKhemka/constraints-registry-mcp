"""Semgrep engine adapter (FR-ENGINE-2).

Runs the ``semgrep`` binary against source-code fixtures, emits SARIF, and
normalizes the output through the shared SARIF seam (CONTRACTS §5).

Artifact convention (CONTRACTS §2):
  - A **path string** → scanned as-is (no temp tree created).
  - A **dict** ``{"path": "rel/name.ext", "content": "<source text>"}`` → written
    to a temp tree and the tree is scanned.
  - A **list** of such dicts → all written to the same temp tree.

The adapter does NOT raise for any unrunnable condition — it returns
``EngineVerdict.errored`` instead (NFR-2).

Determinism (NFR-1, CONTRACTS §7): Semgrep embeds the scanned file path into the
SARIF ``artifactLocation.uri`` field.  When a temp tree is used, we run semgrep
with ``cwd=<temp_dir>`` and target ``.``, which makes Semgrep emit *relative*
URIs.  As a defensive belt-and-suspenders measure, we also strip any remaining
temp-dir prefix from URIs in the SARIF JSON before handing it to ``parse_sarif``.
This means ``Violation.path`` and the URI stored inside ``Violation.raw`` are both
relative and stable across repeated calls with a freshly materialized temp dir.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ....model import EnforcementBinding
from ...interface import EngineAdapter, EngineVerdict, Violation
from ..sarif import compute_result, parse_sarif

__all__ = ["SemgrepAdapter"]


class SemgrepAdapter(EngineAdapter):
    """Engine adapter for the Semgrep static-analysis tool.

    Constructor options (all optional):
      bin        Path / name of the semgrep binary (default: "semgrep").
      timeout    Subprocess timeout in seconds (default: 60).
      min_level  Minimum SARIF level to count as a violation: "note", "warning",
                 or "error" (default: "warning").
    """

    name = "semgrep"

    def __init__(self, options: dict | None = None) -> None:
        options = options or {}
        self.bin: str = options.get("bin", "semgrep")
        self.timeout: int = int(options.get("timeout", 60))
        self.min_level: str = options.get("min_level", "warning")

    # ------------------------------------------------------------------ #
    #  FR-ENGINE-3a
    # ------------------------------------------------------------------ #

    def can_handle(self, binding: EnforcementBinding) -> bool:
        return binding.engine == self.name

    # ------------------------------------------------------------------ #
    #  Available guard (harness SKIPs when False rather than failing)
    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        """True iff the configured semgrep binary is on PATH."""
        return shutil.which(self.bin) is not None

    # ------------------------------------------------------------------ #
    #  FR-ENGINE-3b: evaluate
    # ------------------------------------------------------------------ #

    def evaluate(self, artifact: Any, policy: str) -> EngineVerdict:
        """Evaluate *artifact* against the Semgrep ruleset at *policy*.

        *policy* must be an absolute path to a ``*.yaml`` / ``*.yml`` ruleset
        (or a Semgrep registry reference like ``p/owasp-top-ten`` — anything
        accepted by ``semgrep --config``).  The registry always passes an
        absolute path after resolving the locator.

        Never raises; returns ``EngineVerdict.errored`` on any failure.
        """
        # ---- guards ----
        policy_path = Path(policy)
        if not policy_path.exists():
            return EngineVerdict.errored(
                self.name, policy, f"policy not found: {policy}"
            )
        if not self.available:
            return EngineVerdict.errored(
                self.name, policy, f"semgrep binary not found: {self.bin!r}"
            )

        temp_dir: Path | None = None
        try:
            # ---- materialise artifact ----
            # _materialise may raise ValueError for malformed list items (e.g.
            # missing 'content', non-dict element).  Catch ALL exceptions here
            # so evaluate() NEVER raises (NFR-2, CONTRACTS §2).  The temp tree
            # is cleaned up by the outer finally block even on this path.
            try:
                run_target, run_cwd, temp_dir = self._materialise(artifact)
            except Exception as exc:  # noqa: BLE001
                return EngineVerdict.errored(
                    self.name, policy, f"artifact materialisation failed: {exc}"
                )

            if run_target is None:
                # _materialise returned the unsupported-type sentinel.
                return EngineVerdict.errored(
                    self.name,
                    policy,
                    f"unsupported artifact type for semgrep: {type(artifact).__name__}",
                )

            # ---- build command ----
            # Security: argv list — no shell=True, no string interpolation of
            # untrusted values (CONTRACTS §security).
            #
            # --quiet is REQUIRED on Semgrep 1.x: without it, semgrep writes
            # progress/summary text to stdout *before* the SARIF JSON, making
            # the combined output unparseable as JSON.  --quiet suppresses all
            # non-JSON output to stdout (progress goes to stderr); the SARIF
            # JSON is the only thing left on stdout.
            #
            # policy_path.resolve() converts any relative path to absolute so
            # that the path remains valid when the subprocess runs with
            # cwd=<temp_dir> (CONTRACTS §3: registry passes absolute paths in
            # production, but resolve() is a defensive no-op on an already-
            # absolute path).
            cmd: list[str] = [
                self.bin,
                "--config", str(policy_path.resolve()),
                "--sarif",
                "--metrics=off",
                "--quiet",
                run_target,
            ]

            # ---- run ----
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=run_cwd,
                )
            except FileNotFoundError:
                return EngineVerdict.errored(
                    self.name, policy, f"semgrep binary not found: {self.bin!r}"
                )
            except subprocess.TimeoutExpired:
                return EngineVerdict.errored(
                    self.name, policy, "semgrep evaluation timed out"
                )

            # ---- distinguish tool error from findings ----
            # Semgrep exits 0 (no findings) or 1 (findings found).  Exit ≥ 2
            # indicates a configuration / fatal error (CONTRACTS §4: errored ≠ fail).
            if proc.returncode not in (0, 1):
                err = (proc.stderr.strip() or proc.stdout[:400]).strip()
                return EngineVerdict.errored(
                    self.name,
                    policy,
                    f"semgrep exited {proc.returncode}: {err}",
                )

            # ---- parse SARIF ----
            try:
                sarif: dict = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                err = proc.stderr.strip() or str(exc)
                return EngineVerdict.errored(
                    self.name, policy, f"unparseable semgrep output: {err}"
                )

            # ---- normalise paths for determinism ----
            if temp_dir is not None:
                sarif = _strip_temp_prefix(sarif, temp_dir)

            # ---- delegate to SARIF seam (CONTRACTS §5) ----
            violations: list[Violation] = parse_sarif(sarif, self.name)
            return compute_result(
                violations, policy, self.name, min_level=self.min_level
            )

        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _materialise(
        self, artifact: Any
    ) -> tuple[str | None, str | None, Path | None]:
        """Write *artifact* to a temp tree if needed.

        Returns ``(run_target, run_cwd, temp_dir)``:
        - *run_target*: path/glob to pass as the semgrep scan target.
        - *run_cwd*: working directory for the subprocess (or ``None``).
        - *temp_dir*: ``Path`` to clean up afterwards (or ``None``).

        Returns ``(None, None, None)`` for an unsupported artifact type so the
        caller can emit ``EngineVerdict.errored`` without raising.
        """
        # Case 1: plain path string — scan it directly, no temp tree.
        if isinstance(artifact, str):
            return artifact, None, None

        # Case 2: single {path, content} envelope → normalise to list.
        if isinstance(artifact, dict):
            if "path" in artifact and "content" in artifact:
                items: list[Any] = [artifact]
            else:
                # Unexpected dict shape — unsupported.
                return None, None, None
        elif isinstance(artifact, list):
            items = artifact
        else:
            return None, None, None

        # Write all items into a fresh temp directory.
        tmp = Path(tempfile.mkdtemp(prefix="cregistry_semgrep_"))
        try:
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(
                        f"each artifact list item must be a dict, got {type(item).__name__}"
                    )
                rel = item.get("path")
                content = item.get("content")
                if rel is None or content is None:
                    raise ValueError(
                        "artifact dict must have 'path' and 'content' keys; "
                        f"got keys: {sorted(item.keys())}"
                    )
                dest = tmp / str(rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(content), encoding="utf-8")
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        # Run from inside the temp dir so Semgrep emits relative URIs.
        return ".", str(tmp), tmp


# ---------------------------------------------------------------------------
# SARIF path normalisation for determinism
# ---------------------------------------------------------------------------

def _strip_temp_prefix(sarif: dict, temp_dir: Path) -> dict:
    """Return a deep copy of *sarif* with temp-dir prefixes stripped from URIs.

    Semgrep is invoked with ``cwd=<temp_dir>`` and target ``"."``, which normally
    makes it emit relative URIs already.  As a defensive measure we also handle
    the case where the engine embeds absolute paths — e.g., ``file:///tmp/…``
    or ``/tmp/…`` — and relativise them so ``Violation.path`` and the URI in
    ``Violation.raw`` are stable across runs with freshly-created temp dirs.
    """
    sarif = copy.deepcopy(sarif)
    temp_str = str(temp_dir)
    for run in sarif.get("runs", []):
        # Normalise the artifacts table if present.
        for artifact in run.get("artifacts", []):
            loc = artifact.get("location")
            if isinstance(loc, dict):
                _normalise_uri(loc, temp_str)
        # Normalise every result location.
        for result in run.get("results", []):
            for loc in result.get("locations", []):
                phys = loc.get("physicalLocation")
                if isinstance(phys, dict):
                    art = phys.get("artifactLocation")
                    if isinstance(art, dict):
                        _normalise_uri(art, temp_str)
    return sarif


def _normalise_uri(art: dict, temp_str: str) -> None:
    """In-place: strip *temp_str* prefix (and optional ``file://`` scheme) from art["uri"]."""
    uri: str | None = art.get("uri")
    if not uri:
        return
    # Strip file:// scheme variants (file:///path or file://host/path)
    clean = uri
    if clean.startswith("file://"):
        clean = clean[7:]
        if clean.startswith("/"):
            pass  # file:///path → /path
        # else: file://host/path — keep as-is after stripping scheme
    # Relativise against the temp dir.
    try:
        clean = str(Path(clean).relative_to(temp_str))
    except ValueError:
        clean = clean  # already relative or a different absolute path — leave it
    art["uri"] = clean
