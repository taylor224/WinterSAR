"""dolphin adapter: ENV-001, verified config keys (ADR-0028), CSLC discovery, run via injected
runner, output normalisation to the MintPy/TimeSeries convention (ADR-0029, PERF-05)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from tests.unit.engines_isce2.conftest import DATES, FakeDolphin, write_geotiff
from wintersar.engines import dolphin as dl
from wintersar.engines.base import EngineNotAvailableError, get_engine
from wintersar.io.schemas import Artifact, Artifacts

pytestmark = pytest.mark.engine


def test_registry_and_metadata() -> None:
    eng = get_engine("dolphin")
    assert isinstance(eng, dl.DolphinEngine)
    assert eng.stages == ("timeseries",) and eng.version_constraint == ">=0.40,<1"
    assert "BSD-3-Clause OR Apache-2.0" in eng.license_note
    assert abs(dl.SENTINEL_1_WAVELENGTH_M - 0.05546576) < 1e-7  # c / 5.405 GHz


def test_absent_engine_env_001(engines_absent: None, tmp_path: Path) -> None:
    eng = dl.DolphinEngine()
    assert eng.detect_version() is None
    assert [f.rule_id for f in eng.check_install()] == ["ENV-001"]
    with pytest.raises(EngineNotAvailableError):
        eng.run("timeseries", Artifacts(), {"_out_dir": str(tmp_path)}, tmp_path / "logs")
    with pytest.raises(ValueError):
        eng.run("unwrap", Artifacts(), {}, tmp_path)


def test_build_config_uses_verified_keys(tmp_path: Path) -> None:
    files = [tmp_path / f"{d}.slc.full.vrt" for d in DATES]
    cfg, findings = dl.build_config(
        files,
        tmp_path / "dolphin",
        {
            "subdataset": "/data/VV",
            "ministack_size": 10,
            "half_window": {"x": 11, "y": 5},
            "max_bandwidth": 3,
            "unwrap_method": "tophu",
            "ntiles": [2, 2],
            "n_parallel_tiles": 2,
            "cost": "smooth",
            "init": "mcf",
            "reference_point_rowcol": [10, 20],
            "strides": [2, 2],
            "inversion_method": "L2",
        },
        cores=4,
        gpu=True,
        log_file=tmp_path / "dolphin.log",
    )
    assert cfg["cslc_file_list"] == [str(f) for f in files]
    assert cfg["input_options"] == {
        "cslc_date_fmt": "%Y%m%d",
        "wavelength": dl.SENTINEL_1_WAVELENGTH_M,
        "subdataset": "/data/VV",
    }
    assert (
        cfg["work_directory"] == str(tmp_path / "dolphin") and cfg["keep_paths_relative"] is False
    )
    assert cfg["worker_settings"] == {
        "gpu_enabled": True,
        "threads_per_worker": 4,
        "n_parallel_bursts": 1,
    }
    assert cfg["phase_linking"] == {"ministack_size": 10, "half_window": {"x": 11, "y": 5}}
    assert cfg["interferogram_network"] == {"max_bandwidth": 3}
    assert cfg["unwrap_options"] == {
        "run_unwrap": True,
        "unwrap_method": "snaphu",  # 'tophu' is not an UnwrapMethod value -> DOL-005
        "snaphu_options": {
            "ntiles": [2, 2],
            "n_parallel_tiles": 2,
            "cost": "smooth",
            "init_method": "mcf",
        },
    }
    assert cfg["timeseries_options"] == {
        "run_inversion": True,
        "run_velocity": True,
        "method": "L2",
        "reference_point": [10, 20],
    }
    assert cfg["output_options"] == {"strides": {"x": 2, "y": 2}} and cfg["log_file"] == str(
        tmp_path / "dolphin.log"
    )
    assert [f.rule_id for f in findings] == ["DOL-005"]
    # defaults: no reference point -> DOL-004; unknown keys are not invented
    cfg2, f2 = dl.build_config(files, tmp_path, {}, cores=1)
    assert [f.rule_id for f in f2] == ["DOL-004"]
    assert (
        "phase_linking" not in cfg2
        and "interferogram_network" not in cfg2
        and "output_options" not in cfg2
    )
    assert cfg2["unwrap_options"] == {"run_unwrap": True, "unwrap_method": "snaphu"}
    path = dl.write_config(cfg2, tmp_path / dl.CONFIG_FILENAME)
    assert (
        yaml.safe_load(path.read_text(encoding="utf-8"))["cslc_file_list"] == cfg2["cslc_file_list"]
    )
    assert dl.build_run_argv(path, "dolphin", debug=True) == [
        "dolphin",
        "run",
        str(path),
        "--debug",
    ]


def test_discover_cslc_files_prefers_topsstack_merged_slc(tmp_path: Path) -> None:
    slc = tmp_path / "merged" / "SLC"
    for d in DATES:
        (slc / d).mkdir(parents=True)
        (slc / d / f"{d}.slc.full.vrt").write_text("<VRTDataset/>", encoding="utf-8")
        (slc / d / f"{d}.slc.full").write_bytes(b"\0")
    found = dl.discover_cslc_files(slc)
    assert [p.name for p in found] == [f"{d}.slc.full.vrt" for d in DATES]
    assert dl.discover_cslc_files(tmp_path / "nope") == []


def _params(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    out = tmp_path / "work" / "timeseries" / "node"
    out.mkdir(parents=True, exist_ok=True)
    p: dict[str, Any] = {
        "_out_dir": str(out),
        "_workdir": str(tmp_path / "work"),
        "_cores": 2,
        "_gpu": False,
        "timeseries": {
            "engine": "dolphin",
            "reference_point": "auto_recommend",
            "coherence_threshold": 0.7,
        },
    }
    p.update(extra)
    return p


def test_run_normalises_outputs_to_timeseries_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dl, "find_executable", lambda: "/opt/dolphin/bin/dolphin")
    monkeypatch.setattr(dl, "executable_version", lambda *_a, **_k: "0.42.7")
    slc = tmp_path / "merged" / "SLC"
    for d in DATES:
        (slc / d).mkdir(parents=True)
        (slc / d / f"{d}.slc.full.vrt").write_text("<VRTDataset/>", encoding="utf-8")
    runner = FakeDolphin()
    eng = dl.DolphinEngine(runner=runner)
    assert eng.detect_version() == "0.42.7" and eng.check_install() == []
    inputs = Artifacts().add(
        Artifact(
            name="igrams",
            path=tmp_path / "merged" / "interferograms",
            kind="dir",
            meta={"coreg_slc_dir": str(slc)},
        )
    )
    arts = eng.run("timeseries", inputs, _params(tmp_path), tmp_path / "logs")
    assert runner.calls and runner.calls[0][:2] == ["/opt/dolphin/bin/dolphin", "run"]
    assert runner.config_seen["cslc_file_list"] == [
        str(slc / d / f"{d}.slc.full.vrt") for d in DATES
    ]
    assert runner.config_seen["input_options"]["wavelength"] == pytest.approx(
        dl.SENTINEL_1_WAVELENGTH_M
    )
    ts_art = arts["timeseries"]
    assert (
        ts_art.kind == "npz"
        and ts_art.meta["units"] == "m"
        and ts_art.meta["REF_DATE"] == "20240101"
    )
    z = np.load(ts_art.path)
    assert list(z["dates"]) == ["2024-01-01", "2024-01-13", "2024-01-25", "2024-02-06"]
    disp = z["displacement_m"]
    assert (
        disp.shape == (4, 6, 8)
        and np.all(disp[0] == 0)
        and np.allclose(disp[1], 0.01)
        and np.allclose(disp[3], 0.03)
    )
    assert np.allclose(z["velocity_m_per_yr"], 0.3) and np.allclose(z["coherence"], 0.9)
    assert z["lat"].shape == (6,) and z["lon"].shape == (8,)
    assert z["lon"][0] == pytest.approx(127.0005) and z["lat"][0] == pytest.approx(37.5995)
    assert "dolphin_workdir" in arts and "velocity" in arts and "unw_dolphin" in arts
    ids = [f.rule_id for f in eng.findings]
    assert "DOL-004" in ids and "DOL-006" not in ids and "DOL-001" not in ids
    assert (tmp_path / "logs" / "timeseries.findings.json").exists() and (
        tmp_path / "logs" / "dolphin_run.log"
    ).exists()
    cfg_json = json.loads((Path(_params(tmp_path)["_out_dir"]) / "dolphin_config.json").read_text())
    assert cfg_json["work_directory"].endswith("dolphin")


def test_read_dolphin_timeseries_converts_radians_when_no_wavelength(tmp_path: Path) -> None:
    work = tmp_path / "dolphin"
    (work / "timeseries").mkdir(parents=True)
    dl.write_config(
        {"cslc_file_list": [], "input_options": {"wavelength": None}, "work_directory": str(work)},
        work / dl.CONFIG_FILENAME,
    )
    write_geotiff(
        work / "timeseries" / "20240101_20240113.tif",
        np.full((3, 4), 2.0 * np.pi, dtype=np.float32),
    )
    write_geotiff(
        work / "timeseries" / "20240101_20240125.tif",
        np.full((3, 4), -4.0 * np.pi, dtype=np.float32),
        nodata=-9999.0,
    )
    ts = dl.read_dolphin_timeseries(work)
    assert ts.dates == [date(2024, 1, 1), date(2024, 1, 13), date(2024, 1, 25)]
    # dolphin: metres = -λ/(4π) · radians -> 2π rad = -λ/2 (motion away), -4π rad = +λ (toward)
    assert np.allclose(ts.displacement_m[1], -dl.SENTINEL_1_WAVELENGTH_M / 2)
    assert np.allclose(ts.displacement_m[2], dl.SENTINEL_1_WAVELENGTH_M)
    assert ts.attrs["units_converted_by"] == "wintersar" and ts.attrs["units"] == "m"
    assert ts.velocity_m_per_yr is None and ts.attrs["coords"] == "geographic"
    raw = dl.read_dolphin_timeseries(work, wavelength_m=None)
    assert raw.attrs["units"] == "rad" and np.allclose(raw.displacement_m[1], 2.0 * np.pi)
    with pytest.raises(FileNotFoundError):
        dl.read_dolphin_timeseries(tmp_path / "empty")


def test_read_dolphin_timeseries_pixel_coords_and_latlon_files(tmp_path: Path) -> None:
    work = tmp_path / "dolphin"
    write_geotiff(
        work / "timeseries" / "20240101_20240113.tif", np.ones((3, 4), dtype=np.float32), crs=None
    )
    ts = dl.read_dolphin_timeseries(work, wavelength_m=0.056)
    assert ts.attrs["coords"] == "pixel" and ts.lat.shape == (3,) and ts.lon.shape == (4,)
    lat = write_geotiff(tmp_path / "lat.tif", np.full((3, 4), 37.5, dtype=np.float32), crs=None)
    lon = write_geotiff(tmp_path / "lon.tif", np.full((3, 4), 127.0, dtype=np.float32), crs=None)
    ts2 = dl.read_dolphin_timeseries(work, wavelength_m=0.056, lat_file=lat, lon_file=lon)
    assert ts2.lat.shape == (3, 4) and float(ts2.lon2d()[0, 0]) == 127.0


def test_run_failure_and_missing_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "find_executable", lambda: "/opt/dolphin/bin/dolphin")
    eng = dl.DolphinEngine(runner=FakeDolphin(rc=1))
    with pytest.raises(dl.DolphinRunError) as ei:
        eng.run("timeseries", Artifacts(), _params(tmp_path), tmp_path / "logs")
    assert [f.rule_id for f in ei.value.findings] == ["DOL-002"]
    cslc = [str(tmp_path / f"{d}.tif") for d in DATES]
    with pytest.raises(dl.DolphinRunError) as ei2:
        eng.run("timeseries", Artifacts(), _params(tmp_path, cslc_files=cslc), tmp_path / "logs2")
    f = next(x for x in ei2.value.findings if x.is_fail)
    assert f.rule_id == "DOL-001" and f.params["returncode"] == 1 and Path(f.params["log"]).exists()


def test_run_reads_the_nested_timeseries_dolphin_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``timeseries.dolphin.*`` arrives nested inside the canonical stage mapping (ADR-0034)."""
    from wintersar.pipeline.config import Config
    from wintersar.pipeline.dag import canonical_params

    monkeypatch.setattr(dl, "find_executable", lambda: "/opt/dolphin/bin/dolphin")
    monkeypatch.setattr(dl, "executable_version", lambda *_a, **_k: "0.42.7")
    slc = tmp_path / "cslc"
    for d in DATES:
        (slc / d).mkdir(parents=True)
        (slc / d / f"{d}.slc.full.vrt").write_text("<VRTDataset/>", encoding="utf-8")
    cfg = Config.model_validate(
        {
            "project": {"name": "t", "workdir": "./work"},
            "aoi": "aoi.geojson",
            "time_range": {"start": "2024-01-01", "end": "2024-06-30"},
            "timeseries": {
                "engine": "dolphin",
                "dolphin": {"cslc_dir": str(slc), "ministack_size": 5, "max_bandwidth": 3},
            },
        }
    )
    out = tmp_path / "work" / "timeseries" / "node"
    out.mkdir(parents=True)
    params = canonical_params(cfg, "timeseries", None)
    assert "cslc_dir" not in params and params["timeseries"]["dolphin"]["cslc_dir"] == str(slc)
    params.update({"_out_dir": str(out), "_workdir": str(tmp_path / "work"), "_cores": 2})

    flat = dl.flatten_stage_params(params)
    assert flat["cslc_dir"] == str(slc) and flat["coherence_threshold"] == 0.7
    assert dl.flatten_stage_params({**params, "ministack_size": 9})["ministack_size"] == 9
    assert dl.flatten_stage_params({"cslc_dir": "/x"}) == {"cslc_dir": "/x"}  # flat shape kept

    runner = FakeDolphin()
    eng = dl.DolphinEngine(runner=runner)
    arts = eng.run("timeseries", Artifacts(), params, tmp_path / "logs")
    assert runner.calls  # dolphin was invoked: no DOL-002 'no CSLC inputs'
    assert runner.config_seen["cslc_file_list"] == [
        str(slc / d / f"{d}.slc.full.vrt") for d in DATES
    ]
    assert runner.config_seen["phase_linking"]["ministack_size"] == 5
    assert runner.config_seen["interferogram_network"]["max_bandwidth"] == 3
    assert "timeseries" in arts and [f.rule_id for f in eng.findings if f.is_fail] == []
