"""snaphu adapter: absence (ENV-001), snaphu-py kwargs mapping (ADR-0023), NaN masking,
config-file generator (SNAPHU keywords), subprocess backend, assemble-only, run() artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from wintersar.engines import snaphu as sn
from wintersar.engines._unwrap_common import (
    EngineRunError,
    TileSpec,
    UnwrapError,
    UnwrapResult,
)
from wintersar.engines.base import EngineNotAvailableError, get_engine
from wintersar.io.igrams import load_igram_stack
from wintersar.io.schemas import Artifact, Artifacts

pytestmark = pytest.mark.engine


def _engine() -> sn.SnaphuEngine:
    eng = get_engine("snaphu")
    assert isinstance(eng, sn.SnaphuEngine)
    return eng


# ------------------------------------------------------------------ absence


def test_absent_engine_reports_env_001_and_refuses_to_run(engines_absent: None, pair) -> None:
    eng = _engine()
    assert eng.detect_version() is None
    findings = eng.check_install()
    assert [f.rule_id for f in findings] == ["ENV-001"]
    assert findings[0].params["install_hint"].startswith("conda install -c conda-forge snaphu")
    assert not eng.is_available()
    with pytest.raises(EngineNotAvailableError):
        eng.unwrap(pair.wrapped, pair.coherence, pair.mask, {})
    with pytest.raises(EngineNotAvailableError):
        eng.run("unwrap", Artifacts(), {}, Path("/nonexistent"))


def test_class_metadata() -> None:
    eng = _engine()
    assert eng.name == "snaphu" and eng.stages == ("unwrap",)
    assert eng.version_constraint == ">=0.4,<1"
    assert "cs2" in eng.license_note and "BSD-3-Clause OR Apache-2.0" in eng.license_note


# ------------------------------------------------------------------ snaphu-py backend


def test_snaphu_py_kwargs_mapping_and_masking(fake_snaphu, pair, tmp_path: Path) -> None:
    eng = _engine()
    assert eng.detect_version() == "0.4.1" and eng.check_install() == []
    params = {
        "unwrap": {
            "cost": "smooth",
            "init": "mst",
            "coherence_threshold": 0.4,
            "tiles": {"rows": 2, "cols": 2, "overlap": 0.25, "min_overlap_px": 2},
            "nproc_per_igram": 3,
        },
        "looks": [2, 2],
        "_scratch_dir": str(tmp_path / "scratch"),
        "_log_path": str(tmp_path / "logs" / "snaphu.log"),
    }
    res = eng.unwrap(pair.wrapped, pair.coherence, pair.mask, params)
    assert isinstance(res, UnwrapResult)
    assert len(fake_snaphu.calls) == 1
    kw = fake_snaphu.calls[0]
    # exact upstream keyword names (snaphu-py v0.4.1 unwrap signature)
    assert set(kw) == {
        "igram",
        "corr",
        "nlooks",
        "cost",
        "init",
        "mask",
        "min_conncomp_frac",
        "phase_grad_window",
        "ntiles",
        "tile_overlap",
        "nproc",
        "tile_cost_thresh",
        "min_region_size",
        "single_tile_reoptimize",
        "regrow_conncomps",
        "scratchdir",
        "delete_scratch",
        "unw",
        "conncomp",
    }
    assert kw["igram"].dtype == np.complex64 and kw["corr"].dtype == np.float32
    assert kw["nlooks"] == 4.0
    assert kw["cost"] == "smooth" and kw["init"] == "mst"
    assert kw["ntiles"] == (2, 2)
    # 40x48 -> tiles 20x24 -> 25% = 5 / 6 px
    assert kw["tile_overlap"] == (5, 6)
    assert kw["nproc"] == 3
    assert kw["tile_cost_thresh"] == 500 and kw["min_region_size"] == 100
    assert kw["single_tile_reoptimize"] is True and kw["regrow_conncomps"] is True
    assert kw["min_conncomp_frac"] == 0.01
    assert kw["scratchdir"] == str(tmp_path / "scratch") and kw["delete_scratch"] is False
    assert kw["unw"].dtype == np.float32 and kw["conncomp"].dtype == np.uint32
    # mask semantics: snaphu-py zeros = masked out; wintersar True = masked out
    expected_masked = pair.mask | (pair.coherence < 0.4) | ~np.isfinite(pair.wrapped)
    assert kw["mask"].dtype == np.bool_
    assert np.array_equal(kw["mask"], ~expected_masked)
    assert kw["igram"][expected_masked].tolist() == [0j] * int(expected_masked.sum())
    # outputs
    assert res.unw.dtype == np.float32 and res.unw.shape == pair.wrapped.shape
    assert np.isnan(res.unw[expected_masked]).all()
    assert np.isfinite(res.unw[~expected_masked]).all()
    assert res.conncomp.dtype == np.uint32
    assert (res.conncomp[expected_masked] == 0).all() and (
        res.conncomp[~expected_masked] == 1
    ).all()
    st = res.stats
    assert st["backend"] == "snaphu-py" and st["wall_time_s"] >= 0
    assert st["ntiles"] == [2, 2] and st["tile_overlap_px"] == [5, 6]
    assert st["tile_dir"] == str(tmp_path / "scratch") and st["assemble_only_capable"] is False
    assert st["nlooks"] == 4.0 and st["nlooks_source"] == "looks"
    assert st["n_conncomp"] == 1 and st["masked_fraction"] == pytest.approx(expected_masked.mean())
    assert "UNW-007" not in st["findings"]
    log = (tmp_path / "logs" / "snaphu.log").read_text()
    assert "UNW-006" not in log


def test_snaphu_py_default_nlooks_warns_and_single_tile_has_no_tile_dir(
    fake_snaphu, pair, tmp_path: Path
) -> None:
    eng = _engine()
    res = eng.unwrap(pair.wrapped, pair.coherence, None, {"_log_path": str(tmp_path / "s.log")})
    kw = fake_snaphu.calls[0]
    assert kw["nlooks"] == 1.0 and kw["ntiles"] == (1, 1) and kw["tile_overlap"] == (0, 0)
    assert kw["cost"] == "defo" and kw["init"] == "mcf"  # UnwrapCfg defaults
    assert kw["delete_scratch"] is True  # temp scratch dir
    assert "tile_dir" not in res.stats
    assert "UNW-007" in res.stats["findings"]
    assert "UNW-007" in (tmp_path / "s.log").read_text()
    assert not Path(kw["scratchdir"]).exists()  # temp scratch cleaned up


def test_snaphu_py_overlap_capping_logged(fake_snaphu, pair, tmp_path: Path) -> None:
    eng = _engine()
    res = eng.unwrap(
        pair.wrapped,
        pair.coherence,
        None,
        {"tiles": {"rows": 2, "cols": 1, "min_overlap_px": 500}, "_log_path": str(tmp_path / "l")},
    )
    assert fake_snaphu.calls[0]["tile_overlap"] == (19, 0)
    assert "UNW-006" in res.stats["findings"]
    assert "UNW-006" in (tmp_path / "l").read_text()


def test_topo_cost_without_executable_raises_unw_004(fake_snaphu, pair) -> None:
    eng = _engine()
    with pytest.raises(EngineRunError) as ei:
        eng.unwrap(pair.wrapped, pair.coherence, None, {"cost": "topo"})
    assert ei.value.finding.rule_id == "UNW-004"
    assert fake_snaphu.calls == []
    with pytest.raises(EngineRunError):
        eng.unwrap(
            pair.wrapped, pair.coherence, None, {"backend": "snaphu-py", "assemble_only": True}
        )


def test_requested_backend_unavailable(fake_snaphu, pair) -> None:
    eng = _engine()
    with pytest.raises(EngineNotAvailableError):
        eng.unwrap(pair.wrapped, pair.coherence, None, {"backend": "subprocess"})
    with pytest.raises(ValueError, match="unknown snaphu backend"):
        eng.select_backend({"backend": "nope"})


# ------------------------------------------------------------------ config generator


def test_config_generator_keywords_single_tile(tmp_path: Path) -> None:
    conf = sn.build_snaphu_config(
        {"cost": "defo", "init": "mcf", "nproc_per_igram": 1},
        tmp_path,
        (100, 250),
        nlooks=9.5,
        tiles=TileSpec(),
        has_mask=True,
    )
    text = conf.render()
    assert text.startswith("#")
    kv = sn.SnaphuConfig.parse(text)
    assert kv["INFILE"] == str(tmp_path / "igram.c64")
    assert kv["INFILEFORMAT"] == "COMPLEX_DATA"
    assert kv["LINELENGTH"] == "250"
    assert kv["CORRFILE"] == str(tmp_path / "corr.f32") and kv["CORRFILEFORMAT"] == "FLOAT_DATA"
    assert kv["OUTFILE"] == str(tmp_path / "unw.f32") and kv["OUTFILEFORMAT"] == "FLOAT_DATA"
    assert kv["CONNCOMPFILE"] == str(tmp_path / "conncomp.u32") and kv["CONNCOMPOUTTYPE"] == "UINT"
    assert kv["BYTEMASKFILE"] == str(tmp_path / "mask.i8")
    assert kv["NCORRLOOKS"] == "9.5"
    assert kv["STATCOSTMODE"] == "DEFO" and kv["INITMETHOD"] == "MCF"
    assert kv["MINCONNCOMPFRAC"] == "0.01"
    assert kv["NTILEROW"] == "1" and kv["NTILECOL"] == "1"
    assert kv["ROWOVRLP"] == "0" and kv["COLOVRLP"] == "0" and kv["NPROC"] == "1"
    assert kv["TILECOSTTHRESH"] == "500" and kv["MINREGIONSIZE"] == "100"
    assert kv["LOGFILE"] == str(tmp_path / "snaphu.log") and kv["VERBOSE"] == "TRUE"
    for absent in ("TILEDIR", "RMTMPTILE", "ASSEMBLEONLY", "COSTOUTFILE", "COSTINFILE"):
        assert absent not in kv


def test_config_generator_tiles_topo_assemble_and_costs(tmp_path: Path) -> None:
    tiles = TileSpec(rows=3, cols=2, overlap_px=(200, 150))
    conf = sn.build_snaphu_config(
        {
            "cost": "topo",
            "init": "mst",
            "nproc_per_igram": 4,
            "tile_cost_thresh": 300,
            "min_region_size": 250,
            "single_tile_reoptimize": False,
            "save_cost_file": True,
            "cost_in_file": "/data/costs.bin",
            "snaphu_extra_config": {"defomax_cycle": "1.2"},
        },
        tmp_path,
        (900, 600),
        nlooks=20,
        tiles=tiles,
        has_mask=False,
    )
    kv = sn.SnaphuConfig.parse(conf.render())
    assert kv["STATCOSTMODE"] == "TOPO" and kv["INITMETHOD"] == "MST"
    assert kv["NTILEROW"] == "3" and kv["NTILECOL"] == "2"
    assert kv["ROWOVRLP"] == "200" and kv["COLOVRLP"] == "150" and kv["NPROC"] == "4"
    assert kv["TILECOSTTHRESH"] == "300" and kv["MINREGIONSIZE"] == "250"
    assert kv["TILEDIR"] == str(tmp_path / "tiles") and kv["RMTMPTILE"] == "FALSE"
    assert kv["SINGLETILEREOPTIMIZE"] == "FALSE"
    assert "ASSEMBLEONLY" not in kv
    assert (
        kv["COSTOUTFILE"] == str(tmp_path / "costs.bin") and kv["COSTINFILE"] == "/data/costs.bin"
    )
    assert kv["DEFOMAX_CYCLE"] == "1.2"
    assert "BYTEMASKFILE" not in kv
    conf2 = sn.build_snaphu_config(
        {"assemble_only": True, "tile_dir": "/prev/tiles", "keep_tile_dir": False},
        tmp_path,
        (10, 10),
        1.0,
        TileSpec(rows=2, cols=2, overlap_px=(2, 2)),
        False,
    )
    kv2 = sn.SnaphuConfig.parse(conf2.render())
    assert kv2["ASSEMBLEONLY"] == "TRUE" and kv2["TILEDIR"] == "/prev/tiles"
    assert kv2["RMTMPTILE"] == "TRUE"
    with pytest.raises(ValueError, match="unsupported cost/init"):
        sn.build_snaphu_config({"cost": "p-norm"}, tmp_path, (1, 1), 1.0, TileSpec(), False)


def test_config_write_and_parse_roundtrip(tmp_path: Path) -> None:
    conf = sn.SnaphuConfig(infile=tmp_path / "a", linelength=7, outfile=tmp_path / "b")
    p = conf.write(tmp_path / "sub" / "snaphu.conf")
    assert p.exists()
    assert sn.SnaphuConfig.parse(p.read_text())["LINELENGTH"] == "7"


# ------------------------------------------------------------------ subprocess backend


def test_subprocess_backend_end_to_end(fake_snaphu_exe: Path, pair, tmp_path: Path) -> None:
    eng = _engine()
    assert eng.detect_version() == "2.0.7"
    findings = eng.check_install()
    assert [f.rule_id for f in findings] == ["UNW-013"]
    assert findings[0].severity == "INFO" and eng.is_available()
    scratch = tmp_path / "scratch"
    params = {
        "cost": "topo",
        "nlooks": 3,
        "tiles": {"rows": 2, "cols": 2, "overlap": 0.25, "min_overlap_px": 2},
        "_scratch_dir": str(scratch),
        "_log_path": str(tmp_path / "logs" / "snaphu.log"),
    }
    res = eng.unwrap(pair.wrapped, pair.coherence, pair.mask, params)
    masked = pair.mask | (pair.coherence < 0.3) | ~np.isfinite(pair.wrapped)
    assert res.unw.dtype == np.float32 and np.isnan(res.unw[masked]).all()
    # the fake executable returns the wrapped phase itself
    np.testing.assert_allclose(res.unw[~masked], pair.wrapped[~masked], atol=1e-5)
    assert res.conncomp.dtype == np.uint32 and (res.conncomp[~masked] == 1).all()
    assert (res.conncomp[masked] == 0).all()
    st = res.stats
    assert st["backend"] == "subprocess" and st["snaphu_executable"] == str(fake_snaphu_exe)
    assert st["tile_dir"] == str(scratch / "tiles") and st["assemble_only_capable"] is True
    assert (scratch / "tiles" / "marker").exists()
    kv = sn.SnaphuConfig.parse((scratch / "snaphu.conf").read_text())
    assert kv["STATCOSTMODE"] == "TOPO" and kv["NCORRLOOKS"] == "3.0"
    assert kv["NTILEROW"] == "2" and kv["ROWOVRLP"] == "5" and kv["COLOVRLP"] == "6"
    assert (scratch / "snaphu.log").exists()  # LOGFILE written by the executable
    assert (scratch / "mask.i8").stat().st_size == pair.wrapped.size
    log = (tmp_path / "logs" / "snaphu.log").read_text()
    assert "snaphu subprocess:" in log and "fake snaphu: shape" in log
    # assemble-only re-run reuses the kept tile dir
    res2 = eng.unwrap(
        pair.wrapped,
        pair.coherence,
        pair.mask,
        {**params, "assemble_only": True, "tile_dir": st["tile_dir"]},
    )
    assert res2.stats["assemble_only"] is True
    assert "assemble-only from" in (tmp_path / "logs" / "snaphu.log").read_text()


def test_subprocess_assemble_only_requires_tile_dir(
    fake_snaphu_exe: Path, pair, tmp_path: Path
) -> None:
    eng = _engine()
    with pytest.raises(EngineRunError) as ei:
        eng.unwrap(
            pair.wrapped,
            pair.coherence,
            None,
            {"assemble_only": True, "tile_dir": str(tmp_path / "missing")},
        )
    assert ei.value.finding.rule_id == "UNW-005"


def test_subprocess_failure_is_engine_run_error_with_log(
    fake_snaphu_exe: Path, pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SNAPHU_FAIL", "1")
    eng = _engine()
    with pytest.raises(EngineRunError) as ei:
        eng.unwrap(
            pair.wrapped,
            pair.coherence,
            None,
            {"backend": "subprocess", "_log_path": str(tmp_path / "snaphu.log")},
        )
    f = ei.value.finding
    assert f.rule_id == "UNW-003" and f.params["returncode"] == 2
    assert "secondary arcs" in f.evidence["stderr_tail"]
    assert "secondary arcs" in (tmp_path / "snaphu.log").read_text()


def test_subprocess_preferred_when_snaphu_py_cannot(
    fake_snaphu, fake_snaphu_exe: Path, pair
) -> None:
    eng = _engine()
    assert eng.select_backend({"cost": "defo"}) == ("snaphu-py", None)
    assert eng.select_backend({"cost": "topo"}) == ("subprocess", fake_snaphu_exe)
    assert eng.select_backend({"save_cost_file": True})[0] == "subprocess"
    assert eng.select_backend({"backend": "subprocess"})[0] == "subprocess"
    res = eng.unwrap(pair.wrapped, pair.coherence, None, {"save_cost_file": True})
    assert res.stats["backend"] == "subprocess" and Path(res.stats["cost_file"]).name == "costs.bin"
    assert fake_snaphu.calls == []


def test_find_executable_precedence(fake_snaphu_exe: Path, tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "other-snaphu"
    other.write_text("#!/bin/sh\n")
    assert sn.find_snaphu_executable({"snaphu_executable": str(other)}) == other
    assert sn.find_snaphu_executable({}) == fake_snaphu_exe
    monkeypatch.delenv("WINTERSAR_SNAPHU_EXE")
    assert sn.find_snaphu_executable({}) is None
    assert sn.parse_snaphu_help_version("usage\nsnaphu v2.0.6\n") == "2.0.6"
    assert sn.parse_snaphu_help_version("nothing") is None
    assert os.environ.get("WINTERSAR_SNAPHU_EXE") is None


# ------------------------------------------------------------------ run()


def test_run_writes_unw_npz_and_stats(fake_snaphu, igrams_npz: Path, tmp_path: Path) -> None:
    eng = _engine()
    out = tmp_path / "unwrap"
    inputs = Artifacts().add(Artifact(name="igrams", path=igrams_npz, kind="npz"))
    params = {"unwrap": {"cost": "smooth", "coherence_threshold": 0.5}, "_out_dir": str(out)}
    arts = eng.run("unwrap", inputs, params, out / "logs")
    assert set(arts.items) == {"unw", "unw_stats"}
    unw_art = arts["unw"]
    assert unw_art.path == out / "unw.npz" and unw_art.kind == "npz"
    stack = load_igram_stack(unw_art.path)
    src = load_igram_stack(igrams_npz)
    assert stack.unw is not None and stack.conncomp is not None
    assert stack.unw.shape == src.wrapped.shape and stack.unw.dtype == np.float32
    assert stack.conncomp.dtype == np.uint8  # save_igram_stack semantics
    assert stack.pairs == src.pairs and stack.dates == src.dates
    expected_masked = src.mask | (src.coherence < 0.5)
    assert np.isnan(stack.unw[expected_masked]).all()
    assert np.isfinite(stack.unw[~expected_masked]).all()
    assert len(fake_snaphu.calls) == src.n_pairs
    assert all(c["cost"] == "smooth" for c in fake_snaphu.calls)
    assert fake_snaphu.calls[0]["scratchdir"] == str(out / "scratch" / src.pairs[0])
    stats = json.loads(arts["unw_stats"].path.read_text())
    assert stats["engine"] == "snaphu" and stats["n_pairs"] == src.n_pairs
    assert stats["wall_time_s"] >= 0 and set(stats["pairs"]) == set(src.pairs)
    assert stats["pairs"][src.pairs[0]]["backend"] == "snaphu-py"
    assert unw_art.meta["n_pairs"] == src.n_pairs
    log = (out / "logs" / "snaphu.log").read_text()
    assert "unwrap start" in log and "unwrap done" in log
    assert log.count(" pair ") == src.n_pairs


def test_run_rejects_other_stage_and_missing_input(fake_snaphu, tmp_path: Path) -> None:
    eng = _engine()
    with pytest.raises(UnwrapError) as ei:
        eng.run("timeseries", Artifacts(), {}, tmp_path)
    assert ei.value.finding.rule_id == "UNW-001"
    with pytest.raises(UnwrapError) as ei2:
        eng.run("unwrap", Artifacts(), {"_out_dir": str(tmp_path)}, tmp_path / "logs")
    assert ei2.value.finding.rule_id == "UNW-002"


def test_estimate_uses_memory_model() -> None:
    from wintersar.io.schemas import Plan, StageRecord

    eng = _engine()
    plan = Plan(
        stages=[
            StageRecord(
                stage="unwrap",
                node_hash="h",
                engine="snaphu",
                params={"unwrap": {"memory_mb_per_mpixel": 200.0}},
                extra={"n_pixels": 5_000_000, "n_pairs": 3},
            ),
            StageRecord(stage="timeseries", node_hash="h2", engine="mintpy"),
        ],
        to_run=["h"],
    )
    r = eng.estimate(plan)
    assert r.peak_rss_gb == pytest.approx(1000.0 / 1024.0)
    assert r.n_jobs == 3
