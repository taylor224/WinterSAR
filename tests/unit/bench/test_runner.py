"""bench.runner: run_site with an injected runner, metrics, aggregation, findings."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tests.unit.bench.conftest import failing_runner, injected_fake_runner
from wintersar.bench.profiler import Measurement
from wintersar.bench.runner import (
    RunOutcome,
    aggregate_stages,
    aggregate_total,
    closure_rms,
    compute_metrics,
    fake_engine_runner,
    git_sha,
    gt_geometry,
    machine_info,
    run_site,
    triplets,
    unwrap_error_fraction,
)
from wintersar.bench.sites import Site
from wintersar.i18n import t

GROUND_TRUTH_CSV = (
    Path(__file__).resolve().parents[2] / "fixtures" / "ground_truth" / "leveling_synth.csv"
)


def _meas(wall: float, rss: float = 0.1) -> Measurement:
    return Measurement(wall, 0.0, rss, 0.05, 0, disk_peak_gb=0.001)


def test_run_site_with_injected_runner_writes_result_json(
    tmp_path: Path, synthetic_site: Site
) -> None:
    out = tmp_path / "bench_result.json"
    res = run_site(
        synthetic_site, out, runner=injected_fake_runner, repeats=2, workdir=tmp_path / "w"
    )
    assert res.ok and res.path == out and out.exists(), res.findings
    data = json.loads(out.read_text(encoding="utf-8"))
    assert (
        data["schema_version"] == 1
        and data["repeats"] == 2
        and data["runner"] == "injected_fake_runner"
    )
    assert data["site"]["name"] == "S_synthetic" and data["wintersar_version"]
    assert set(data["machine"]) >= {"cores", "memory_gb", "python", "os"}
    assert data["git_sha"] is None or len(data["git_sha"]) == 40
    assert list(data["stages"]) == list(synthetic_site.stages)
    for s in data["stages"].values():
        assert s["n_runs"] == 2 and len(s["wall_time_s_runs"]) == 2
        assert s["wall_time_s"] == pytest.approx(float(np.median(s["wall_time_s_runs"])))
        assert s["peak_rss_gb"] > 0 and s["disk_peak_gb"] is not None
    assert (
        data["total"]["wall_time_s"] > 0
        and len(data["runs"]) == 2
        and all(r["ok"] for r in data["runs"])
    )
    assert np.isfinite(data["metrics"]["closure_rms"]) and data["metrics"]["closure_rms"] > 0
    assert data["metrics"]["unwrap_error_fraction"] == 0.0  # fake unwrap returns the truth
    assert "compare" not in data and data["findings"] == []
    # home directory masked in the JSON (rule 11.11)
    assert str(Path.home()) not in out.read_text(encoding="utf-8")


def test_shipped_fake_engine_runner(tmp_path: Path, synthetic_site: Site) -> None:
    outcome = fake_engine_runner(synthetic_site, tmp_path, 0)
    assert outcome.ok and set(outcome.stages) == set(synthetic_site.stages)
    assert {"igrams", "unw", "timeseries", "velocity"} <= set(outcome.artifacts)
    assert outcome.total is not None and outcome.total.wall_time_s > 0
    m = compute_metrics(outcome.artifacts, synthetic_site)
    assert m["closure_rms"] is not None and m["unwrap_error_fraction"] == 0.0


def test_template_and_network_sites_are_refused(tmp_path: Path, sites_dir: Path) -> None:
    from wintersar.bench.sites import load_site

    s = load_site(sites_dir / "S.yaml")
    res = run_site(s, tmp_path / "r.json", runner=injected_fake_runner)
    ids = [f.rule_id for f in res.findings]
    assert not res.ok and "BENCH-004" in ids and "BENCH-002" in ids
    assert res.data["stages"] == {} and (tmp_path / "r.json").exists()
    net = Site.model_validate({"name": "n", "size": "S", "aoi": "/a.geojson", "network": True})
    res2 = run_site(net, None, runner=injected_fake_runner)
    assert [f.rule_id for f in res2.findings] == ["BENCH-002"] and res2.path is None
    res3 = run_site(net, None, runner=failing_runner, allow_network=True, repeats=1)
    assert next(f.rule_id for f in res3.findings) == "BENCH-005"


def test_failed_run_reports_bench_005(tmp_path: Path, synthetic_site: Site) -> None:
    res = run_site(synthetic_site, tmp_path / "f.json", runner=failing_runner, repeats=3)
    f = [x for x in res.findings if x.rule_id == "BENCH-005"]
    assert len(f) == 1 and f[0].params["stage"] == "unwrap" and f[0].params["repeat"] == 1
    assert not res.ok and len(res.data["runs"]) == 1  # stops at the first failure


def test_metric_unavailable_and_budget_findings(tmp_path: Path, synthetic_site: Site) -> None:
    site = synthetic_site.model_copy(
        update={"metrics": ["closure_rms", "gt_rmse"], "max_wall_time_s": 1e-6}
    )
    res = run_site(site, None, runner=injected_fake_runner, repeats=1)
    ids = {f.rule_id for f in res.findings}
    assert "BENCH-006" in ids and "BENCH-007" in ids and res.ok  # INFO/WARN only
    assert res.metrics["gt_rmse"] is None


def test_gt_rmse_matches_the_validate_stage_on_the_same_timeseries(
    tmp_path: Path, synthetic_site: Site
) -> None:
    """gt_rmse must use validate's reader/geometry: the fake-engine .npz has no lat/lon,
    incidence or heading, and io.formats' own synthetic grid puts the levelling sites off
    the raster (bench reported None while the pipeline validate stage reported an RMSE)."""
    from wintersar.validate.api import load_timeseries
    from wintersar.validate.ground_truth import load_csv
    from wintersar.validate.metrics import compare

    site = synthetic_site.model_copy(
        update={"metrics": ["gt_rmse"], "ground_truth": str(GROUND_TRUTH_CSV)}
    )
    outcome = injected_fake_runner(site, tmp_path / "w", 0)
    metrics = compute_metrics(outcome.artifacts, site)
    expected = compare(
        load_timeseries(outcome.artifacts["timeseries"], **gt_geometry(site)),
        load_csv(GROUND_TRUTH_CSV),
    )
    assert expected.n_sites > 0 and np.isfinite(expected.rmse_m)
    assert metrics["gt_rmse"] == pytest.approx(float(expected.rmse_m))

    res = run_site(site, tmp_path / "gt.json", runner=injected_fake_runner, repeats=1)
    assert res.ok and [f.rule_id for f in res.findings] == []
    assert res.metrics["gt_rmse"] == pytest.approx(float(expected.rmse_m))


def test_gt_geometry_follows_the_config_the_validate_stage_would_build(tmp_path: Path) -> None:
    """Same values run_validate derives: engine.target_pixel_m, heading from the orbit
    direction, AOI north-west corner (absent for the synthetic empty AOI)."""
    from wintersar.validate.api import SYNTHETIC_PIXEL_M
    from wintersar.validate.los import S1_HEADING_ASC_DEG, S1_HEADING_DESC_DEG

    site = Site(name="s", size="S", synthetic=True)
    assert gt_geometry(site) == {
        "pixel_m": SYNTHETIC_PIXEL_M,
        "heading_deg": S1_HEADING_DESC_DEG,
    }
    aoi = tmp_path / "aoi.geojson"
    aoi.write_text(
        json.dumps(
            {
                "type": "Polygon",
                "coordinates": [[[126.9, 37.5], [127.0, 37.5], [127.0, 37.6], [126.9, 37.6]]],
            }
        ),
        encoding="utf-8",
    )
    real = Site(
        name="r",
        size="S",
        aoi=str(aoi),
        config={"engine": {"target_pixel_m": 80.0}, "data": {"orbit_direction": "asc"}},
    )
    geom = gt_geometry(real)
    assert geom["pixel_m"] == 80.0 and geom["heading_deg"] == S1_HEADING_ASC_DEG
    assert (geom["lat0_deg"], geom["lon0_deg"]) == (37.6, 126.9)


def test_bench_006_reports_why_a_metric_is_missing(tmp_path: Path, synthetic_site: Site) -> None:
    """The reason the metric could not be computed is no longer swallowed by `except Exception`."""
    far = tmp_path / "far.csv"
    far.write_text(
        GROUND_TRUTH_CSV.read_text(encoding="utf-8")
        .replace("37.5", "10.5")
        .replace("126.9", "20.9"),
        encoding="utf-8",
    )
    site = synthetic_site.model_copy(update={"metrics": ["gt_rmse"], "ground_truth": str(far)})
    res = run_site(site, None, runner=injected_fake_runner, repeats=1)
    assert res.metrics["gt_rmse"] is None and res.ok
    f = next(x for x in res.findings if x.rule_id == "BENCH-006")
    assert f.params["metric"] == "gt_rmse" and "n_sites=0" in f.params["reason"]
    assert t("bench.BENCH-006.cause", "en", **f.params).endswith("n_sites=0 n_points=0).")
    # no ground truth at all -> no reason, just the plain INFO
    plain = run_site(
        synthetic_site.model_copy(update={"metrics": ["gt_rmse"]}),
        None,
        runner=injected_fake_runner,
        repeats=1,
    )
    assert next(x for x in plain.findings if x.rule_id == "BENCH-006").params["reason"] == "-"


def test_compare_with_baseline_adds_report_and_regression_findings(
    tmp_path: Path, synthetic_site: Site
) -> None:
    base = run_site(synthetic_site, tmp_path / "base.json", runner=injected_fake_runner, repeats=1)
    slow_base = json.loads((tmp_path / "base.json").read_text())
    for s in slow_base["stages"].values():
        s["wall_time_s"] = 1e-6  # everything current is slower than this baseline
    (tmp_path / "slow.json").write_text(json.dumps(slow_base))
    warn = run_site(
        synthetic_site,
        tmp_path / "cur.json",
        runner=injected_fake_runner,
        repeats=1,
        compare_with=tmp_path / "slow.json",
    )
    assert warn.ok and warn.comparison is not None and not warn.comparison.ok
    assert {f.rule_id for f in warn.findings} == {"BENCH-001"}
    assert all(f.severity == "WARN" for f in warn.findings)
    assert warn.data["compare"]["ok"] is False and warn.data["compare"]["regressions"]
    fail = run_site(
        synthetic_site,
        None,
        runner=injected_fake_runner,
        repeats=1,
        compare_with=slow_base,
        fail_on_regression=True,
    )
    assert not fail.ok and all(f.severity == "FAIL" for f in fail.findings)
    same = run_site(
        synthetic_site,
        None,
        runner=injected_fake_runner,
        repeats=1,
        compare_with=base.data,
        threshold=100.0,
    )
    assert same.ok and same.comparison is not None and same.comparison.ok
    missing = run_site(
        synthetic_site,
        None,
        runner=injected_fake_runner,
        repeats=1,
        compare_with=tmp_path / "nope.json",
    )
    assert [f.rule_id for f in missing.findings] == ["BENCH-003"] and missing.ok


def test_progress_callback_and_runner_by_name(tmp_path: Path, synthetic_site: Site) -> None:
    seen: list[tuple[int, int]] = []
    res = run_site(
        synthetic_site, None, runner="fake", repeats=2, progress=lambda i, n, o: seen.append((i, n))
    )
    assert seen == [(1, 2), (2, 2)] and res.ok and res.data["runner"] == "fake"


# ---------------------------------------------------------------------- metrics


def test_triplets_and_closure_rms() -> None:
    pairs = ["20240101_20240113", "20240113_20240125", "20240101_20240125", "20240125_20240206"]
    assert triplets(pairs) == [(0, 1, 2)]
    rng = np.random.default_rng(0)
    a, b = rng.uniform(-3, 3, (8, 8)), rng.uniform(-3, 3, (8, 8))
    consistent = np.stack([a, b, a + b, rng.uniform(-3, 3, (8, 8))])
    wrapped = np.angle(np.exp(1j * consistent))
    assert closure_rms(wrapped, pairs) == pytest.approx(0.0, abs=1e-6)
    noisy = wrapped.copy()
    noisy[2] = np.angle(np.exp(1j * (noisy[2] + 0.5)))
    assert closure_rms(noisy, pairs) == pytest.approx(0.5, abs=1e-6)
    mask = np.zeros((8, 8), bool)
    mask[:, :4] = True
    assert closure_rms(noisy, pairs, mask) == pytest.approx(0.5, abs=1e-6)
    assert closure_rms(wrapped[:2], pairs[:2]) is None
    assert closure_rms(noisy, pairs, np.ones((8, 8), bool)) is None


def test_unwrap_error_fraction() -> None:
    truth = np.zeros((2, 4, 4))
    unw = truth + 2.0  # constant offset is removed
    assert unwrap_error_fraction(unw, truth) == 0.0
    bad = unw.copy()
    bad[0, 0, :] += 2 * np.pi  # 4 of 16 pixels in pair 0
    assert unwrap_error_fraction(bad, truth) == pytest.approx((4 / 16 + 0) / 2)
    bad[1] = np.nan
    assert unwrap_error_fraction(bad, truth) == pytest.approx(4 / 16)
    assert unwrap_error_fraction(np.full((1, 2, 2), np.nan), np.zeros((1, 2, 2))) is None


def test_aggregation_medians() -> None:
    o1 = RunOutcome(stages={"a": _meas(1.0, 0.1), "b": _meas(5.0)}, total=_meas(6.0))
    o2 = RunOutcome(stages={"a": _meas(3.0, 0.3)}, total=_meas(9.0))
    o3 = RunOutcome(stages={"a": _meas(2.0, 0.2)}, total=_meas(7.0))
    agg = aggregate_stages([o1, o2, o3])
    assert agg["a"]["wall_time_s"] == 2.0 and agg["a"]["peak_rss_gb"] == pytest.approx(0.2)
    assert agg["a"]["n_runs"] == 3 and agg["b"]["n_runs"] == 1 and agg["b"]["wall_time_s"] == 5.0
    assert aggregate_total([o1, o2, o3])["wall_time_s"] == 7.0
    assert aggregate_total([RunOutcome(stages={"a": _meas(1.0)})])["wall_time_s"] == 1.0
    assert aggregate_stages([]) == {}


def test_git_sha_and_machine_info(tmp_path: Path) -> None:
    sha = git_sha()
    assert sha is None or (len(sha) == 40 and all(c in "0123456789abcdef" for c in sha))
    assert git_sha(tmp_path) is None
    info = machine_info()
    assert info["cores"] >= 1 and info["memory_gb"] > 0 and "python" in info
