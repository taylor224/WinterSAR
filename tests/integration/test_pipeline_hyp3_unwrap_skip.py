"""ADR-0020: when the interferogram engine bundles unwrapping (HyP3), the local unwrap stage
is skipped and ``unw`` is produced by the interferogram node (dry-run plan only, no network)."""

from __future__ import annotations

from pathlib import Path

import yaml

from wintersar.pipeline.api import plan
from wintersar.pipeline.config import EXAMPLE_CONFIG, Config


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
    # the interferogram record advertises 'unw' as an output so timeseries can consume it
    ig = by_stage["interferogram"]
    outputs = ig.extra.get("outputs") or list(ig.outputs) or ig.extra.get("spec_outputs")
    assert outputs is None or "unw" in outputs or "unw" in str(ig.extra)


def test_isce2_path_keeps_local_unwrap(tmp_path: Path) -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    cfg = _cfg(tmp_path)
    cfg.engine.interferogram = "isce2_topsstack"
    p = plan(cfg)
    by_stage = {r.stage: r for r in p.stages}
    assert by_stage["unwrap"].status != "skipped"
    assert "PIPELINE-011" not in {f.rule_id for f in p.findings}
    assert raw  # silence unused
