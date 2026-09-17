"""ADR-0020: when the interferogram engine bundles unwrapping (HyP3), the local unwrap stage
is skipped and ``unw`` is produced by the interferogram node (dry-run plan only, no network)."""

from __future__ import annotations

from pathlib import Path

import yaml

from wintersar.pipeline.api import plan
from wintersar.pipeline.config import EXAMPLE_CONFIG, Config
from wintersar.pipeline.dag import Dag


def _cfg(tmp_path: Path, **overrides: object) -> Config:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    raw["project"]["workdir"] = str(tmp_path / "work")
    raw["aoi"] = str(tmp_path / "aoi.geojson")
    raw["engine"]["interferogram"] = "hyp3"
    raw["validate"] = {}
    for k, v in overrides.items():
        raw[k] = v
    (tmp_path / "aoi.geojson").write_text(
        '{"type":"Polygon","coordinates":[[[126.9,37.5],[127.0,37.5],[127.0,37.6],[126.9,37.6],[126.9,37.5]]]}'
    )
    return Config.model_validate(raw)


def test_hyp3_path_skips_local_unwrap_and_exposes_unw(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    p = plan(cfg)
    by_stage = {r.stage: r for r in p.stages}
    assert by_stage["unwrap"].status == "skipped"
    ids = {f.rule_id for f in p.findings}
    assert "PIPELINE-011" in ids
    # the interferogram node advertises 'unw' as an output so timeseries can consume it
    # (plan records carry no outputs yet, so the DAG node is what has to be asserted)
    dag = Dag(cfg)
    dag.build()
    assert dag.node("interferogram").spec.outputs == ["igrams", "unw"]
    assert dag.node("timeseries").producers["unw"] == "interferogram"
    assert dag.node("timeseries").producers["igrams"] == "interferogram"


def test_isce2_path_keeps_local_unwrap(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.engine.interferogram = "isce2_topsstack"
    p = plan(cfg)
    by_stage = {r.stage: r for r in p.stages}
    assert by_stage["unwrap"].status != "skipped"
    assert "PIPELINE-011" not in {f.rule_id for f in p.findings}
    dag = Dag(cfg)
    dag.build()
    assert dag.node("interferogram").spec.outputs == ["igrams"]
    assert dag.node("timeseries").producers["unw"] == "unwrap"


def test_example_config_plans_without_blocked_stages(tmp_path: Path) -> None:
    """The shipped config sets validate.leveling_csv, which must not block the plan.

    ``geocode`` belongs to the time-series engine (MintPy); keyed on the interferogram
    engine it was skipped on every real path, ``velocity`` had no producer and every run
    stopped at plan time with a FAIL PIPELINE-002.
    """
    for interferogram in ("hyp3", "isce2_topsstack"):
        cfg = _cfg(tmp_path, validate={"leveling_csv": str(tmp_path / "leveling.csv")})
        cfg.engine.interferogram = interferogram  # type: ignore[assignment]
        p = plan(cfg)
        by_stage = {r.stage: r for r in p.stages}
        assert by_stage["geocode"].status != "skipped", interferogram
        assert by_stage["geocode"].engine == "mintpy"
        assert by_stage["validate"].status != "skipped"
        assert "PIPELINE-002" not in {f.rule_id for f in p.findings}, interferogram
        # ENV-001 (engines not installed here) is expected; no DAG stage may be blocked
        blocking = [f for f in p.findings if f.is_fail and f.rule_id.startswith("PIPELINE-")]
        assert blocking == [], [(f.rule_id, f.params) for f in blocking]
        # the missing ground-truth file is a plan-time WARN, not a late VAL-006 surprise
        assert "PIPELINE-012" in {f.rule_id for f in p.findings}
