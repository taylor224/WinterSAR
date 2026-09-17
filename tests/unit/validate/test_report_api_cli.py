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
from wintersar.validate.sweep import DEFAULT_OBJECTIVES

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


def test_npz_heading_is_never_invented(tmp_path, ts_npz, ts_npz_bare, gnss_csv):
    """A fake-engine .npz carries no heading; guessing one silently inverts GNSS east/north."""
    from wintersar.validate.ground_truth import GroundTruthError, load_csv

    bare = api.load_timeseries(ts_npz_bare)
    assert bare.heading_deg is None and "synthetic_heading" not in bare.attrs
    with pytest.raises(GroundTruthError) as exc:  # VAL-008 instead of a wrong-sign comparison
        compare(bare, load_csv(gnss_csv))
    assert exc.value.finding.rule_id == "VAL-008"
    assert exc.value.finding.params["what"] == "heading_deg"
    # a caller-supplied fallback fills it and says so in attrs
    filled = api.load_timeseries(ts_npz_bare, default_heading_deg=-12.0)
    assert filled.heading_deg == -12.0 and filled.attrs["synthetic_heading"] is True
    # what the file knows wins over the fallback, an explicit override wins over the file
    assert api.load_timeseries(ts_npz, default_heading_deg=-12.0).heading_deg == 192.0
    assert "synthetic_heading" not in api.load_timeseries(ts_npz, default_heading_deg=-12.0).attrs
    assert api.load_timeseries(ts_npz, heading_deg=-12.0).heading_deg == -12.0


def test_npz_round_trip_with_io_formats_keeps_metadata(tmp_path, synth_ts):
    """The interchange .npz must mean the same thing to io.formats and to validate (#44)."""
    from wintersar.io import formats

    synth_ts.reference_latlon = (37.51, 127.01)
    synth_ts.attrs = {**synth_ts.attrs, "engine": "dolphin", "units": "m", "source": str(tmp_path)}
    p = formats.write_timeseries_npz(synth_ts, tmp_path / "interchange.npz")
    io_ts = formats.read_timeseries_npz(p)
    val_ts = api.load_timeseries(p)
    assert val_ts.reference_latlon == io_ts.reference_latlon == (37.51, 127.01)
    assert val_ts.attrs["engine"] == "dolphin" and val_ts.attrs["units"] == "m"
    assert val_ts.heading_deg == io_ts.heading_deg == synth_ts.heading_deg
    np.testing.assert_allclose(val_ts.lat2d(), io_ts.lat2d())
    assert val_ts.incidence2d()[0, 0] == pytest.approx(synth_ts.incidence_deg)
    assert str(Path.home()) not in json.dumps(val_ts.attrs, default=str)  # rule 11.11
    assert val_ts.attrs["source"].endswith("interchange.npz")


def test_run_validate_stage_heading_follows_orbit_direction(
    tmp_path, cache_dir, ts_npz_bare, gnss_csv
):
    """data.orbit_direction=auto must not hand the stage a descending heading (VAL-008 >
    a silently inverted GNSS comparison); asc/desc fills it as a flagged fallback."""
    inputs = Artifacts().add(Artifact(name="timeseries", path=ts_npz_bare, kind="npz"))
    gnss = {"gnss": {"source": "csv", "path": str(gnss_csv)}}
    auto = write_fake_config(tmp_path, validate=gnss)
    assert auto.data.orbit_direction == "auto"
    with pytest.raises(StageFailureError) as exc:
        api.run_validate(auto, inputs, {}, tmp_path / "out", tmp_path / "logs")
    assert exc.value.findings[0].rule_id == "VAL-008"
    (tmp_path / "desc").mkdir()
    desc = write_fake_config(tmp_path / "desc", validate=gnss, data={"orbit_direction": "desc"})
    _arts, findings = api.run_validate(desc, inputs, {}, tmp_path / "out2", tmp_path / "logs2")
    assert any(f.rule_id == "VAL-017" and f.severity == "WARN" for f in findings)


def test_cli_closure_missing_inputs_name_the_right_file(tmp_path, igram_stack):
    ig = save_igram_stack(igram_stack, tmp_path / "igrams.npz")
    missing = tmp_path / "missing.npz"
    res = runner.invoke(app, ["--lang", "en", "closure", "--igrams", str(missing)])
    assert res.exit_code == 2 and "Interferogram stack not found" in res.output
    res2 = runner.invoke(
        app, ["--lang", "en", "closure", "--igrams", str(ig), "--unw", str(missing)]
    )
    assert res2.exit_code == 2 and "Unwrapped stack not found" in res2.output


def test_commands_are_module_level_and_registered():
    """Same shape as pipeline/research/select/unwrap: module-level *_cmd + a thin register()."""
    import typer

    from wintersar.validate import cli as vcli

    names = ("validate_cmd", "refpoint_cmd", "sweep_cmd", "closure_cmd")
    assert all(callable(getattr(vcli, n)) for n in names)
    sub = typer.Typer()
    vcli.register(sub)
    assert {c.name for c in sub.registered_commands} == {
        "validate",
        "refpoint",
        "sweep",
        "closure",
    }


def test_cli_sweep_without_ground_truth_still_ranks(tmp_path, cache_dir):
    """Finding 83: gt_rmse is None for every row, so it is dropped from the objectives
    (VAL-018 INFO) instead of silently emptying the Pareto front."""
    cfg = write_fake_config(tmp_path)
    grid = tmp_path / "sweep.yaml"
    grid.write_text("grid:\n  unwrap.coherence_threshold: [0.3, 0.5]\n", encoding="utf-8")
    args = [
        "--json",
        "sweep",
        "--config",
        str(cfg.config_path),
        "--grid",
        str(grid),
        "--out",
        str(tmp_path / "sweep_out"),
        "--set",
        "interferogram.n_dates=5",
        "--set",
        "interferogram.shape=[24,24]",
    ]
    payload = _json(runner.invoke(app, args))
    assert payload["data"]["objectives"] == list(DEFAULT_OBJECTIVES)
    assert payload["data"]["pareto"], "no ground truth must not empty the front"
    info = [f for f in payload["findings"] if f["rule_id"] == "VAL-018"]
    assert len(info) == 1 and info[0]["severity"] == "INFO"
    assert info[0]["params"]["dropped"] == "gt_rmse"
    assert info[0]["params"]["used"] == "closure_rms, wall_time_s"
    md = (tmp_path / "sweep_out" / "sweep.md").read_text(encoding="utf-8")
    assert "gt_rmse" in md.splitlines()[-1]  # the note says which objective was ignored


def test_run_validate_report_language(tmp_path, cache_dir, ts_npz, leveling_csv, monkeypatch):
    """Stage reports follow project.language (repo convention), and an explicit CLI --lang
    wins as soon as the shared CliState can flag one (see the module note on _report_lang)."""
    from wintersar.util.clistate import state

    cfg = write_fake_config(tmp_path, validate={"leveling_csv": str(leveling_csv)})
    assert cfg.project.language == "ko"
    inputs = Artifacts().add(Artifact(name="timeseries", path=ts_npz, kind="npz"))
    monkeypatch.setattr(state, "lang", "en")
    arts, _ = api.run_validate(cfg, inputs, {}, tmp_path / "ko", tmp_path / "log")
    assert "검증" in arts["validation_report"].path.read_text(encoding="utf-8").splitlines()[0]
    monkeypatch.setattr(state, "lang_explicit", True, raising=False)
    arts2, _ = api.run_validate(cfg, inputs, {}, tmp_path / "en", tmp_path / "log")
    first = arts2["validation_report"].path.read_text(encoding="utf-8").splitlines()[0]
    assert "validation report" in first
