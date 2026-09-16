"""MintPy adapter tests: detection, step sequencing via a fake smallbaselineApp.py on PATH,
h5py readers on documented attrs (never imports mintpy). ADR-0021, PERF-09."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from tests.unit.engines_hyp3_mintpy.conftest import make_timeseries_h5
from wintersar.engines import mintpy as mp
from wintersar.engines.base import get_engine
from wintersar.io.schemas import Artifact, Artifacts
from wintersar.io.timeseries import TimeSeries


def _inputs(tmp_path: Path) -> Artifacts:
    data = tmp_path / "hyp3"
    data.mkdir(exist_ok=True)
    meta = {
        "processor": "hyp3",
        "patterns": {
            "unwFile": "*/*/*_unw_phase.tif",
            "corFile": "*/*/*_corr.tif",
            "demFile": "*/*/*_dem.tif",
        },
        "clipped": False,
    }
    return Artifacts().add(Artifact(name="unw", path=data, kind="dir", meta=meta))


def _params(tmp_path: Path, stage: str, **extra: object) -> dict[str, object]:
    return {
        "_out_dir": str(tmp_path / "work" / stage),
        "_cache_dir": str(tmp_path / "cache"),
        "_cores": 8,
        "_memory_gb": 25.6,
        "timeseries": {
            "reference_point": (37.55, 126.95),
            "troposphere": "era5",
            "deramp": "linear",
            "unwrap_error_correction": "phase_closure",
            "coherence_threshold": 0.7,
        },
        **extra,
    }


def test_never_imports_mintpy() -> None:
    assert "mintpy" not in sys.modules
    import re

    for mod in ("mintpy", "mintpy_template"):
        src = Path(mp.__file__).with_name(f"{mod}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import mintpy(\s|$)|from mintpy(\.|\s+import))", src, re.M)


def test_stage_steps_verified() -> None:
    assert mp.STAGE_STEPS["timeseries"] == (
        "load_data",
        "modify_network",
        "reference_point",
        "quick_overview",
        "correct_unwrap_error",
        "invert_network",
    )
    assert mp.STAGE_STEPS["corrections"] == (
        "correct_LOD",
        "correct_SET",
        "correct_ionosphere",
        "correct_troposphere",
        "deramp",
        "correct_topography",
        "residual_RMS",
        "reference_date",
        "velocity",
    )
    assert mp.STAGE_STEPS["geocode"] == ("geocode", "google_earth", "hdfeos5")
    assert get_engine("mintpy").stages == ("timeseries", "corrections", "geocode")


def test_parse_version() -> None:
    assert mp.parse_version("MintPy version 1.6.4, date 2026-07-25") == "1.6.4"
    assert mp.parse_version("MintPy version 1.5.0rc1, date 2023-01-01\nlogo") == "1.5.0rc1"
    assert mp.parse_version("something else") is None


def test_detect_version_missing(no_mintpy_on_path: None) -> None:
    eng = get_engine("mintpy")
    assert eng.detect_version() is None
    assert [f.rule_id for f in eng.check_install()] == ["ENV-001"]
    assert "GPL" in eng.license_note


def test_detect_version_with_fake_exe(fake_mintpy_exe: Path) -> None:
    eng = get_engine("mintpy")
    assert eng.detect_version() == "1.6.4"
    assert eng.check_install() == []


def test_run_three_stages_in_order(tmp_path: Path, fake_mintpy_exe: Path) -> None:
    eng = mp.MintPyEngine()
    logs = tmp_path / "logs"
    # ---- timeseries
    arts = eng.run(
        "timeseries", _inputs(tmp_path), _params(tmp_path, "timeseries"), logs / "timeseries"
    )
    workdir = Path(arts["mintpy_workdir"].path)
    assert workdir == tmp_path / "work" / "timeseries" / "mintpy"
    steps = (workdir / "steps.log").read_text().split()
    assert steps == list(mp.STAGE_STEPS["timeseries"])
    cfg = workdir / mp.TEMPLATE_FILENAME
    assert arts["mintpy_template"].path == cfg
    text = cfg.read_text()
    assert (
        "mintpy.compute.numWorker = 7" in text.replace("  ", " ")
        or "mintpy.compute.numWorker" in text
    )
    entries = json.loads((workdir / mp.TEMPLATE_JSON).read_text())
    assert (
        entries["mintpy.compute.cluster"] == "local" and entries["mintpy.compute.numWorker"] == "7"
    )
    assert entries["mintpy.compute.maxMemory"] == "25.6"
    assert entries["mintpy.load.processor"] == "hyp3" and entries["mintpy.load.unwFile"].endswith(
        "*/*/*_unw_phase.tif"
    )
    assert entries["mintpy.troposphericDelay.weatherDir"] == str(
        (tmp_path / "cache" / "weather" / "ERA5").resolve()
    )
    assert entries["mintpy.reference.lalo"] == "37.550000,126.950000"
    assert (
        arts["timeseries"].path == workdir / "timeseries.h5"
        and arts["timeseries"].meta["FILE_TYPE"] == "timeseries"
    )
    assert {"ifgram_stack", "geometry", "temporal_coherence", "mask"} <= set(arts.items)
    for step in mp.STAGE_STEPS["timeseries"]:
        assert (logs / "timeseries" / f"mintpy_{step}.log").exists()
    stage_log = (logs / "timeseries" / "timeseries.log").read_text()
    assert stage_log.count("STEP ") == 6 and "END mintpy" in stage_log
    manifest = json.loads((tmp_path / "work" / "timeseries" / "mintpy_timeseries.json").read_text())
    assert manifest["steps"] == list(mp.STAGE_STEPS["timeseries"]) and set(
        manifest["timings_s"]
    ) == set(manifest["steps"])
    assert [f.rule_id for f in eng.findings] == []  # reference point pinned -> no MP-006
    # ---- corrections (inherits the work dir through the artifact)
    arts2 = eng.run("corrections", arts, _params(tmp_path, "corrections"), logs / "corrections")
    steps = (workdir / "steps.log").read_text().split()
    assert steps == list(mp.STAGE_STEPS["timeseries"] + mp.STAGE_STEPS["corrections"])
    assert arts2["timeseries"].path == workdir / "timeseries_ERA5_ramp_demErr.h5"
    assert arts2["velocity"].path == workdir / "velocity.h5"
    assert arts2["mintpy_workdir"].path == workdir
    # ---- geocode (inputs already geocoded -> MintPy skips; hdfeos5 skipped when save.hdfEos5=no)
    arts3 = eng.run("geocode", arts2, _params(tmp_path, "geocode"), logs / "geocode")
    steps = (workdir / "steps.log").read_text().split()
    assert steps[-2:] == ["geocode", "google_earth"]
    assert (
        arts3["timeseries"].meta["geocoded"] is True
        and arts3["velocity"].path == workdir / "velocity.h5"
    )
    assert arts3["kmz"].path.name == "geo_velocity.kmz"
    assert arts3["geometry"].path == workdir / "inputs" / "geometryGeo.h5"


def test_run_auto_reference_point_warns(tmp_path: Path, fake_mintpy_exe: Path) -> None:
    eng = mp.MintPyEngine()
    p = _params(tmp_path, "timeseries")
    p["timeseries"] = {"reference_point": "auto_recommend"}
    eng.run("timeseries", _inputs(tmp_path), p, tmp_path / "logs")
    assert [f.rule_id for f in eng.findings] == ["MP-006"] and eng.findings[0].severity == "WARN"
    findings_json = json.loads((tmp_path / "logs" / "timeseries.findings.json").read_text())
    assert findings_json[0]["rule_id"] == "MP-006"


def test_step_failure_raises_mp001(
    tmp_path: Path, fake_mintpy_exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MINTPY_FAIL_STEP", "modify_network")
    eng = mp.MintPyEngine()
    with pytest.raises(mp.MintPyStepError) as ei:
        eng.run("timeseries", _inputs(tmp_path), _params(tmp_path, "timeseries"), tmp_path / "logs")
    err = ei.value
    assert err.step == "modify_network" and err.returncode == 1
    assert err.log_path is not None and "simulated failure" in err.log_path.read_text()
    ids = [f.rule_id for f in err.findings]
    assert ids[0] == "MP-001" and err.findings[0].params["step"] == "modify_network"
    workdir = tmp_path / "work" / "timeseries" / "mintpy"
    assert (workdir / "steps.log").read_text().split() == ["load_data", "modify_network"]  # stopped
    assert (tmp_path / "logs" / "timeseries.findings.json").exists()


def test_oom_is_mp002(
    tmp_path: Path, fake_mintpy_exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MINTPY_OOM_STEP", "invert_network")
    eng = mp.MintPyEngine()
    with pytest.raises(mp.MintPyStepError) as ei:
        eng.run("timeseries", _inputs(tmp_path), _params(tmp_path, "timeseries"), tmp_path / "logs")
    ids = [f.rule_id for f in ei.value.findings]
    assert "MP-002" in ids
    oom = next(f for f in ei.value.findings if f.rule_id == "MP-002")
    assert oom.params["max_memory_gb"] == "25.6" and oom.params["num_worker"] == "7"


def test_corrections_without_workdir_is_mp004(tmp_path: Path, fake_mintpy_exe: Path) -> None:
    eng = mp.MintPyEngine()
    with pytest.raises(mp.MintPyStepError) as ei:
        eng.run("corrections", Artifacts(), _params(tmp_path, "corrections"), tmp_path / "logs")
    assert [f.rule_id for f in ei.value.findings] == ["MP-004"]


def test_timeseries_without_unw_artifact_is_mp003(tmp_path: Path, fake_mintpy_exe: Path) -> None:
    eng = mp.MintPyEngine()
    with pytest.raises(mp.MintPyStepError) as ei:
        eng.run("timeseries", Artifacts(), _params(tmp_path, "timeseries"), tmp_path / "logs")
    assert [f.rule_id for f in ei.value.findings] == ["MP-003"]


def test_run_missing_engine_raises(tmp_path: Path, no_mintpy_on_path: None) -> None:
    from wintersar.engines.base import EngineNotAvailableError

    with pytest.raises(EngineNotAvailableError):
        mp.MintPyEngine().run(
            "timeseries", _inputs(tmp_path), _params(tmp_path, "timeseries"), tmp_path / "logs"
        )
    with pytest.raises(ValueError, match="stage"):
        mp.MintPyEngine().run("unwrap", Artifacts(), {}, tmp_path / "logs")


def test_injected_runner_and_steps_override(tmp_path: Path, fake_mintpy_exe: Path) -> None:
    calls: list[list[str]] = []

    def runner(cmd: list[str], cwd: Path, log: Path) -> int:
        calls.append(cmd)
        log.write_text("ok\n")
        if cmd[cmd.index("--dostep") + 1] == "invert_network":
            make_timeseries_h5(cwd / "timeseries.h5", sidecars=True)
        return 0

    eng = mp.MintPyEngine(runner=runner)
    p = _params(tmp_path, "timeseries", steps=["load_data", "invert_network"])
    arts = eng.run("timeseries", _inputs(tmp_path), p, tmp_path / "logs")
    assert [c[c.index("--dostep") + 1] for c in calls] == ["load_data", "invert_network"]
    assert calls[0][0].endswith("smallbaselineApp.py") and calls[0][1].endswith(
        mp.TEMPLATE_FILENAME
    )
    assert calls[0][calls[0].index("--dir") + 1] == str(arts["mintpy_workdir"].path)
    with pytest.raises(ValueError, match="unknown MintPy steps"):
        eng.run(
            "timeseries",
            _inputs(tmp_path),
            _params(tmp_path, "x", steps=["bogus"]),
            tmp_path / "logs",
        )


def test_missing_expected_output_is_mp007(tmp_path: Path) -> None:
    def runner(cmd: list[str], cwd: Path, log: Path) -> int:
        log.write_text("skip: already processed\n")
        return 0

    eng = mp.MintPyEngine(runner=runner)
    with pytest.raises(mp.MintPyStepError) as ei:
        eng.run(
            "timeseries",
            _inputs(tmp_path),
            _params(tmp_path, "timeseries", steps=["invert_network"]),
            tmp_path / "logs",
        )
    assert [f.rule_id for f in ei.value.findings] == ["MP-007"] and ei.value.returncode == 0


# ------------------------------------------------------------------ parse_log


def test_parse_log_patterns(tmp_path: Path) -> None:
    eng = mp.MintPyEngine()
    log = tmp_path / "a.log"
    log.write_text(
        "# $ smallbaselineApp.py cfg --dostep invert_network\nsome output\nMemoryError: Unable to allocate 12 GiB\n"
    )
    f = eng.parse_log(
        log, entries={"mintpy.compute.maxMemory": "8.0", "mintpy.compute.numWorker": "3"}
    )
    assert [x.rule_id for x in f] == ["MP-002"] and f[0].params["max_memory_gb"] == "8.0"
    log.write_text(
        "# $ smallbaselineApp.py cfg --dostep load_data\nERROR: no unwFile found in /data/*/*unw_phase.tif\n"
    )
    f = eng.parse_log(log, entries={"mintpy.load.unwFile": "/data/*/*unw_phase.tif"})
    assert [x.rule_id for x in f] == ["MP-003"] and f[0].params[
        "pattern"
    ] == "/data/*/*unw_phase.tif"
    log.write_text(
        "# $ smallbaselineApp.py cfg --dostep reference_point\nTraceback (most recent call last):\nValueError: reference pixel is masked\n"
    )
    f = eng.parse_log(log)
    assert [x.rule_id for x in f] == ["MP-001"] and f[0].params["step"] == "reference_point"
    assert eng.parse_log(tmp_path / "missing.log") == []
    log.write_text("all fine\n")
    assert eng.parse_log(log) == []


# ------------------------------------------------------------------ h5py reader


def test_read_timeseries_h5_geocoded(tmp_path: Path) -> None:
    p = make_timeseries_h5(tmp_path / "timeseries.h5", n_dates=4, ny=4, nx=5, sidecars=True)
    ts = mp.read_timeseries_h5(p)
    assert isinstance(ts, TimeSeries)
    assert ts.n_dates == 4 and ts.shape == (4, 5)
    assert ts.dates[0] == date(2024, 1, 1) and ts.dates[-1] == date(2024, 2, 6)
    assert ts.displacement_m.dtype == np.float32
    np.testing.assert_allclose(ts.displacement_m[2], -0.002, atol=1e-7)
    # pixel centres: Y_FIRST + Y_STEP * (row + 0.5) (mintpy.utils.utils0.get_lat_lon)
    np.testing.assert_allclose(ts.lat[:2], [37.6 - 0.0005, 37.6 - 0.0015])
    np.testing.assert_allclose(ts.lon[:2], [126.9 + 0.0005, 126.9 + 0.0015])
    assert ts.lat2d().shape == (4, 5) and ts.lon2d().shape == (4, 5)
    assert ts.heading_deg == pytest.approx(-167.9)
    assert ts.reference_latlon == pytest.approx((37.5985, 126.9025))
    assert ts.velocity_m_per_yr is not None and ts.velocity_m_per_yr.shape == (4, 5)
    assert ts.coherence is not None and float(ts.coherence.mean()) == pytest.approx(0.9)
    assert ts.dem_m is not None and float(ts.dem_m[0, 0]) == 50.0
    inc = ts.incidence2d()
    assert inc is not None and float(inc[0, 0]) == 39.0
    assert (
        ts.attrs["REF_DATE"] == "20240101"
        and ts.attrs["UNIT"] == "m"
        and ts.attrs["FILE_TYPE"] == "timeseries"
    )
    assert ts.attrs["mask"].dtype == bool and ts.attrs["bperp"].shape == (4,)
    assert ts.nearest_pixel(37.5985, 126.9025) == (1, 2)  # REF_Y/REF_X
    assert mp.read_h5_attrs(p)["WIDTH"] == "5"


def test_read_timeseries_h5_radar_coords(tmp_path: Path) -> None:
    p = make_timeseries_h5(tmp_path / "timeseries.h5", geocoded=False)
    with pytest.raises(ValueError, match="radar coordinates"):
        mp.read_timeseries_h5(p)
    make_timeseries_h5(tmp_path / "timeseries.h5", geocoded=False, sidecars=True)
    ts = mp.read_timeseries_h5(p)  # finds inputs/geometryRadar.h5 automatically
    assert ts.lat.ndim == 2 and ts.lat.shape == (4, 5)
    assert float(ts.lat[0, 0]) == pytest.approx(37.5995, abs=1e-5)


def test_read_timeseries_h5_utm(tmp_path: Path) -> None:
    import h5py

    p = make_timeseries_h5(tmp_path / "timeseries.h5", ny=2, nx=2)
    with h5py.File(p, "a") as f:
        f.attrs["X_UNIT"] = "meters"
        f.attrs["Y_UNIT"] = "meters"
        f.attrs["EPSG"] = "32652"
        f.attrs["X_FIRST"] = "300000"
        f.attrs["Y_FIRST"] = "4160000"
        f.attrs["X_STEP"] = "80"
        f.attrs["Y_STEP"] = "-80"
    ts = mp.read_timeseries_h5(p)
    assert (
        ts.lat.shape == (2, 2) and 37 < float(ts.lat[0, 0]) < 38 and 126 < float(ts.lon[0, 0]) < 128
    )


def test_read_timeseries_h5_shape_mismatch(tmp_path: Path) -> None:
    import h5py

    p = tmp_path / "bad.h5"
    with h5py.File(p, "w") as f:
        f.create_dataset("timeseries", data=np.zeros((2, 3, 3), np.float32))
        f.create_dataset("date", data=np.array([b"20240101"]))
        f.attrs["X_FIRST"] = "1"
        f.attrs["Y_FIRST"] = "1"
        f.attrs["X_STEP"] = "1"
        f.attrs["Y_STEP"] = "-1"
    with pytest.raises(ValueError, match="does not match"):
        mp.read_timeseries_h5(p)
