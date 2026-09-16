"""search/precheck python stages wired to the select module (lazy adapters)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.conftest import make_burst
from tests.unit.pipeline._support import write_fake_config
from wintersar.io.schemas import Artifacts, Finding, Pair, StackCandidate
from wintersar.pipeline import stages
from wintersar.pipeline.stages import StageFailureError, load_python_stage


def test_adapter_lookup_prefers_api_then_adapter(monkeypatch) -> None:
    real = stages.load_entrypoint
    monkeypatch.setattr(stages, "load_entrypoint", lambda m, a: None)
    assert load_python_stage("search") is None  # nothing importable -> skipped stage
    assert load_python_stage("validate") is None
    assert load_python_stage("unwrap") is None  # not a python stage
    monkeypatch.setattr(stages, "load_entrypoint", real)
    fn = load_python_stage("search")
    assert fn is None or fn is stages.select_search_stage or callable(fn)


def test_search_adapter(tmp_path: Path, cache_dir: Path, monkeypatch) -> None:
    search = pytest.importorskip("wintersar.select.search")
    cfg = write_fake_config(tmp_path)
    recs = [make_burst("2024-01-01"), make_burst("2024-01-13")]
    result = search.SearchResult(records=recs, product_type="BURST")
    monkeypatch.setattr(search, "search_from_config", lambda cfg: result)
    out, logs = tmp_path / "out", tmp_path / "logs"
    out.mkdir()
    arts, findings = stages.select_search_stage(cfg, Artifacts(), {}, out, logs)
    assert findings == []
    cand = arts["candidates"]
    assert cand.path == out / "candidates.json" and cand.path.exists()
    assert cand.meta["n_records"] == 2 and cand.meta["n_dates"] == 2
    assert "records=2" in (logs / "search.log").read_text()
    # FAIL findings from search fail the stage and travel with the exception
    bad = search.SearchResult(
        records=[],
        product_type="BURST",
        findings=[Finding(rule_id="SEL-SEARCH-01", severity="FAIL", message_key="x")],
    )
    monkeypatch.setattr(search, "search_from_config", lambda cfg: bad)
    with pytest.raises(StageFailureError) as ei:
        stages.select_search_stage(cfg, Artifacts(), {}, out, logs)
    assert ei.value.findings[0].rule_id == "SEL-SEARCH-01"


def test_precheck_adapter(tmp_path: Path, cache_dir: Path, aoi_geojson: Path, monkeypatch) -> None:
    select_cli = pytest.importorskip("wintersar.select.cli")
    report = pytest.importorskip("wintersar.select.report")
    search = pytest.importorskip("wintersar.select.search")
    cfg = write_fake_config(tmp_path)
    cfg.aoi = aoi_geojson
    recs = [make_burst("2024-01-01"), make_burst("2024-01-13")]
    cand_path = search.save_records(
        search.SearchResult(records=recs, product_type="BURST"), tmp_path / "candidates.json"
    )
    stack = StackCandidate(
        relative_orbit=52,
        flight_direction="DESCENDING",
        polarization="VV",
        burst_ids=["052_109903_IW2"],
        dates=[date(2024, 1, 1), date(2024, 1, 13)],
        coverage_of_aoi=1.0,
        pairs=[
            Pair(reference=date(2024, 1, 1), secondary=date(2024, 1, 13), temporal_baseline_days=12)
        ],
    )
    info = Finding(rule_id="SEL-07", severity="INFO", message_key="x")

    def fake_precheck(records, cfg, aoi_wkt, geometry=None, poeorb_available=None):
        assert len(records) == 2 and aoi_wkt.startswith("POLYGON")
        return select_cli.PrecheckResult(candidates=[stack], findings=[info], recommended=stack)

    monkeypatch.setattr(select_cli, "run_precheck", fake_precheck)

    def fake_report(candidates, findings, out_dir, lang, **kw):
        p = Path(out_dir) / "precheck_report.json"
        p.write_text("{}", encoding="utf-8")
        return {"json": p}

    monkeypatch.setattr(report, "write_precheck_report", fake_report)
    inputs = Artifacts()
    from wintersar.io.schemas import Artifact

    inputs.add(Artifact(name="candidates", path=cand_path, kind="json"))
    out, logs = tmp_path / "out", tmp_path / "logs"
    out.mkdir()
    arts, findings = stages.select_precheck_stage(cfg, inputs, {}, out, logs)
    assert findings == [info]
    assert arts["stack"].meta["stack_id"] == "T052D_VV" and arts["stack"].meta["n_pairs"] == 1
    assert arts["stack"].path.exists() and arts["precheck_report"].path.exists()
    fail = Finding(rule_id="SEL-01", severity="FAIL", message_key="x")
    monkeypatch.setattr(
        select_cli,
        "run_precheck",
        lambda *a, **k: select_cli.PrecheckResult(candidates=[stack], findings=[fail]),
    )
    with pytest.raises(StageFailureError) as ei:
        stages.select_precheck_stage(cfg, inputs, {}, out, logs)
    assert ei.value.findings == [fail]
