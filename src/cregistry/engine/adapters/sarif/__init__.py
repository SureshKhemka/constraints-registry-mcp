"""SARIF v2.1.0 normalizer — the single SARIF ingestion point for all engines.

Public API (signatures are FROZEN per CONTRACTS §5):

    parse_sarif(sarif_json: dict, engine: str) -> list[Violation]
    compute_result(violations, policy, engine, *, min_level="warning") -> EngineVerdict

Engine adapters (Checkov, Semgrep, …) call these two functions and MUST NOT
re-implement SARIF parsing.  The helper ``get_sarif_level`` is also public so
that engine adapters can inspect the level of individual violations without
coupling to the internal _LEVEL_ORDER dict.

Level ordering (SARIF §3.27.10 + CONTRACTS §4):
    note (0) < warning (1) < error (2)
    "none" results are dropped during parse and never become Violations.
    The default level when the field is absent is "warning" (SARIF spec default).
"""

from __future__ import annotations

import logging
from typing import Any

from ...interface import EngineVerdict, Verdict, Violation  # noqa: F401 — Verdict re-exported

__all__ = ["parse_sarif", "compute_result", "get_sarif_level"]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Level constants
# ---------------------------------------------------------------------------

#: Numeric ordering for SARIF levels.  Absent from this dict means unknown →
#: treat as the default.  "none" is intentionally absent — it is dropped, not
#: compared.
_LEVEL_ORDER: dict[str, int] = {
    "note": 0,
    "warning": 1,
    "error": 2,
}
_DEFAULT_LEVEL = "warning"  # SARIF §3.27.10: level defaults to "warning" when absent
_DROP_LEVELS = frozenset({"none"})

# ---------------------------------------------------------------------------
# Pathological-input guards
# ---------------------------------------------------------------------------

#: Maximum number of runs to process from a single SARIF document.
_MAX_RUNS = 100

#: Maximum number of results to process in total across all runs.  Prevents a
#: hostile SARIF report from hanging the registry (NFR — defensive resource use).
_MAX_RESULTS = 50_000


# ---------------------------------------------------------------------------
# Public helper: level accessor
# ---------------------------------------------------------------------------

def get_sarif_level(raw: Any) -> str:
    """Return the resolved SARIF level stored inside a Violation's raw dict.

    During ``parse_sarif`` we embed the fully-resolved level (after
    defaultConfiguration fallback) back into the raw copy under the ``"level"``
    key, so this function is a plain dict lookup — the rule-map lookup already
    happened at parse time.

    Falls back to ``"warning"`` when the key is absent or the value is not a
    recognised level string.  Never returns ``"none"`` — results at level
    ``"none"`` are dropped during parse and never become ``Violation`` objects.
    """
    if not isinstance(raw, dict):
        return _DEFAULT_LEVEL
    level = raw.get("level", _DEFAULT_LEVEL)
    if not isinstance(level, str) or level not in _LEVEL_ORDER:
        return _DEFAULT_LEVEL
    return level


def _level_gte(level: str, min_level: str) -> bool:
    """True when ``level`` >= ``min_level`` in SARIF ordering."""
    lo = _LEVEL_ORDER.get(level, _LEVEL_ORDER[_DEFAULT_LEVEL])
    mo = _LEVEL_ORDER.get(min_level, _LEVEL_ORDER[_DEFAULT_LEVEL])
    return lo >= mo


# ---------------------------------------------------------------------------
# parse_sarif
# ---------------------------------------------------------------------------

def parse_sarif(sarif_json: dict, engine: str) -> list[Violation]:
    """Flatten every SARIF run.result (level != 'none') into Violation objects.

    Stores a shallow copy of the original SARIF result in ``Violation.raw``,
    with the fully-resolved ``level`` value embedded so that
    ``get_sarif_level(v.raw)`` is always a simple dict lookup.

    Parameters
    ----------
    sarif_json:
        Parsed SARIF document (output of ``json.loads``).  Any non-dict value
        is treated as malformed → returns ``[]``.
    engine:
        Name of the calling engine (used only for log messages).

    Returns
    -------
    list[Violation]
        Empty list on malformed/empty input.  Individual un-parseable results
        are skipped with a DEBUG log; parseable siblings are still returned.
        Never raises.
    """
    try:
        return _parse_sarif_inner(sarif_json, engine)
    except Exception as exc:  # noqa: BLE001
        log.warning("parse_sarif(%s): unexpected error, returning []: %s", engine, exc)
        return []


def _parse_sarif_inner(sarif_json: dict, engine: str) -> list[Violation]:
    if not isinstance(sarif_json, dict):
        return []

    runs = sarif_json.get("runs")
    if not isinstance(runs, list):
        return []

    violations: list[Violation] = []
    total_results = 0

    for run in runs[:_MAX_RUNS]:
        if not isinstance(run, dict):
            continue

        rule_map = _build_rule_map(run)
        results = run.get("results")
        if not isinstance(results, list):
            continue

        for result in results:
            if total_results >= _MAX_RESULTS:
                log.warning(
                    "parse_sarif(%s): reached _MAX_RESULTS=%d cap, truncating",
                    engine,
                    _MAX_RESULTS,
                )
                return violations  # early exit to avoid wasted iteration

            total_results += 1

            try:
                v = _result_to_violation(result, rule_map)
            except Exception as exc:  # noqa: BLE001
                log.debug("parse_sarif(%s): skipping result (error: %s)", engine, exc)
                continue

            if v is not None:
                violations.append(v)

    return violations


def _build_rule_map(run: dict) -> dict[str, dict]:
    """Index rules by id for ``defaultConfiguration.level`` lookup.

    Returns an empty dict on any structural problem; errors here must not
    propagate to the caller.
    """
    rule_map: dict[str, dict] = {}
    try:
        rules = run["tool"]["driver"]["rules"]
        if not isinstance(rules, list):
            return rule_map
        for rule in rules:
            if isinstance(rule, dict) and isinstance(rule.get("id"), str):
                rule_map[rule["id"]] = rule
    except (KeyError, TypeError):
        pass
    return rule_map


def _result_to_violation(result: Any, rule_map: dict[str, dict]) -> Violation | None:
    """Convert one SARIF result to a ``Violation``, or ``None`` to drop it.

    Level resolution (SARIF §3.27.10):
    1. Use ``result.level`` if present.
    2. Fall back to ``rule.defaultConfiguration.level`` from the run's driver rules.
    3. Default to ``"warning"`` (SARIF spec default).
    Normalise to lower-case; drop if level == "none"; treat unknown values as "warning".
    """
    if not isinstance(result, dict):
        return None

    # ---- level resolution ----
    level: str | None = result.get("level")
    if isinstance(level, str):
        level = level.lower()
    else:
        level = None  # absent or wrong type → trigger fallback

    if level is None:
        rule_id_for_lookup = result.get("ruleId")
        if isinstance(rule_id_for_lookup, str) and rule_id_for_lookup in rule_map:
            dc = rule_map[rule_id_for_lookup].get("defaultConfiguration")
            if isinstance(dc, dict):
                dc_level = dc.get("level")
                if isinstance(dc_level, str):
                    level = dc_level.lower()
        if level is None:
            level = _DEFAULT_LEVEL

    if level in _DROP_LEVELS:
        return None  # SARIF level="none" → discard

    if level not in _LEVEL_ORDER:
        level = _DEFAULT_LEVEL  # unknown level string → treat as default

    # ---- raw: shallow copy with resolved level embedded ----
    # We store a shallow copy so that get_sarif_level(v.raw) is a trivial dict
    # lookup; the rule-map traversal already happened above.  All other
    # original fields are preserved verbatim.
    raw: dict = dict(result)
    raw["level"] = level

    # ---- message ----
    msg_obj = result.get("message")
    if isinstance(msg_obj, dict):
        message: str = msg_obj.get("text") or ""
    elif isinstance(msg_obj, str):
        message = msg_obj
    else:
        message = result.get("ruleId") or ""

    # ---- rule ----
    rule_id = result.get("ruleId")
    rule: str | None = str(rule_id) if isinstance(rule_id, str) and rule_id else None

    # ---- path: first physicalLocation.artifactLocation.uri ----
    path: str | None = None
    locations = result.get("locations")
    if isinstance(locations, list) and locations:
        first_loc = locations[0]
        if isinstance(first_loc, dict):
            pl = first_loc.get("physicalLocation")
            if isinstance(pl, dict):
                al = pl.get("artifactLocation")
                if isinstance(al, dict):
                    uri = al.get("uri")
                    if uri and isinstance(uri, str):
                        path = uri

    # ---- remediation: helpUri from rule metadata (optional enrichment) ----
    remediation: str | None = None
    if rule and rule in rule_map:
        rd = rule_map[rule]
        help_uri = rd.get("helpUri")
        if not help_uri:
            help_obj = rd.get("help")
            if isinstance(help_obj, dict):
                help_uri = help_obj.get("uri")
        if help_uri and isinstance(help_uri, str):
            remediation = help_uri

    return Violation(
        message=message,
        rule=rule,
        path=path,
        raw=raw,
        remediation=remediation,
    )


# ---------------------------------------------------------------------------
# compute_result
# ---------------------------------------------------------------------------

def compute_result(
    violations: list[Violation],
    policy: str,
    engine: str,
    *,
    min_level: str = "warning",
) -> EngineVerdict:
    """Filter violations by min_level and return a pass/fail EngineVerdict.

    ``level`` ordering: note (0) < warning (1) < error (2).

    Parameters
    ----------
    violations:
        Output of ``parse_sarif``.  Non-list values are treated as empty.
    policy:
        Policy locator (opaque; passed through to ``EngineVerdict``).
    engine:
        Engine name (passed through to ``EngineVerdict``).
    min_level:
        Minimum SARIF level to keep.  Defaults to ``"warning"`` per CONTRACTS §3.
        Unknown values fall back to ``"warning"`` with a warning log.

    Returns
    -------
    EngineVerdict
        ``failed_`` when at least one kept violation meets the threshold;
        ``passed_`` otherwise.  Returns ``errored`` on any unexpected internal
        error so the caller is never left with an unhandled exception (NFR-2).
    """
    try:
        return _compute_result_inner(violations, policy, engine, min_level=min_level)
    except Exception as exc:  # noqa: BLE001
        return EngineVerdict.errored(engine, policy, f"compute_result internal error: {exc}")


def _compute_result_inner(
    violations: list[Violation],
    policy: str,
    engine: str,
    *,
    min_level: str,
) -> EngineVerdict:
    if not isinstance(violations, list):
        violations = []

    if min_level not in _LEVEL_ORDER:
        log.warning(
            "compute_result(%s): unknown min_level %r; defaulting to 'warning'",
            engine,
            min_level,
        )
        min_level = _DEFAULT_LEVEL

    kept = [
        v
        for v in violations
        if isinstance(v, Violation) and _level_gte(get_sarif_level(v.raw), min_level)
    ]

    # Deterministic ordering (NFR-1): stable sort by (rule, path, message).
    kept.sort(key=lambda v: (v.rule or "", v.path or "", v.message))

    if kept:
        return EngineVerdict.failed_(engine, policy, kept)
    return EngineVerdict.passed_(engine, policy)
