"""Report files, the Python API (incl. the pipeline stage entry point) and the CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from tests.unit.pipeline._support import write_fake_config
from wintersar.cli import app
from wintersar.io.igrams import save_igram_stack
from wintersar.io.schemas import Artifact, Artifacts
from wintersar.pipeline.stages import StageFailureError
from wintersar.validate import api
from wintersar.validate.closure import closure_phase
from wintersar.validate.metrics import compare
from wintersar.validate.refpoint import recommend
from wintersar.validate.report import (
    plots_available,
    render_html,
    render_markdown,
    write_report,
)

runner = CliRunner()


def _json(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# ---------------------------------------------------------------- report


def test_write_report_files(tmp_path, synth_ts, leveling_csv, gnss_csv, igram_stack):
    gt = api.load_ground_truth(leveling_csv, gnss_csv)
    res = compare(synth_ts, gt)
    res.findings.append(api.summary_finding(res))
    closure = closure_phase(igram_stack)
    cands = recommend(synth_ts, top_k=3)
    home = str(Path.home())
    for lang in ("ko", "en"):
        paths = write_report(
            res,
            tmp_path / lang,
            lang,
            source=f"{home}/work/ts.npz",
            closure=closure,
            refpoints=cands,
            sweep_md="| a |\n|---|\n| 1 |",
            sweep_html="<table><tr><td>1</td></tr></table>",
        )
        md = paths["markdown"].read_text(encoding="utf-8")
        html = paths["html"].read_text(encoding="utf-8")
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert "L01-center" in md and "G01" in md and "VAL-009" in md and "VAL-013" in md
        assert home not in md and home not in html and "~/work/ts.npz" in md
        assert "<table" in html and "L01-center" in html and "VAL-009" in html
        assert (
            data["comparison"]["n_sites"] == 5
            and len(data["refpoints"]) == 3
            and data["closure"]["n_triplets"] == closure.n_triplets
        )
        assert ("검증" in md) if lang == "ko" else ("validation report" in md)
        assert "| 1 |" in md and "<table><tr><td>1</td>" in html
        assert "{" not in md.split("##")[1]  # every label formatted
        if plots_available():
            assert paths["plots"].is_dir() and len(list(paths["plots"].glob("site_*.png"))) == 5
            assert "plots/site_L01-center.png" in md and 'src="plots/site_G01.png"' in html
    # no plots / minimal
    md = render_markdown(res, "en")
    assert "## Per-site comparison" in md
    assert "<html" in render_html(res, "en")


# ---------------------------------------------------------------- api


def test_load_timeseries_npz_synthesises_geometry(ts_npz, synth_ts):
    ts = api.load_timeseries(ts_npz)
    assert ts.n_dates == synth_ts.n_dates and ts.shape == synth_ts.shape
    assert (
        ts.attrs["synthetic_geometry"] is True and "~" in ts.attrs["source"]
    ) or "timeseries.npz" in ts.attrs["source"]
    np.testing.assert_allclose(ts.lat2d(), synth_ts.lat2d(), atol=1e-9)
    np.testing.assert_allclose(ts.lon2d(), synth_ts.lon2d(), atol=1e-9)
    assert ts.heading_deg == synth_ts.heading_deg and ts.incidence2d()[0, 0] == pytest.approx(39.0)
    assert ts.velocity_m_per_yr is not None
    ts2 = api.load_timeseries(ts_npz, heading_deg=-12.0, incidence_deg=35.0, lat0_deg=36.0)
    assert (
        ts2.heading_deg == -12.0 and ts2.incidence2d()[0, 0] == 35.0 and ts2.lat2d()[0, 0] == 36.0
    )
    with pytest.raises(api.ValidateError) as exc:
        api.load_timeseries(ts_npz.with_name("missing.npz"))
    assert exc.value.finding.rule_id == "VAL-006"
    bad = ts_npz.with_name("bad.h5")
    bad.write_bytes(b"")
    with pytest.raises(api.ValidateError) as exc:
        api.load_timeseries(bad)
    assert exc.value.finding.rule_id == "VAL-011"
    bad2 = ts_npz.with_name("bad.xyz")  # suffix nobody handles
    bad2.write_bytes(b"")
    with pytest.raises(api.ValidateError) as exc:
        api.load_timeseries(bad2)
    assert exc.value.finding.rule_id == "VAL-011"
    incomplete = ts_npz.with_name("incomplete.npz")
    np.savez(incomplete, dates=np.array(["2024-01-01"]))
    with pytest.raises(api.ValidateError) as exc:
        api.load_timeseries(incomplete)
    assert exc.value.finding.rule_id == "VAL-011"


def test_load_timeseries_npz_with_explicit_grid(tmp_path, synth_ts):
    p = tmp_path / "ts.npz"
    np.savez(
        p,
        dates=np.array([d.strftime("%Y%m%d") for d in synth_ts.dates]),
        displacement_m=synth_ts.displacement_m,
        lat=synth_ts.lat,
        lon=synth_ts.lon,
        incidence_deg=np.full(synth_ts.shape, 41.0),
        coherence=synth_ts.coherence,
        conncomp=synth_ts.conncomp,
        dem_m=synth_ts.dem_m,
    )
    ts = api.load_timeseries(p)
    assert "synthetic_geometry" not in ts.attrs and ts.dates == synth_ts.dates
    assert ts.incidence2d()[3, 3] == 41.0 and ts.coherence is not None and ts.conncomp is not None


def test_validate_timeseries_api(tmp_path, ts_npz, leveling_csv, gnss_csv):
    res, paths = api.validate_timeseries(
        ts_npz, leveling_csv, gnss_csv, tmp_path / "out", "en", plots=False
    )
    assert res.n_sites == 5 and res.rmse_m < 0.003
    assert (
        paths["markdown"].exists()
        and paths["html"].exists()
        and paths["json"].exists()
        and "plots" not in paths
    )
    assert res.findings[-1].rule_id == "VAL-013"
    # default out dir = <ts dir>/validate
    _, paths2 = api.validate_timeseries(ts_npz, leveling_csv, plots=False)
    assert paths2["markdown"].parent == ts_npz.parent / "validate"
    with pytest.raises(api.ValidateError) as exc:
        api.validate_timeseries(ts_npz, None, None, tmp_path)
    assert exc.value.finding.rule_id == "VAL-014"


def test_run_validate_stage(tmp_path, cache_dir, ts_npz, leveling_csv):
    cfg = write_fake_config(tmp_path, validate={"leveling_csv": str(leveling_csv)})
    inputs = (
        Artifacts()
        .add(Artifact(name="velocity", path=tmp_path / "v.npy"))
        .add(Artifact(name="timeseries", path=ts_npz, kind="npz"))
    )
    out, log = tmp_path / "out", tmp_path / "logs"
    arts, findings = api.run_validate(cfg, inputs, {"validate": {"radius_m": 120.0}}, out, log)
    assert "validation_report" in arts and arts["validation_report"].path.exists()
    assert arts["validation_report"].meta["n_sites"] == 3 and arts["validation_json"].path.exists()
    assert any(f.rule_id == "VAL-013" for f in findings) and any(
        f.rule_id == "VAL-009" for f in findings
    )
    assert (log / "validate.log").read_text(encoding="utf-8").startswith("sites=3")
    data = json.loads(arts["validation_json"].path.read_text(encoding="utf-8"))
    assert data["comparison"]["radius_m"] == 120.0
    # without a timeseries artifact: WARN + empty report, no exception
    arts2, findings2 = api.run_validate(
        cfg,
        Artifacts().add(Artifact(name="velocity", path=tmp_path / "v.npy")),
        {},
        tmp_path / "out2",
        log,
    )
    assert findings2[0].rule_id == "VAL-012" and arts2["validation_report"].path.exists()
    # bad CSV -> StageFailureError carrying the finding
    bad = tmp_path / "bad.csv"
    bad.write_text("site_id,lat\nA,1\n", encoding="utf-8")
    (tmp_path / "c2").mkdir()
    cfg2 = write_fake_config(tmp_path / "c2", validate={"leveling_csv": str(bad)})
    with pytest.raises(StageFailureError) as exc:
        api.run_validate(cfg2, inputs, {}, tmp_path / "out3", log)
    assert exc.value.findings[0].rule_id == "VAL-001"


def test_aoi_origin_from_geojson(aoi_geojson, tmp_path):
    assert api._aoi_origin(aoi_geojson) == (37.6, 126.9)
    assert api._aoi_origin(None) is None and api._aoi_origin(tmp_path / "none.json") is None
    empty = tmp_path / "e.json"
    empty.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    assert api._aoi_origin(empty) is None


def test_synthetic_grid_spacing():
    lat, lon = api.synthetic_latlon_grid((3, 4), 37.0, 127.0, 40.0)
    assert lat.shape == (3, 4) and lat[0, 0] == 37.0 and lon[0, 0] == 127.0
    assert lat[1, 0] < lat[0, 0] and lon[0, 1] > lon[0, 0]
    from wintersar.validate.metrics import haversine_m

    assert haversine_m(lat[0, 0], lon[0, 0], lat[1, 0], lon[1, 0]) == pytest.approx(40.0, rel=1e-3)
    assert haversine_m(lat[0, 0], lon[0, 0], lat[0, 1], lon[0, 1]) == pytest.approx(40.0, rel=1e-3)


# ---------------------------------------------------------------- cli


def test_cli_validate_json_and_text(tmp_path, ts_npz, leveling_csv, gnss_csv, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")  # keep rich tables from wrapping cell text
    out = tmp_path / "rep"
    res = runner.invoke(
        app,
        [
            "--json",
            "validate",
            "--ts",
            str(ts_npz),
            "--leveling",
            str(leveling_csv),
            "--gnss",
            str(gnss_csv),
            "--out",
            str(out),
            "--radius",
            "100",
            "--no-plots",
        ],
    )
    payload = _json(res)
    assert payload["ok"] and payload["command"] == "validate"
    assert payload["data"]["n_sites"] == 5 and payload["data"]["rmse_m"] < 0.003
    assert {f["rule_id"] for f in payload["findings"]} == {"VAL-009", "VAL-013"}
    assert (out / "validation_report.md").exists()
    assert "/Users" not in json.dumps(payload["data"]["paths"])
    text = runner.invoke(
        app,
        [
            "--lang",
            "en",
            "validate",
            "--ts",
            str(ts_npz),
            "--leveling",
            str(leveling_csv),
            "--out",
            str(out),
            "--no-plots",
        ],
    )
    assert text.exit_code == 0, text.output
    assert (
        "Ground-truth validation" in text.output
        and "L01-center" in text.output
        and "RMSE" in text.output
    )
    ko = runner.invoke(
        app,
        [
            "validate",
            "--ts",
            str(ts_npz),
            "--leveling",
            str(leveling_csv),
            "--out",
            str(out),
            "--no-plots",
        ],
    )
    assert ko.exit_code == 0 and "대조군 검증" in ko.output


def test_cli_validate_errors(tmp_path, ts_npz, leveling_csv):
    assert (
        runner.invoke(
            app, ["validate", "--ts", str(tmp_path / "no.npz"), "--leveling", str(leveling_csv)]
        ).exit_code
        == 2
    )
    assert (
        runner.invoke(
            app, ["validate", "--ts", str(ts_npz), "--leveling", str(tmp_path / "no.csv")]
        ).exit_code
        == 2
    )
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "site_id,lat,lon,date,up_m,method\nA,37.6,126.9,2024-01-01,15.0,leveling\n",
        encoding="utf-8",
    )
    res = runner.invoke(
        app,
        [
            "--json",
            "validate",
            "--ts",
            str(ts_npz),
            "--leveling",
            str(bad),
            "--out",
            str(tmp_path / "o"),
        ],
    )
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert not payload["ok"] and payload["findings"][0]["rule_id"] == "VAL-007"
    res = runner.invoke(
        app, ["--json", "validate", "--ts", str(ts_npz), "--out", str(tmp_path / "o")]
    )
    assert res.exit_code == 1 and json.loads(res.stdout)["findings"][0]["rule_id"] == "VAL-014"


def test_cli_refpoint(tmp_path, ts_npz, synth_ts, aoi_geojson):
    coh = tmp_path / "coh.npy"
    np.save(coh, synth_ts.coherence)
    out = tmp_path / "ref.json"
    res = runner.invoke(
        app,
        [
            "--json",
            "refpoint",
            "--ts",
            str(ts_npz),
            "--aoi",
            str(aoi_geojson),
            "--top",
            "3",
            "--coherence",
            str(coh),
            "--out",
            str(out),
        ],
    )
    payload = _json(res)
    assert payload["ok"] and len(payload["data"]["candidates"]) == 3
    assert payload["data"]["mintpy"]["threshold"] == 0.85 and payload["data"]["n_aoi_pixels"] > 0
    assert (
        json.loads(out.read_text(encoding="utf-8"))["candidates"][0]["score"]
        == payload["data"]["candidates"][0]["score"]
    )
    text = runner.invoke(
        app,
        [
            "--lang",
            "en",
            "refpoint",
            "--ts",
            str(ts_npz),
            "--top",
            "2",
            "--weights",
            "coherence=0.5,distance=0.5",
        ],
    )
    assert text.exit_code == 0, text.output
    assert (
        "Reference-point recommendation" in text.output
        and "timeseries.reference_point" in text.output
    )
    assert (
        runner.invoke(
            app, ["refpoint", "--ts", str(ts_npz), "--aoi", str(tmp_path / "no.geojson")]
        ).exit_code
        == 2
    )
    assert (
        runner.invoke(app, ["refpoint", "--ts", str(ts_npz), "--weights", "bogus=1"]).exit_code == 2
    )


def test_cli_closure(tmp_path, igram_stack):
    ig = save_igram_stack(igram_stack, tmp_path / "igrams.npz")
    unw = np.asarray(igram_stack.unw).copy()
    unw[2, 4:12, 4:12] += 2 * np.pi
    unw_p = tmp_path / "unw.npz"
    np.savez(unw_p, unw=unw, pairs=np.array(igram_stack.pairs))
    res = runner.invoke(
        app,
        [
            "--json",
            "closure",
            "--igrams",
            str(ig),
            "--unw",
            str(unw_p),
            "--out",
            str(tmp_path / "cl"),
        ],
    )
    payload = _json(res)
    assert payload["data"]["mode"] == "unw" and payload["data"]["suspicious"] == [
        igram_stack.pairs[2]
    ]
    assert (tmp_path / "cl" / "closure_dashboard.json").exists() and (
        tmp_path / "cl" / "closure_maps.npz"
    ).exists()
    text = runner.invoke(app, ["--lang", "en", "closure", "--igrams", str(ig), "--wrapped"])
    assert text.exit_code == 0 and "wrapped closure" in text.output


def test_cli_sweep_on_fake_pipeline(tmp_path, cache_dir, leveling_csv):
    cfg = write_fake_config(tmp_path)
    grid = tmp_path / "sweep.yaml"
    grid.write_text(
        "grid:\n  unwrap.coherence_threshold: [0.3, 0.5]\nobjectives: [gt_rmse, wall_time_s]\n",
        encoding="utf-8",
    )
    out = tmp_path / "sweep_out"
    res = runner.invoke(
        app,
        [
            "--json",
            "sweep",
            "--config",
            str(cfg.config_path),
            "--grid",
            str(grid),
            "--out",
            str(out),
            "--leveling",
            str(leveling_csv),
            "--set",
            "interferogram.n_dates=5",
            "--set",
            "interferogram.shape=[24,24]",
        ],
    )
    payload = _json(res)
    assert payload["ok"] and payload["data"]["n_points"] == 2 and len(payload["data"]["rows"]) == 2
    assert all(r["ok"] for r in payload["data"]["rows"]) and payload["data"]["pareto"]
    assert (out / "sweep.md").exists() and (out / "sweep.json").exists()
    text = runner.invoke(
        app,
        [
            "--lang",
            "en",
            "sweep",
            "--config",
            str(cfg.config_path),
            "--grid",
            str(grid),
            "--out",
            str(out),
            "--set",
            "interferogram.n_dates=5",
            "--set",
            "interferogram.shape=[24,24]",
        ],
    )
    assert text.exit_code == 0, text.output
    assert "Sweep finished: 2/2" in text.output
    bad = tmp_path / "bad.yaml"
    bad.write_text("grid: {}\n", encoding="utf-8")
    res = runner.invoke(
        app, ["--json", "sweep", "--config", str(cfg.config_path), "--grid", str(bad)]
    )
    assert res.exit_code == 1 and json.loads(res.stdout)["findings"][0]["rule_id"] == "VAL-015"
    assert (
        runner.invoke(
            app, ["sweep", "--config", str(cfg.config_path), "--grid", str(tmp_path / "none.yaml")]
        ).exit_code
        == 2
    )
    assert (
        runner.invoke(
            app, ["sweep", "--config", str(tmp_path / "none.yaml"), "--grid", str(grid)]
        ).exit_code
        == 2
    )
