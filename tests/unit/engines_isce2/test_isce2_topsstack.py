"""isce2_topsstack adapter: ENV-001, version probe, exact stackSentinel.py flags (ADR-0026),
looks/network derivation, fetch → coregister → interferogram → multilook with injected runners,
failure reporting, resume and update mode (ADR-0029)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.engines_isce2.conftest import (
    DATES,
    PAIRS,
    FakeBurst2Safe,
    FakeStackSentinel,
    make_stack,
)
from wintersar.engines import burst2safe, runfiles
from wintersar.engines import isce2_topsstack as ts
from wintersar.engines.base import EngineNotAvailableError, get_engine
from wintersar.io.schemas import Artifact, Artifacts

pytestmark = pytest.mark.engine


def _engine(**kw: Any) -> ts.Isce2TopsStackEngine:
    return ts.Isce2TopsStackEngine(**kw)


# ------------------------------------------------------------------ metadata / absence


def test_registry_and_metadata() -> None:
    eng = get_engine("isce2_topsstack")
    assert isinstance(eng, ts.Isce2TopsStackEngine)
    assert eng.stages == ("fetch", "coregister", "interferogram", "multilook")
    assert eng.version_constraint == ">=2.6,<3"
    assert "Apache-2.0" in eng.license_note and "topsStack" in eng.install_hint


def test_absent_engine_env_001_and_refuses_to_run(engines_absent: None, tmp_path: Path) -> None:
    eng = _engine()
    assert eng.detect_version() is None
    findings = eng.check_install()
    assert [f.rule_id for f in findings] == ["ENV-001"]
    assert findings[0].params["engine"] == "isce2_topsstack"
    assert not eng.is_available()
    with pytest.raises(EngineNotAvailableError):
        eng.run("fetch", Artifacts(), {"_out_dir": str(tmp_path)}, tmp_path / "logs")


def test_version_probe_is_subprocess_marker_based(isce_present: None) -> None:
    assert (
        ts.parse_isce_version("Using default ISCE Path: /opt/isce\nISCE_VERSION=2.6.3\n") == "2.6.3"
    )
    assert (
        ts.parse_isce_version("Traceback ... ModuleNotFoundError: No module named 'isce'") is None
    )
    eng = _engine(version_probe=lambda: "2.6.3")
    assert eng.detect_version() == "2.6.3" and eng.check_install() == []
    # executable on PATH but 'import isce' fails -> "unknown" + ISCE2-016 hint (still usable)
    eng2 = _engine(version_probe=lambda: None)
    assert eng2.detect_version() == "unknown"
    assert [f.rule_id for f in eng2.check_install()] == ["ISCE2-016"]
    # out-of-range version -> ENV-002 WARN
    eng3 = _engine(version_probe=lambda: "2.5.0")
    assert [f.rule_id for f in eng3.check_install()] == ["ENV-002"]
    # the probe really runs a subprocess without isce installed -> None (never imports isce here)
    assert ts.probe_isce_version(timeout=60) is None


# ------------------------------------------------------------------ argv builder (ADR-0026)


def test_stacksentinel_argv_exact_flags(tmp_path: Path) -> None:
    args = ts.TopsStackArgs(
        slc_dir=tmp_path / "SLC",
        orbit_dir=tmp_path / "orbits",
        aux_dir=tmp_path / "aux",
        work_dir=tmp_path / "wd",
        dem=tmp_path / "dem.wgs84",
        polarization="VV",
        workflow="interferogram",
        swaths=(2,),
        bbox=(37.5, 37.6, 126.9, 127.0),
        reference_date="20240101",
        coregistration="NESD",
        esd_coherence_threshold=0.85,
        num_overlap_connections=3,
        num_connections=2,
        range_looks=9,
        azimuth_looks=3,
        filter_strength=0.6,
        unwrap_method="snaphu",
        rm_filter=True,
        exclude_dates=("20240218",),
        include_dates=("20240101", "20240113"),
        start_date="2024-01-01",
        stop_date="2024-06-30",
        use_gpu=True,
        num_proc=1,
        num_proc4topo=4,
        text_cmd="source ~/.bash_profile;",
        virtual_merge=True,
        snr_misreg_threshold=10,
    )
    argv = args.to_argv("stackSentinel.py")
    assert argv == [
        "stackSentinel.py",
        "-s", str(tmp_path / "SLC"),
        "-o", str(tmp_path / "orbits"),
        "-a", str(tmp_path / "aux"),
        "-w", str(tmp_path / "wd"),
        "-d", str(tmp_path / "dem.wgs84"),
        "-p", "vv",
        "-W", "interferogram",
        "-n", "2",
        "-C", "NESD",
        "-c", "2",
        "-r", "9",
        "-z", "3",
        "-f", "0.6",
        "-u", "snaphu",
        "-b", "37.5 37.6 126.9 127",
        "-m", "20240101",
        "-e", "0.85",
        "-O", "3",
        "--snr_misreg_threshold", "10",
        "-x", "20240218",
        "-i", "20240101,20240113",
        "--start_date", "2024-01-01",
        "--stop_date", "2024-06-30",
        "--rmFilter",
        "--useGPU",
        "--num_proc", "1",
        "--num_proc4topo", "4",
        "-t", "source ~/.bash_profile;",
        "-V", "True",
    ]  # fmt: skip
    # geometry coregistration drops the NESD-only flags
    geo = ts.TopsStackArgs(
        slc_dir=Path("s"),
        orbit_dir=Path("o"),
        aux_dir=Path("a"),
        work_dir=Path("w"),
        dem=Path("d"),
        coregistration="geometry",
    )
    argv = geo.to_argv()
    assert "-e" not in argv and "-O" not in argv and argv[argv.index("-C") + 1] == "geometry"
    assert argv[-4:] == ["--num_proc", "1", "--num_proc4topo", "1"]


@pytest.mark.parametrize(
    "kw",
    [
        {"workflow": "nope"},
        {"coregistration": "esd"},
        {"unwrap_method": "icu2"},
        {"range_looks": 0},
        {"swaths": (4,)},
        {"bbox": (38.0, 37.0, 126.0, 127.0)},
        {"reference_date": "2024-01-01"},
    ],
)
def test_stacksentinel_argv_validation(kw: dict[str, Any]) -> None:
    base = dict(
        slc_dir=Path("s"), orbit_dir=Path("o"), aux_dir=Path("a"), work_dir=Path("w"), dem=Path("d")
    )
    with pytest.raises(ValueError):
        ts.TopsStackArgs(**base, **kw).to_argv()


# ------------------------------------------------------------------ helpers


def test_resolve_looks_explicit_and_auto() -> None:
    assert ts.resolve_looks({"looks": [7, 2]})[0] == (7, 2)
    (rg, az), info = ts.resolve_looks({"looks": "auto", "target_pixel_m": 40.0})
    assert info["mode"] == "auto" and rg >= 1 and az >= 1
    assert abs(info["pixel_m"] - 40.0) < 15.0 and info["aspect_ratio"] <= 1.2
    (rg80, az80), _ = ts.resolve_looks({"target_pixel_m": 80.0})
    assert rg80 * az80 > rg * az


def test_derive_num_connections_and_bbox() -> None:
    stack = make_stack()
    assert ts.derive_num_connections(stack) == 2  # pairs span up to two neighbours
    assert ts.derive_num_connections(None) == ts.DEFAULT_NUM_CONNECTIONS
    assert ts.bbox_snwe({}, stack) == (37.5, 37.6, 126.9, 127.0)
    assert ts.bbox_snwe({"bbox": "1 2 3 4"}, None) == (1.0, 2.0, 3.0, 4.0)
    assert ts.bbox_snwe({"bbox": [1, 2, 3, 4]}, None) == (1.0, 2.0, 3.0, 4.0)
    assert ts.bbox_snwe({}, None) is None
    assert ts.granules_per_date(stack)["2024-01-01"] == [
        "S1_109903_IW2_20240101T092000_VV_AB12-BURST",
        "S1_109904_IW2_20240101T092000_VV_CD34-BURST",
    ]
    assert (
        ts.safe_date(
            Path("S1A_IW_SLC__1SDV_20240101T092000_20240101T092027_051234_062E5F_ABCD.SAFE")
        )
        == "20240101"
    )


def test_prep_isce_listing_matches_mintpy_docs(tmp_path: Path) -> None:
    listing = ts.prep_isce_listing(tmp_path)
    pats = listing["patterns"]
    assert pats["metaFile"] == str(tmp_path / "reference" / "IW*.xml")
    assert pats["unwFile"] == str(tmp_path / "merged" / "interferograms" / "*" / "filt_*.unw")
    assert pats["connCompFile"].endswith("filt_*.unw.conncomp")
    assert pats["demFile"].endswith("merged/geom_reference/hgt.rdr")
    assert pats["incAngleFile"].endswith("los.rdr") and pats["azAngleFile"].endswith("los.rdr")
    assert (
        listing["prep_isce_cmd"][:2] == ["prep_isce.py", "-f"]
        and listing["prep_isce_cmd"][3] == "-m"
    )
    assert listing["baseline_keyword"] == "Bperp (average):"


# ------------------------------------------------------------------ stages


def _params(tmp_path: Path, stage: str, **extra: Any) -> dict[str, Any]:
    out = tmp_path / "work" / stage / "node"
    out.mkdir(parents=True, exist_ok=True)
    p: dict[str, Any] = {
        "_out_dir": str(out),
        "_workdir": str(tmp_path / "work"),
        "_cache_dir": str(tmp_path / "cache"),
        "_cores": 2,
        "_memory_gb": 4.0,
        "_gpu": False,
        "interferogram": "isce2_topsstack",
        "looks": [9, 3],
        "target_pixel_m": 40.0,
        "esd": True,
        "cleanup": "stage",
        "filter": {"type": "goldstein", "alpha": 0.6, "window": 64},
    }
    p.update(extra)
    return p


def _run_fetch(
    tmp_path: Path, stack_json: Path, dem_file: Path, **extra: Any
) -> tuple[ts.Isce2TopsStackEngine, Artifacts]:
    b2s_runner = FakeBurst2Safe()
    eng = _engine(runner=b2s_runner, version_probe=lambda: "2.6.3")
    inputs = Artifacts().add(Artifact(name="stack", path=stack_json, kind="json"))
    arts = eng.run(
        "fetch",
        inputs,
        _params(tmp_path, "fetch", dem=str(dem_file), **extra),
        tmp_path / "logs" / "fetch",
    )
    return eng, arts


def test_fetch_builds_safes_from_granules(
    tmp_path: Path,
    stack_json: Path,
    dem_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    isce_present: None,
) -> None:
    monkeypatch.setattr(burst2safe, "python_module_version", lambda *_a, **_k: "2.0.3")
    monkeypatch.setenv("EARTHDATA_TOKEN", "x" * 24)
    eng, arts = _run_fetch(tmp_path, stack_json, dem_file)
    assert "slc_manifest" in arts
    m = json.loads(arts["slc_manifest"].path.read_text(encoding="utf-8"))
    assert m["dates"] == DATES and len(m["safes"]) == 4
    assert all(Path(p).is_dir() and p.endswith(".SAFE") for p in m["safes"].values())
    assert m["bbox_snwe"] == [37.5, 37.6, 126.9, 127.0]  # from the AOI polygon in stack.json
    assert m["reference_date"] == "20240101" and m["swaths"] == [2] and m["polarization"] == "vv"
    assert (
        m["dem"] == str(dem_file) and Path(m["orbit_dir"]).is_dir() and Path(m["aux_dir"]).is_dir()
    )
    assert "isce2_topsstack" in m["workdir"] and m["stack_id"] == "T052D_VV"
    ids = [f.rule_id for f in eng.findings]
    assert "ISCE2-006" not in ids and "ISCE2-007" not in ids
    assert "ISCE2-014" in ids  # no EOF yet: topsStack fetchOrbit.py fallback
    assert (tmp_path / "logs" / "fetch" / "fetch.findings.json").exists()
    # second fetch: SAFEs are reused (no new burst2safe calls)
    eng2, _ = _run_fetch(tmp_path, stack_json, dem_file)
    assert eng2._runner.calls == []  # type: ignore[attr-defined]


def test_fetch_with_user_slc_dir_and_missing_dem(
    tmp_path: Path, stack_json: Path, isce_present: None
) -> None:
    slc = tmp_path / "myslc"
    slc.mkdir()
    for d in DATES:
        (slc / f"S1A_IW_SLC__1SDV_{d}T092000_{d}T092027_051234_062E5F_ABCD.zip").write_bytes(b"PK")
    eng = _engine(runner=FakeBurst2Safe(), version_probe=lambda: "2.6.3")
    inputs = Artifacts().add(Artifact(name="stack", path=stack_json, kind="json"))
    with pytest.raises(ts.TopsStackError) as ei:
        eng.run(
            "fetch",
            inputs,
            _params(tmp_path, "fetch", slc_dir=str(slc), aoi_wkt=None),
            tmp_path / "logs",
        )
    fails = [f.rule_id for f in ei.value.findings if f.is_fail]
    # sardem is not installed: the DEM module's ENV-006 is forwarded, then ISCE2-002
    assert fails[-1] == "ISCE2-002" and set(fails) <= {"ENV-006", "ISCE2-002"}
    # no SAFE in slc_dir -> ISCE2-007
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ts.TopsStackError) as ei2:
        eng.run("fetch", inputs, _params(tmp_path, "fetch", slc_dir=str(empty)), tmp_path / "logs2")
    assert (
        ei2.value.findings[0].rule_id == "ISCE2-007"
        and ei2.value.findings[0].params["reason"] == "no_safe_in_slc_dir"
    )


def test_fetch_without_granules_or_burst2safe_fails_clearly(
    tmp_path: Path, dem_file: Path, engines_absent: None, isce_present: None
) -> None:
    p = tmp_path / "stack.json"
    p.write_text(make_stack(with_granules=False).model_dump_json(), encoding="utf-8")
    eng = _engine(runner=FakeBurst2Safe(), version_probe=lambda: "2.6.3")
    with pytest.raises(ts.TopsStackError) as ei:
        eng.run(
            "fetch",
            Artifacts().add(Artifact(name="stack", path=p, kind="json")),
            _params(tmp_path, "fetch", dem=str(dem_file)),
            tmp_path / "logs",
        )
    assert ei.value.findings[0].params["reason"] == "no_granules"


def _coregister(
    tmp_path: Path,
    slc_manifest: Path,
    stack_json: Path,
    gen: FakeStackSentinel,
    job_env: dict[str, str],
    **extra: Any,
) -> tuple[ts.Isce2TopsStackEngine, Artifacts]:
    eng = _engine(runner=gen, version_probe=lambda: "2.6.3")
    inputs = Artifacts()
    inputs.add(Artifact(name="slc_manifest", path=slc_manifest, kind="json"))
    inputs.add(Artifact(name="stack", path=stack_json, kind="json"))
    params = _params(tmp_path, "coregister", env={"PATH": job_env["PATH"]}, **extra)
    return eng, eng.run("coregister", inputs, params, tmp_path / "logs" / "coregister")


def _fetched(
    tmp_path: Path, stack_json: Path, dem_file: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setattr(burst2safe, "python_module_version", lambda *_a, **_k: "2.0.3")
    monkeypatch.setenv("EARTHDATA_TOKEN", "x" * 24)
    _, arts = _run_fetch(tmp_path, stack_json, dem_file)
    return arts["slc_manifest"].path


def test_coregister_generates_run_files_and_runs_until_merge(
    tmp_path: Path,
    stack_json: Path,
    dem_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    isce_present: None,
    job_env: dict[str, str],
) -> None:
    slc_manifest = _fetched(tmp_path, stack_json, dem_file, monkeypatch)
    gen = FakeStackSentinel()
    eng, arts = _coregister(tmp_path, slc_manifest, stack_json, gen, job_env, looks="auto")
    assert len(gen.calls) == 1
    argv = gen.calls[0]
    assert argv[0] == "/opt/isce2/topsStack/stackSentinel.py"
    assert (
        argv[argv.index("-i") + 1] == ",".join(DATES) and argv[argv.index("-m") + 1] == "20240101"
    )
    assert argv[argv.index("-c") + 1] == "2" and argv[argv.index("-C") + 1] == "NESD"
    assert argv[argv.index("-f") + 1] == "0.6" and argv[argv.index("--num_proc4topo") + 1] == "2"
    assert argv[argv.index("-b") + 1] == "37.5 37.6 126.9 127"
    m = json.loads(arts["coreg_manifest"].path.read_text(encoding="utf-8"))
    wd = Path(m["workdir"])
    assert m["steps"] == list(runfiles.INTERFEROGRAM_WORKFLOW_STEPS[:3]) + list(
        runfiles.INTERFEROGRAM_WORKFLOW_STEPS[8:]
    )
    assert (
        m["ran"][-1] == "merge_reference_secondary_slc" and "generate_burst_igram" not in m["ran"]
    )
    assert set(m["completed"]) == set(m["ran"]) and len(m["looks"]) == 2
    assert (wd / "merged" / "SLC" / "20240113" / "20240113.slc.full.vrt").exists()
    assert (wd / "merged" / "geom_reference" / "los.rdr").exists()
    assert not (wd / "merged" / "interferograms").exists()
    ids = [f.rule_id for f in eng.findings]
    assert "ISCE2-010" in ids and "ISCE2-013" in ids and "ISCE2-001" not in ids
    logs = tmp_path / "logs" / "coregister"
    assert (logs / "stackSentinel.log").exists() and (
        logs / "run_02_unpack_secondary_slc" / "job_002.log"
    ).exists()
    assert (logs / runfiles.SUMMARY_FILENAME).exists()
    # re-run: nothing pending, stackSentinel not called again (run_files kept, PERF-03/11)
    gen2 = FakeStackSentinel()
    _, arts2 = _coregister(tmp_path, slc_manifest, stack_json, gen2, job_env)
    assert gen2.calls == [] and json.loads(arts2["coreg_manifest"].path.read_text())["ran"] == []


def test_interferogram_stage_products_manifest_and_cleanup(
    tmp_path: Path,
    stack_json: Path,
    dem_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    isce_present: None,
    job_env: dict[str, str],
) -> None:
    slc_manifest = _fetched(tmp_path, stack_json, dem_file, monkeypatch)
    _, carts = _coregister(tmp_path, slc_manifest, stack_json, FakeStackSentinel(), job_env)
    eng = _engine(runner=FakeStackSentinel(), version_probe=lambda: "2.6.3")
    inputs = Artifacts().add(carts["coreg_manifest"])
    params = _params(tmp_path, "interferogram", env={"PATH": job_env["PATH"]})
    arts = eng.run("interferogram", inputs, params, tmp_path / "logs" / "interferogram")
    ig = arts["igrams"]
    wd = Path(json.loads(carts["coreg_manifest"].path.read_text())["workdir"])
    assert ig.kind == "dir" and ig.path == wd / "merged" / "interferograms"
    assert ig.meta["processor"] == "isce" and ig.meta["pairs"] == PAIRS and ig.meta["n_pairs"] == 5
    assert (
        ig.meta["dates"] == DATES and ig.meta["looks"] == [9, 3] and ig.meta["unwrapped"] is False
    )
    assert ig.meta["reader"] == "wintersar.io.formats.read_isce_raster"
    assert ig.meta["coreg_slc_dir"] == str(wd / "merged" / "SLC")
    assert "unw" not in arts  # unwrap left to wintersar.unwrap by default
    man = json.loads(arts["igrams_manifest"].path.read_text(encoding="utf-8"))
    first = man["interferograms"][0]
    assert first["pair"] == PAIRS[0]
    assert first["files"]["int"]["exists"] and first["files"]["int"]["xml"].endswith("fine.int.xml")
    assert first["files"]["cor"]["exists"] and not first["files"]["unw"]["exists"]
    assert man["geometry"]["hgt.rdr"]["exists"]
    prep = json.loads(arts["prep_isce"].path.read_text(encoding="utf-8"))
    assert prep["patterns"]["metaFile"] == str(wd / "reference" / "IW*.xml")
    # cleanup 'stage': burst interferograms removed after merge (merged fine.int is real)
    assert not (wd / "interferograms").exists() or not any((wd / "interferograms").iterdir())
    assert (wd / "merged" / "interferograms" / PAIRS[0] / "fine.int").read_bytes() == b"ISCEDATA"
    assert any(f.rule_id == "ISCE2-011" for f in eng.findings)
    # multilook: pass-through with the same artifact + looks meta
    ml = eng.run("multilook", arts, _params(tmp_path, "multilook"), tmp_path / "logs" / "multilook")
    assert (
        ml["igrams"].path == ig.path and ml["igrams"].meta["looks"] == [9, 3] and "prep_isce" in ml
    )
    # unwrap_in_isce -> 'unw' artifact too (resumes: only the unwrap step runs)
    eng3 = _engine(runner=FakeStackSentinel(), version_probe=lambda: "2.6.3")
    arts3 = eng3.run(
        "interferogram",
        inputs,
        {**params, "unwrap_in_isce": True},
        tmp_path / "logs" / "interferogram2",
    )
    assert "unw" in arts3 and arts3["igrams"].meta["unwrapped"] is True
    assert (wd / "merged" / "interferograms" / PAIRS[0] / "filt_fine.unw.conncomp").exists()


def test_failed_step_reports_job_and_log_for_diagnose(
    tmp_path: Path,
    stack_json: Path,
    dem_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    isce_present: None,
    job_env: dict[str, str],
) -> None:
    slc_manifest = _fetched(tmp_path, stack_json, dem_file, monkeypatch)
    gen = FakeStackSentinel(fail_step="average_baseline")
    with pytest.raises(ts.TopsStackError) as ei:
        _coregister(tmp_path, slc_manifest, stack_json, gen, job_env)
    fails = [f for f in ei.value.findings if f.is_fail]
    assert [f.rule_id for f in fails] == ["ISCE2-001"]
    f = fails[0]
    assert (
        f.params["step"] == "average_baseline" and f.params["run_file"] == "run_03_average_baseline"
    )
    assert f.params["job_index"] == 0 and f.params["returncode"] == 1 and f.params["attempts"] == 2
    log = Path(f.params["log"])
    assert (
        log.exists() and log.name == "job_000.log" and "# exit=1" in log.read_text(encoding="utf-8")
    )
    findings_json = tmp_path / "logs" / "coregister" / "coregister.findings.json"
    assert json.loads(findings_json.read_text(encoding="utf-8"))[-1]["rule_id"] == "ISCE2-001"
    # steps before the failure are recorded as completed -> a re-run resumes at the failed step
    wd = Path(json.loads(slc_manifest.read_text())["workdir"])
    assert runfiles.completed_steps(wd) == {"unpack_topo_reference", "unpack_secondary_slc"}
    (wd / "run_files" / "run_03_average_baseline").write_text("true\n", encoding="utf-8")
    _, arts = _coregister(tmp_path, slc_manifest, stack_json, FakeStackSentinel(), job_env)
    m = json.loads(arts["coreg_manifest"].path.read_text())
    assert m["ran"][0] == "average_baseline"


def test_stacksentinel_failure_is_isce2_003(
    tmp_path: Path,
    stack_json: Path,
    dem_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    isce_present: None,
    job_env: dict[str, str],
) -> None:
    slc_manifest = _fetched(tmp_path, stack_json, dem_file, monkeypatch)
    with pytest.raises(ts.TopsStackError) as ei:
        _coregister(tmp_path, slc_manifest, stack_json, FakeStackSentinel(rc=2), job_env)
    f = ei.value.findings[-1]
    assert (
        f.rule_id == "ISCE2-003" and f.params["returncode"] == 2 and Path(f.params["log"]).exists()
    )


def test_update_mode_reuses_geometry_and_regenerates_run_files(
    tmp_path: Path,
    stack_json: Path,
    dem_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    isce_present: None,
    job_env: dict[str, str],
) -> None:
    """PERF-11 / ADR-0029: an existing coreg_secondarys/ + a new date -> ISCE2-008, run_files archived."""
    slc_manifest = _fetched(tmp_path, stack_json, dem_file, monkeypatch)
    _coregister(tmp_path, slc_manifest, stack_json, FakeStackSentinel(), job_env)
    m = json.loads(slc_manifest.read_text(encoding="utf-8"))
    wd = Path(m["workdir"])
    assert (wd / "coreg_secondarys" / "20240113").is_dir()
    m["dates"] = [*DATES, "20240218"]
    m["safes"]["20240218"] = m["safes"]["20240206"].replace("20240206", "20240218")
    slc_manifest.write_text(json.dumps(m), encoding="utf-8")
    gen = FakeStackSentinel(dates=[*DATES, "20240218"])
    eng, arts = _coregister(tmp_path, slc_manifest, stack_json, gen, job_env)
    ids = [f.rule_id for f in eng.findings]
    assert "ISCE2-008" in ids and "ISCE2-015" in ids
    assert len(gen.calls) == 1 and "20240218" in gen.calls[0][gen.calls[0].index("-i") + 1]
    assert (wd / "reference" / "IW2.xml").exists()  # never deleted
    assert any(p.name.startswith("run_files.bak-") for p in wd.iterdir())
    cm = json.loads(arts["coreg_manifest"].path.read_text())
    assert cm["update_mode"] is True and cm["ran"][0] == "unpack_topo_reference"


def test_missing_inputs_are_isce2_012(tmp_path: Path, isce_present: None) -> None:
    eng = _engine(runner=FakeStackSentinel(), version_probe=lambda: "2.6.3")
    for stage, needed in (
        ("coregister", "slc_manifest"),
        ("interferogram", "coreg_manifest"),
        ("multilook", "igrams"),
    ):
        with pytest.raises(ts.TopsStackError) as ei:
            eng.run(stage, Artifacts(), _params(tmp_path, stage), tmp_path / "logs" / stage)
        assert (
            ei.value.findings[0].rule_id == "ISCE2-012"
            and ei.value.findings[0].params["needed"] == needed
        )
    with pytest.raises(ValueError):
        eng.run("unwrap", Artifacts(), {}, tmp_path)


def test_workdir_is_stable_across_params_that_do_not_change_geometry(tmp_path: Path) -> None:
    eng = _engine(version_probe=lambda: "2.6.3")
    stack = make_stack()
    a = eng.topsstack_workdir(
        {"_workdir": str(tmp_path), "looks": [9, 3], "cleanup": "stage"}, stack
    )
    b = eng.topsstack_workdir(
        {"_workdir": str(tmp_path), "looks": [5, 1], "cleanup": "none"}, stack
    )
    c = eng.topsstack_workdir({"_workdir": str(tmp_path), "esd": False}, stack)
    assert (
        a == b
        and a != c
        and a.parent == tmp_path / "isce2_topsstack"
        and a.name.startswith("T052D_VV_")
    )
    assert eng.topsstack_workdir({"workdir": "/x/y"}, stack) == Path("/x/y")


# ------------------------------------------------------------------ canonical stage params


def _cfg(**isce2: Any) -> Any:
    """Minimal ``Config`` whose engine section differs from every adapter default."""
    from wintersar.pipeline.config import Config

    return Config.model_validate(
        {
            "project": {"name": "t", "workdir": "./work"},
            "aoi": "aoi.geojson",
            "time_range": {"start": "2024-01-01", "end": "2024-06-30"},
            "engine": {
                "interferogram": "isce2_topsstack",
                "looks": [5, 1],
                "esd": False,
                "filter": {"type": "none"},
                "isce2": dict(isce2),
            },
        }
    )


def _canonical_params(
    stage: str, tmp_path: Path, overrides: dict[str, Any] | None = None, **isce2: Any
) -> dict[str, Any]:
    """Stage params exactly as the executor builds them (``dag.canonical_params`` + private keys)."""
    from wintersar.pipeline.dag import canonical_params

    p = canonical_params(_cfg(**isce2), stage, overrides)
    out = tmp_path / "work" / stage / "node"
    out.mkdir(parents=True, exist_ok=True)
    p.update(
        {
            "_out_dir": str(out),
            "_workdir": str(tmp_path / "work"),
            "_cache_dir": str(tmp_path / "cache"),
            "_cores": 2,
            "_memory_gb": 4.0,
            "_gpu": False,
        }
    )
    return p


def test_flatten_stage_params_expands_the_nested_config_sections(tmp_path: Path) -> None:
    p = _canonical_params("coregister", tmp_path, slc_dir="/safes", dem="/dem.wgs84")
    assert isinstance(p["engine"], dict) and "isce2" in p["engine"]  # sections stay nested
    flat = ts.flatten_stage_params(p)
    assert flat["slc_dir"] == "/safes" and flat["dem"] == "/dem.wgs84"  # engine.isce2.*
    assert flat["looks"] == [5, 1] and flat["esd"] is False  # engine.*
    assert flat["filter"]["type"] == "none" and flat["target_pixel_m"] == 40.0
    assert flat["_cores"] == 2 and flat["_out_dir"] == p["_out_dir"]  # private keys survive
    # precedence: top-level override > isce2 > engine.isce2 > engine > data
    assert ts.flatten_stage_params({**p, "looks": [9, 3]})["looks"] == [9, 3]
    assert ts.flatten_stage_params({**p, "isce2": {"dem": "/other"}})["dem"] == "/other"
    assert ts.flatten_stage_params({"data": {"polarization": "HH"}})["polarization"] == "HH"
    # the flat shape (tests / wintersar.pipeline.api callers) is unchanged
    assert ts.flatten_stage_params({"looks": [2, 1]}) == {"looks": [2, 1]}


def test_fetch_reads_slc_dir_and_dem_from_the_nested_engine_section(
    tmp_path: Path, stack_json: Path, dem_file: Path, isce_present: None
) -> None:
    """``engine.isce2.{slc_dir,dem}`` must reach fetch through the nested stage mapping.

    ``Config.stage_params("fetch")`` currently carries only the ``data`` section, so on the
    pipeline path the engine options arrive as an override (``--set fetch.engine.isce2.dem=…``);
    adding ``engine`` to the fetch sections is pipeline-owned (reported separately).
    """
    slc = tmp_path / "myslc"
    slc.mkdir()
    for d in DATES:
        (slc / f"S1A_IW_SLC__1SDV_{d}T092000_{d}T092027_051234_062E5F_ABCD.zip").write_bytes(b"PK")
    b2s = FakeBurst2Safe()
    eng = _engine(runner=b2s, version_probe=lambda: "2.6.3")
    params = _canonical_params(
        "fetch",
        tmp_path,
        overrides={"engine": {"isce2": {"slc_dir": str(slc), "dem": str(dem_file)}}},
    )
    assert "slc_dir" not in params and params["engine"]["isce2"]["slc_dir"] == str(slc)
    arts = eng.run(
        "fetch",
        Artifacts().add(Artifact(name="stack", path=stack_json, kind="json")),
        params,
        tmp_path / "logs" / "fetch",
    )
    m = json.loads(arts["slc_manifest"].path.read_text(encoding="utf-8"))
    assert m["slc_dir"] == str(slc) and m["dates"] == DATES and m["dem"] == str(dem_file)
    assert b2s.calls == []  # slc_dir honoured: no burst2safe round trip
    assert [f.rule_id for f in eng.findings if f.is_fail] == []


def test_coregister_reads_looks_esd_and_filter_from_the_nested_engine_section(
    tmp_path: Path,
    stack_json: Path,
    dem_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    isce_present: None,
    job_env: dict[str, str],
) -> None:
    slc_manifest = _fetched(tmp_path, stack_json, dem_file, monkeypatch)
    gen = FakeStackSentinel()
    eng = _engine(runner=gen, version_probe=lambda: "2.6.3")
    params = _canonical_params("coregister", tmp_path)
    params["env"] = {"PATH": job_env["PATH"]}
    inputs = Artifacts()
    inputs.add(Artifact(name="slc_manifest", path=slc_manifest, kind="json"))
    inputs.add(Artifact(name="stack", path=stack_json, kind="json"))
    arts = eng.run("coregister", inputs, params, tmp_path / "logs" / "coregister")
    argv = gen.calls[0]
    assert argv[argv.index("-r") + 1] == "5" and argv[argv.index("-z") + 1] == "1"  # engine.looks
    assert argv[argv.index("-C") + 1] == "geometry"  # engine.esd: false
    assert argv[argv.index("-f") + 1] == "0"  # engine.filter.type: none
    m = json.loads(arts["coreg_manifest"].path.read_text(encoding="utf-8"))
    assert m["looks"] == [5, 1] and m["looks_info"]["mode"] == "explicit"
    assert "ISCE2-010" not in [f.rule_id for f in eng.findings]  # looks are explicit, not auto


def test_coregister_num_connections_comes_from_the_slc_manifest_pairs(
    tmp_path: Path,
    stack_json: Path,
    dem_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    isce_present: None,
    job_env: dict[str, str],
) -> None:
    """The executor forwards only ``slc_manifest``; ``-c`` must still match the selected network."""
    slc_manifest = _fetched(tmp_path, stack_json, dem_file, monkeypatch)
    assert json.loads(slc_manifest.read_text(encoding="utf-8"))["pairs"] == PAIRS
    gen = FakeStackSentinel()
    eng = _engine(runner=gen, version_probe=lambda: "2.6.3")
    inputs = Artifacts().add(Artifact(name="slc_manifest", path=slc_manifest, kind="json"))
    params = _params(tmp_path, "coregister", env={"PATH": job_env["PATH"]})
    arts = eng.run("coregister", inputs, params, tmp_path / "logs" / "coregister")
    argv = gen.calls[0]
    assert (
        argv[argv.index("-c") + 1] == "2"
    )  # pairs span two neighbours, not DEFAULT_NUM_CONNECTIONS
    m = json.loads(arts["coreg_manifest"].path.read_text(encoding="utf-8"))
    assert m["num_connections"] == 2
    info = next(f for f in eng.findings if f.rule_id == "ISCE2-013")
    assert info.params["n"] == 2
    # explicit params still win, and an empty/unknown manifest falls back to the default
    assert ts.num_connections_from_pairs(PAIRS, DATES) == 2
    assert ts.num_connections_from_pairs([], DATES) == ts.DEFAULT_NUM_CONNECTIONS
    assert ts.num_connections_from_pairs(["20240101_20240206"], DATES) == 3
