"""Checkov engine adapter (FR-ENGINE-2, CONTRACTS §2/§3/§4/§5).

Invokes the real ``checkov`` binary and delegates all SARIF parsing to the
shared normalizer (CONTRACTS §5).

How Checkov 3.x delivers SARIF
-------------------------------
Checkov 3.x does NOT write SARIF to stdout.  With ``--output sarif`` it writes
a file named ``results.sarif`` in the **current working directory** of the
subprocess.  (Stdout receives only the ASCII-art banner and progress text.)
The adapter therefore:
  1. Creates a ``TemporaryDirectory`` as the subprocess cwd.
  2. After the subprocess exits, reads ``<cwd>/results.sarif``.
  3. Parses the JSON from that file.
  4. Deletes the temp directory automatically when the ``with`` block exits.

CLI flags used
--------------
``-f <file>``            scan a single artifact file (absolute path)
``--output sarif``       write SARIF to ``results.sarif`` in subprocess cwd
``--config-file <path>`` resolve check selection from the policy locator

The ``policy`` arg to ``evaluate`` must be an absolute path to a Checkov
YAML config file that carries ``check:`` and ``framework:`` keys, e.g.::

    check:
      - CKV_AWS_20
    framework:
      - cloudformation

Artifact materialization (CONTRACTS §2)
----------------------------------------
- ``artifact`` is a dict/list (parsed IaC JSON) → written to a
  ``NamedTemporaryFile`` with suffix ``.json`` and scanned as ``-f <temp>``.
  Deleted in ``finally`` (separate from the run directory).
- ``artifact`` is a ``str`` or ``os.PathLike`` → scanned directly; no temp file.

Temp-path determinism (NFR-1)
------------------------------
Checkov embeds the scanned file path in SARIF ``physicalLocation.uri``.
On macOS/Linux the URI omits the leading ``/`` (e.g.
``var/folders/.../tmp1234.json``).  Before parsing, the adapter replaces
every occurrence of (a) the absolute temp path, (b) the relative form (no
leading ``/``), and (c) the basename with ``"<artifact>"``.  This ensures two
``evaluate()`` calls on the same dict produce identical ``to_dict()`` — the
VH-ENGINE-2 determinism check.

Error vs fail
-------------
Checkov exits 1 when violations are found (normal); exits 0 when none are
found.  The adapter reads ``results.sarif`` regardless of exit code.  If the
file is absent or unparseable, OR if ``subprocess`` raises, the adapter
returns ``EngineVerdict.errored`` — it never raises to the caller (NFR-2).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ....model import EnforcementBinding
from ...interface import EngineAdapter, EngineVerdict
from ..sarif import compute_result, parse_sarif

# Canonical placeholder substituted for temp-file paths in SARIF output so
# that two evaluate() calls on the same artifact produce identical to_dict().
_ARTIFACT_PLACEHOLDER = "<artifact>"

# Filename Checkov 3.x writes when --output sarif is requested.
_SARIF_RESULT_FILENAME = "results.sarif"


class CheckovAdapter(EngineAdapter):
    """Engine adapter for Checkov (https://github.com/bridgecrewio/checkov).

    Constructor options (passed from config ``options:`` dict):

    ``bin``        Path / name of the checkov executable.  Default ``"checkov"``.
    ``timeout``    Subprocess timeout in seconds.  Default ``60``.
    ``min_level``  Minimum SARIF level to count as a violation
                   (``"note"``, ``"warning"``, ``"error"``).  Default ``"warning"``.
    """

    name = "checkov"

    def __init__(self, options: dict | None = None) -> None:
        opts = options or {}
        self.bin: str = opts.get("bin", "checkov")
        self.timeout: int = int(opts.get("timeout", 60))
        self.min_level: str = opts.get("min_level", "warning")

    # ------------------------------------------------------------------
    # EngineAdapter interface
    # ------------------------------------------------------------------

    def can_handle(self, binding: EnforcementBinding) -> bool:  # FR-ENGINE-3a
        return binding.engine == self.name

    @property
    def available(self) -> bool:
        """True when the checkov binary is resolvable on PATH."""
        return shutil.which(self.bin) is not None

    def evaluate(self, artifact: Any, policy: str) -> EngineVerdict:  # FR-ENGINE-3b
        """Evaluate *artifact* against the Checkov config at *policy*.

        Never raises; returns ``EngineVerdict.errored`` for any unrunnable
        condition (missing binary, missing policy, timeout, absent/invalid SARIF).
        A clean run that finds violations → ``verdict="fail"`` (not errored).
        """
        policy_path = Path(policy)
        if not policy_path.exists():
            return EngineVerdict.errored(
                self.name, policy, f"policy not found: {policy}"
            )
        if not self.available:
            return EngineVerdict.errored(
                self.name, policy, f"checkov binary not found: {self.bin!r}"
            )

        scan_target, temp_path, mat_error = self._materialize(artifact)
        if scan_target is None:
            return EngineVerdict.errored(
                self.name, policy, mat_error or "artifact materialization failed"
            )

        try:
            return self._run_and_parse(scan_target, temp_path, policy, str(policy_path))
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _materialize(
        self, artifact: Any
    ) -> tuple[str | None, str | None, str | None]:
        """Return ``(scan_target, temp_path, error_msg)``.

        When ``artifact`` is a path-like: ``(str(artifact), None, None)`` —
        no temp file created.

        When ``artifact`` is a dict/list: write a temp ``.json``,
        return ``(temp_path, temp_path, None)`` — caller must delete it.

        On failure: ``(None, None, error_message)``.
        """
        if isinstance(artifact, (str, os.PathLike)):
            return str(artifact), None, None

        try:
            tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump(artifact, tf)
            tf.flush()
            tf.close()
            return tf.name, tf.name, None
        except Exception as exc:  # noqa: BLE001
            return None, None, f"failed to write artifact to temp file: {exc}"

    def _run_and_parse(
        self,
        scan_target: str,
        temp_path: str | None,
        policy: str,
        policy_path_str: str,
    ) -> EngineVerdict:
        """Invoke checkov, read results.sarif from a dedicated temp dir, return verdict.

        Checkov 3.x writes SARIF to ``results.sarif`` in the subprocess cwd,
        not to stdout.  We supply a fresh TemporaryDirectory as cwd so the file
        lands there without colliding with concurrent evaluations.
        """
        argv = [
            self.bin,
            "-f", scan_target,
            "--output", "sarif",
            "--config-file", policy_path_str,
        ]

        try:
            with tempfile.TemporaryDirectory() as run_dir:
                try:
                    proc = subprocess.run(
                        argv,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                        cwd=run_dir,
                    )
                except FileNotFoundError:
                    return EngineVerdict.errored(
                        self.name, policy, f"checkov binary not found: {self.bin!r}"
                    )
                except subprocess.TimeoutExpired:
                    return EngineVerdict.errored(
                        self.name, policy, "checkov evaluation timed out"
                    )

                # Checkov exits 1 when it finds violations — NOT an engine error.
                # Detect the verdict by reading the SARIF file it wrote.
                sarif_file = Path(run_dir) / _SARIF_RESULT_FILENAME
                if not sarif_file.exists():
                    stderr_hint = (proc.stderr.strip() or proc.stdout.strip())[:200]
                    return EngineVerdict.errored(
                        self.name,
                        policy,
                        f"checkov did not produce {_SARIF_RESULT_FILENAME!r} "
                        f"(exit {proc.returncode}): {stderr_hint}",
                    )

                sarif_text = sarif_file.read_text(encoding="utf-8")
                # TemporaryDirectory cleans up here; sarif_text is already in memory.

        except (FileNotFoundError, subprocess.TimeoutExpired):  # pragma: no cover
            # Should not reach here — already handled inside the with block,
            # but kept as a safety net for any future refactor.
            raise  # let the caller's try/except handle it
        except Exception as exc:  # noqa: BLE001
            return EngineVerdict.errored(
                self.name, policy, f"unexpected error running checkov: {exc}"
            )

        # --- determinism: strip temp path so two runs produce identical to_dict() ---
        if temp_path is not None:
            sarif_text = self._normalize_paths(sarif_text, temp_path)

        try:
            sarif = json.loads(sarif_text)
        except json.JSONDecodeError as exc:
            return EngineVerdict.errored(
                self.name,
                policy,
                f"checkov produced invalid SARIF JSON: {exc}",
            )

        violations = parse_sarif(sarif, self.name)
        return compute_result(violations, policy, self.name, min_level=self.min_level)

    @staticmethod
    def _normalize_paths(sarif_text: str, temp_path: str) -> str:
        """Replace temp-file path variants with the canonical placeholder.

        Checkov 3.x embeds the path WITHOUT a leading ``/`` in SARIF
        ``physicalLocation.artifactLocation.uri`` on macOS/Linux (e.g. the
        absolute path ``/var/folders/.../tmp.json`` becomes
        ``var/folders/.../tmp.json`` in the SARIF).

        We replace three forms — longest first to prevent partial overlaps:

        1. Absolute path with leading slash    e.g. ``/private/tmp/tmp123.json``
        2. Relative form, no leading slash     e.g. ``private/tmp/tmp123.json``
        3. Basename only                       e.g. ``tmp123.json``
        """
        basename = Path(temp_path).name
        relative = temp_path.lstrip("/")

        result = sarif_text
        # (1) absolute path — longest; replace first to avoid partial overlap
        result = result.replace(temp_path, _ARTIFACT_PLACEHOLDER)
        # (2) relative form (what Checkov 3.x actually embeds on macOS/Linux)
        if relative != temp_path:
            result = result.replace(relative, _ARTIFACT_PLACEHOLDER)
        # (3) basename — most specific fallback for any remaining occurrence
        result = result.replace(basename, _ARTIFACT_PLACEHOLDER)
        return result
