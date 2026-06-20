"""EXT-RELOAD — hot-reload regression (beyond-spec coverage for periodic refresh).

Not a Section-7 VH-* requirement; this exercises the runtime hot-reload added so
the server can pick up constraint changes without a restart. Proves:
* EXT-RELOAD-1: a change on disk is published as a NEW immutable bundle on
  reload, the latest pointer moves, and the previous version stays retrievable
  (FR-VERSION-3 preserved).
* EXT-RELOAD-2: a reload whose import fails (unresolvable conflict) keeps the
  last-good bundle serving and reports the failure (NFR-2).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import yaml

from ...config import RegistryConfig
from ...service import RegistryService
from ..result import CheckResult

SECTION = "EXT-RELOAD"


def _constraint(cid: str, severity: str, scope: dict) -> dict:
    return {
        "id": cid,
        "title": cid,
        "intent": "reload test constraint",
        "category": "infrastructure",
        "severity": severity,
        "scope": scope,
        "guidance": {"example_compliant": "ok"},
        "owner": "reload-test",
        "version": "1.0.0",
    }


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True))


def run(config: RegistryConfig) -> list[CheckResult]:
    tmp = Path(tempfile.mkdtemp(prefix="cregistry-reload-"))
    try:
        cfg_path = tmp / "registry.config.yaml"
        team_dir = tmp / "sources" / "team"
        _write(team_dir / "constraints" / "a.yaml",
               _constraint("team.s3", "soft", {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]}))
        _write(cfg_path, {"sources": [{"name": "team", "path": "sources/team", "precedence": 50}], "engines": []})

        svc = RegistryService.from_config_path(cfg_path)
        v1 = svc.store.latest().bundle_id
        n1 = len(svc.store.latest().constraints)

        # --- EXT-RELOAD-1: add a constraint, reload, expect a new published bundle.
        _write(team_dir / "constraints" / "b.yaml",
               _constraint("team.gcs", "soft", {"providers": ["gcp"]}))
        st1 = svc.reload()
        v2 = svc.store.latest().bundle_id
        n2 = len(svc.store.latest().constraints)
        old_retained = svc.store.get(v1) is not None

        picked_up = st1["ok"] and st1["changed"] and v2 != v1 and n2 == n1 + 1 and old_retained
        r1 = (
            CheckResult.ok(SECTION, "EXT-RELOAD-1",
                           f"reload published new bundle ({v1[:14]}…->{v2[:14]}…); old version retained")
            if picked_up else
            CheckResult.fail(SECTION, "EXT-RELOAD-1", "hot reload did not pick up change",
                             details=[{"v1": v1, "v2": v2, "n1": n1, "n2": n2, "old_retained": old_retained, "status": st1}])
        )

        # --- EXT-RELOAD-2: introduce an illegal relaxation; reload must keep last-good.
        plat_dir = tmp / "sources" / "platform"
        _write(plat_dir / "constraints" / "p.yaml",
               _constraint("plat.s3", "hard", {"providers": ["aws"], "resource_types": ["aws_s3_bucket"]}))
        _write(cfg_path, {"sources": [
            {"name": "team", "path": "sources/team", "precedence": 50},
            {"name": "platform", "path": "sources/platform", "precedence": 100},
        ], "engines": []})
        st2 = svc.reload()
        latest_after = svc.store.latest().bundle_id

        kept_last_good = (not st2["ok"]) and bool(st2.get("conflicts")) and latest_after == v2
        r2 = (
            CheckResult.ok(SECTION, "EXT-RELOAD-2",
                           "failed reload kept last-good bundle and reported the conflict")
            if kept_last_good else
            CheckResult.fail(SECTION, "EXT-RELOAD-2", "failed reload did not preserve last-good bundle",
                             details=[{"latest_after": latest_after, "expected": v2, "status": st2}])
        )
        return [r1, r2]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
