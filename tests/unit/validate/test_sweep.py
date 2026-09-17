"""Parameter sweep (R-08/R-11, ADR-0044): grid expansion, Pareto front, fake runner,
and the real fake-engine pipeline with cache reuse (Phase 4 DoD)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import yaml

from tests.unit.pipeline._support import SMALL, spy_fake_engine, write_fake_config
from wintersar.pipeline import api as pipeline_api
from wintersar.validate.ground_truth import load_csv
from wintersar.validate.sweep import (
    METRIC_KEYS,
    SweepRow,
    apply_point,
    available_objectives,
    expand,
    flatten,
    grid_points,
    load_sweep_yaml,
    make_grid,
    metrics_from_run,
    objectives_finding,
    pareto_front,
    run_sweep,
    to_html_table,
    to_markdown,
    write_sweep,
)

GRID = {
    "grid": {
        "unwrap.coherence_threshold": [0.3, 0.5],
        "engine.filter.alpha": [0.4, 0.6],
        "timeseries.troposphere": ["era5", "none"],
    },
    "objectives": ["gt_rmse", "wall_time_s"],
}


def _grid_file(tmp_path: Path, data: dict[str, Any] | None = None) -> Path:
    p = tmp_path / "sweep.yaml"
    p.write_text(yaml.safe_dump(data or GRID), encoding="utf-8")
    return p


def test_make_grid_cartesian_product(tmp_path):
    grid = make_grid(_grid_file(tmp_path))
    assert len(grid) == 8
    assert grid[0] == {
        "unwrap": {"coherence_threshold": 0.3},
        "engine": {"filter": {"alpha": 0.4}},
        "timeseries": {"troposphere": "era5"},
    }
    assert (
        grid[-1]["timeseries"]["troposphere"] == "none"
        and grid[-1]["unwrap"]["coherence_threshold"] == 0.5
    )
    assert len({str(flatten(g)) for g in grid}) == 8
    spec = load_sweep_yaml(_grid_file(tmp_path))
    assert spec.objectives == ["gt_rmse", "wall_time_s"]
    # list values (engine.looks) and a bare top-level grid without the 'grid:' key
    p = _grid_file(
        tmp_path, {"engine.looks": [[4, 1], [8, 2]], "unwrap.method": ["snaphu", "tophu"]}
    )
    g2 = make_grid(p)
    assert len(g2) == 4 and g2[0]["engine"]["looks"] == [4, 1]


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"grid": {}}, "grid"),
        ({"grid": {"nodots": [1]}}, "dotted"),
        ({"grid": {"a.b": []}}, "no values"),
        ([1, 2], "mapping"),
    ],
)
def test_bad_grid_files(tmp_path, data, match):
    with pytest.raises(ValueError, match=match):
        load_sweep_yaml(_grid_file(tmp_path, data))
    with pytest.raises(ValueError, match="file not found"):
        load_sweep_yaml(tmp_path / "missing.yaml")


def test_max_points_guard(tmp_path):
    spec = load_sweep_yaml(_grid_file(tmp_path, {**GRID, "max_points": 4}))
    with pytest.raises(ValueError, match="max_points"):
        grid_points(spec)


def test_flatten_expand_round_trip():
    nested = {"engine": {"filter": {"alpha": 0.4}, "looks": [4, 1]}, "unwrap": {"method": "tophu"}}
    flat = flatten(nested)
    assert flat == {"engine.filter.alpha": 0.4, "engine.looks": [4, 1], "unwrap.method": "tophu"}
    assert expand(flat) == nested


def test_apply_point_validates_and_keeps_original(tmp_path, cache_dir):
    cfg = write_fake_config(tmp_path)
    new = apply_point(
        cfg,
        {
            "unwrap": {"coherence_threshold": 0.5},
            "engine": {"filter": {"alpha": 0.4}, "looks": [4, 1]},
        },
    )
    assert new.unwrap.coherence_threshold == 0.5 and new.engine.filter.alpha == 0.4
    assert new.engine.looks == (4, 1) and new.config_path == cfg.config_path
    assert cfg.unwrap.coherence_threshold == 0.3 and cfg.engine.filter.alpha == 0.6
    assert new.stage_params("unwrap") != cfg.stage_params("unwrap")
    assert new.stage_params("interferogram") != cfg.stage_params("interferogram")
    assert new.workdir == cfg.workdir
    with pytest.raises(ValueError):  # pydantic: extra='forbid'
        apply_point(cfg, {"unwrap": {"nope": 1}})


def _rows() -> list[SweepRow]:
    data = [  # (gt_rmse, wall_time_s)
        (0.010, 10.0),
        (0.008, 20.0),
        (0.012, 5.0),
        (0.008, 30.0),  # dominated by row 1
        (0.020, 40.0),  # dominated by everything
    ]
    return [
        SweepRow(
            index=i,
            params={"k": i},
            metrics={"gt_rmse": a, "wall_time_s": b, "temporal_coherence": 0.9},
        )
        for i, (a, b) in enumerate(data)
    ]


def test_pareto_front():
    rows = _rows()
    front = pareto_front(rows, ["gt_rmse", "wall_time_s"])
    assert [r.index for r in front] == [0, 1, 2]
    # maximise temporal coherence: all tie on it, so only wall time decides
    front2 = pareto_front(rows, {"temporal_coherence": "max", "wall_time_s": "min"})
    assert [r.index for r in front2] == [2]
    rows[1].metrics["gt_rmse"] = None  # missing objective -> excluded; row 3 is now undominated
    assert [r.index for r in pareto_front(rows, ["gt_rmse", "wall_time_s"])] == [0, 2, 3]
    rows[2].ok = False  # failed runs never enter the front
    assert [r.index for r in pareto_front(rows, ["gt_rmse", "wall_time_s"])] == [0, 3]


def test_markdown_and_html_tables(tmp_path):
    rows = _rows()
    md = to_markdown(rows, ["gt_rmse", "wall_time_s"], "en")
    lines = md.splitlines()
    assert lines[0].startswith("| # | parameters |") and "GT RMSE (mm)" in lines[0]
    assert lines[2].endswith("| * | ok |") and lines[6].endswith("|  | ok |")
    assert "10.0" in lines[2]  # gt_rmse in mm
    assert "Pareto front over gt_rmse (min), wall_time_s (min)" in md
    html = to_html_table(rows, ["gt_rmse", "wall_time_s"], "ko")
    assert html.count("<tr>") == 6 and "대조군 RMSE" in html
    paths = write_sweep(rows, tmp_path / "out", ["gt_rmse", "wall_time_s"], "en")
    assert paths["json"].exists() and paths["markdown"].exists()
    try:
        import matplotlib  # noqa: F401

        assert paths["plot"].exists()
    except ImportError:
        assert "plot" not in paths


def test_run_sweep_with_fake_runner(tmp_path, cache_dir):
    cfg = write_fake_config(tmp_path)
    seen: list[tuple[float, float]] = []

    def runner(cfg_i, overrides):
        seen.append((cfg_i.unwrap.coherence_threshold, cfg_i.engine.filter.alpha))
        assert overrides == SMALL
        thr = cfg_i.unwrap.coherence_threshold
        return SimpleNamespace(
            ok=True,
            run_id=f"r{len(seen)}",
            metrics={"gt_rmse": thr / 100, "wall_time_s": 1.0, "closure_rms": 0.0},
        )

    grid = make_grid(_grid_file(tmp_path))
    rows = run_sweep(cfg, grid, runner=runner, param_overrides=SMALL)
    assert len(rows) == 8 and len(seen) == 8
    assert all(r.ok and r.run_id for r in rows)
    assert rows[0].params == {
        "unwrap.coherence_threshold": 0.3,
        "engine.filter.alpha": 0.4,
        "timeseries.troposphere": "era5",
    }
    assert set(rows[0].metrics) == set(METRIC_KEYS) and rows[0].metrics["gt_rmse"] == pytest.approx(
        0.003
    )
    assert rows[0].metrics["residual_rms"] is None
    front = pareto_front(rows, ["gt_rmse", "wall_time_s"])
    assert {r.params["unwrap.coherence_threshold"] for r in front} == {0.3}
    d = rows[0].to_dict()
    assert d["ok"] and d["metrics"]["gt_rmse"] == pytest.approx(0.003)


def test_run_sweep_survives_failing_points(tmp_path, cache_dir):
    cfg = write_fake_config(tmp_path)
    calls = {"n": 0}
    home = str(Path.home())

    def runner(cfg_i, overrides):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"boom {home}/secret")
        return SimpleNamespace(ok=False, error="stage unwrap failed", records=[], artifacts=None)

    rows = run_sweep(
        cfg,
        [{"unwrap": {"coherence_threshold": 0.3}}, {"unwrap": {"coherence_threshold": 0.5}}],
        runner=runner,
    )
    assert [r.ok for r in rows] == [False, False]
    assert rows[0].error.startswith("RuntimeError: boom ~/secret")  # home masked (rule 11.11)
    assert home not in rows[0].error
    assert rows[1].error == "stage unwrap failed"
    assert all(r.findings[0].rule_id == "VAL-016" for r in rows)
    assert pareto_front(rows) == []
    md = to_markdown(rows, None, "en")
    assert md.count("| failed |") == 2


def test_real_fake_pipeline_sweep_reuses_cache(tmp_path, cache_dir, monkeypatch, leveling_csv):
    """Phase 4 DoD: the sweep re-runs only the changed stage and its downstream (PERF-03)."""
    cfg = write_fake_config(tmp_path)
    spy = spy_fake_engine(monkeypatch)
    grid = [
        {"unwrap": {"coherence_threshold": t}, "timeseries": {"troposphere": tr}}
        for t in (0.3, 0.5)
        for tr in ("era5", "none")
    ]
    gt = load_csv(leveling_csv)
    rows = run_sweep(cfg, grid, gt, param_overrides=SMALL)
    assert len(rows) == 4 and all(r.ok for r in rows), [r.error for r in rows]
    for r in rows:
        assert r.run_id
        m = r.metrics
        assert m["closure_rms"] is not None and m["closure_rms"] < 1e-3  # fake unw == truth
        # proxy on the *noisy wrapped* phase (coherence ~0.6): well below 1, still in (0, 1]
        assert m["temporal_coherence"] is not None and 0.0 < m["temporal_coherence"] <= 1.0
        assert m["residual_rms"] is not None and np.isfinite(m["residual_rms"])
        assert m["gt_rmse"] is not None and np.isfinite(m["gt_rmse"])  # geometry synthesised
        assert m["wall_time_s"] is not None and m["wall_time_s"] >= 0
    # first point runs the full engine chain; later points reuse fetch..multilook from the cache
    first_stages = spy.stages[:8]
    assert first_stages == [
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ]
    later = spy.stages[8:]
    assert "interferogram" not in later and "multilook" not in later and "fetch" not in later
    assert rows[0].metrics["cache_hits"] == 0 and all(
        r.metrics["cache_hits"] >= 4 for r in rows[1:]
    )
    # changing only the troposphere (timeseries section) must not re-run unwrap
    assert later[:3] == ["timeseries", "corrections", "geocode"]
    # an identical re-run of the whole grid is fully cached
    spy.clear()
    rows2 = run_sweep(cfg, grid, gt, param_overrides=SMALL)
    assert spy.stages == [] and all(r.metrics["cache_hits"] == 8 for r in rows2)
    # metrics_from_run works directly on a RunResult
    result = pipeline_api.run(apply_point(cfg, grid[0]), param_overrides=SMALL)
    m = metrics_from_run(result, gt)
    assert m["cache_hits"] == 8 and m["gt_rmse"] is not None


def test_pareto_front_ignores_objectives_nothing_could_measure():
    """A sweep without ground truth still has a front: gt_rmse is None everywhere, so it
    cannot rank anything and is dropped (VAL-018) instead of emptying the table."""
    rows = _rows()
    for r in rows:
        r.metrics["gt_rmse"] = None
    used, dropped = available_objectives(rows, ["gt_rmse", "wall_time_s"])
    assert dropped == ["gt_rmse"] and list(used) == ["wall_time_s"]
    assert [r.index for r in pareto_front(rows, ["gt_rmse", "wall_time_s"])] == [2]  # fastest
    f = objectives_finding(rows, ["gt_rmse", "wall_time_s"])
    assert f is not None and f.rule_id == "VAL-018" and f.severity == "INFO"
    assert f.params == {"dropped": "gt_rmse", "used": "wall_time_s"}
    md = to_markdown(rows, ["gt_rmse", "wall_time_s"], "en")
    assert "Pareto front over wall_time_s (min)" in md and "ignored: gt_rmse" in md
    assert md.splitlines()[4].endswith("| * | ok |")  # row 2 marked
    # nothing measurable at all -> no front, and the finding names every objective
    for r in rows:
        r.metrics["wall_time_s"] = None
    assert pareto_front(rows, ["gt_rmse", "wall_time_s"]) == []
    assert objectives_finding(rows, ["gt_rmse", "wall_time_s"]).params["used"] == "-"
    # every run failed: VAL-016 already explains that, nothing is "unavailable"
    for r in rows:
        r.ok = False
    assert objectives_finding(rows, ["gt_rmse"]) is None and pareto_front(rows) == []
