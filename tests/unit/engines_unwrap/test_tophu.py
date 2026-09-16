"""tophu adapter: absence (ENV-001), multiscale_unwrap kwargs mapping (ADR-0024),
unwrap_func selection, NaN masking, conncomp dtype, run() artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from wintersar.engines import tophu as th
from wintersar.engines._unwrap_common import UnwrapError, UnwrapResult
from wintersar.engines.base import EngineNotAvailableError, get_engine
from wintersar.io.igrams import load_igram_stack
from wintersar.io.schemas import Artifact, Artifacts

pytestmark = pytest.mark.engine


def _engine() -> th.TophuEngine:
    eng = get_engine("tophu")
    assert isinstance(eng, th.TophuEngine)
    return eng


def test_absent_engine_reports_env_001(engines_absent: None, pair) -> None:
    eng = _engine()
    assert eng.detect_version() is None
    assert [f.rule_id for f in eng.check_install()] == ["ENV-001"]
    with pytest.raises(EngineNotAvailableError):
        eng.unwrap(pair.wrapped, pair.coherence, pair.mask, {})
    with pytest.raises(EngineNotAvailableError):
        eng.run("unwrap", Artifacts(), {}, Path("/nonexistent"))


def test_class_metadata() -> None:
    eng = _engine()
    assert eng.name == "tophu" and eng.stages == ("unwrap",)
    assert eng.version_constraint == ">=0.2,<1"
    assert "conda-forge tophu" in eng.install_hint
    assert eng.license_note.startswith("BSD-3-Clause OR Apache-2.0")


def test_multiscale_unwrap_kwargs_mapping_and_masking(fake_tophu, pair, tmp_path: Path) -> None:
    eng = _engine()
    assert eng.detect_version() == "0.2.1" and eng.check_install() == []
    params = {
        "unwrap": {
            "cost": "smooth",
            "init": "mst",
            "coherence_threshold": 0.35,
            "tiles": {"rows": 2, "cols": 3, "overlap": 0.25, "min_overlap_px": 2},
        },
        "nlooks": 6,
        "downsample_factor": [4, 2],
        "_scratch_dir": str(tmp_path / "scratch"),
        "_log_path": str(tmp_path / "tophu.log"),
    }
    res = eng.unwrap(pair.wrapped, pair.coherence, pair.mask, params)
    assert isinstance(res, UnwrapResult)
    assert len(fake_tophu.calls) == 1
    kw = fake_tophu.calls[0]
    # exact upstream keyword names (tophu v0.2.1 multiscale_unwrap signature)
    assert set(kw) == {
        "unwrapped",
        "conncomp",
        "igram",
        "coherence",
        "nlooks",
        "unwrap_func",
        "downsample_factor",
        "ntiles",
        "min_conncomp_overlap",
        "scratchdir",
        "do_lowpass_filter",
    }
    assert kw["unwrapped"].dtype == np.float32 and kw["conncomp"].dtype == np.uint32
    assert kw["igram"].dtype == np.complex64 and kw["coherence"].dtype == np.float32
    assert kw["nlooks"] == 6.0
    assert kw["downsample_factor"] == (4, 2) and kw["ntiles"] == (2, 3)
    assert kw["min_conncomp_overlap"] == 0.5
    assert kw["scratchdir"] == str(tmp_path / "scratch")
    assert kw["do_lowpass_filter"] is True  # upstream default, not overridden
    func = kw["unwrap_func"]
    assert isinstance(func, fake_tophu.module.SnaphuUnwrap)
    assert func.cost == "smooth" and func.init_method == "mst"
    expected_masked = pair.mask | (pair.coherence < 0.35) | ~np.isfinite(pair.wrapped)
    assert (kw["igram"][expected_masked] == 0).all() and (
        kw["coherence"][expected_masked] == 0
    ).all()
    assert res.unw.dtype == np.float32 and np.isnan(res.unw[expected_masked]).all()
    assert np.isfinite(res.unw[~expected_masked]).all()
    assert res.conncomp.dtype == np.uint32 and (res.conncomp[expected_masked] == 0).all()
    st = res.stats
    assert st["backend"] == "tophu/snaphu" and st["wall_time_s"] >= 0
    assert st["ntiles"] == [2, 3] and st["downsample_factor"] == [4, 2]
    assert st["tile_dir"] == str(tmp_path / "scratch") and st["assemble_only_capable"] is False
    assert "multiscale_unwrap" in (tmp_path / "tophu.log").read_text()


def test_defaults_single_tile_and_downsample(fake_tophu, pair) -> None:
    eng = _engine()
    res = eng.unwrap(pair.wrapped, pair.coherence, None, {})
    kw = fake_tophu.calls[0]
    assert kw["ntiles"] == (1, 1)
    assert kw["downsample_factor"] == th.DEFAULT_DOWNSAMPLE_FACTOR == (3, 3)
    assert kw["nlooks"] == 1.0 and "UNW-007" in res.stats["findings"]
    assert kw["unwrap_func"].cost == "defo" and kw["unwrap_func"].init_method == "mcf"
    assert "tile_dir" not in res.stats
    assert not Path(kw["scratchdir"]).exists()  # temp scratch removed


@pytest.mark.parametrize(
    ("name", "cls_attr"),
    [("icu", "ICUUnwrap"), ("phass", "PhassUnwrap"), ("snaphu", "SnaphuUnwrap")],
)
def test_unwrap_func_selection(fake_tophu, pair, name: str, cls_attr: str) -> None:
    eng = _engine()
    res = eng.unwrap(pair.wrapped, pair.coherence, None, {"tophu_unwrap_func": name})
    func = fake_tophu.calls[-1]["unwrap_func"]
    assert isinstance(func, getattr(fake_tophu.module, cls_attr))
    assert res.stats["backend"] == f"tophu/{name}"


def test_unwrap_func_snaphu_py_callback(fake_tophu, fake_snaphu, pair, tmp_path: Path) -> None:
    eng = _engine()
    res = eng.unwrap(
        pair.wrapped,
        pair.coherence,
        None,
        {"tophu_unwrap_func": "snaphu-py", "cost": "smooth", "nlooks": 2},
    )
    func = fake_tophu.calls[-1]["unwrap_func"]
    assert isinstance(func, th.SnaphuPyCallback)
    assert res.stats["backend"] == "tophu/snaphu-py"
    # the callback forwarded the tophu protocol arguments to snaphu-py with the verified names
    call = fake_snaphu.calls[-1]
    assert call["igram"].dtype == np.complex64 and call["corr"].dtype == np.float32
    assert call["nlooks"] == 2.0 and call["cost"] == "smooth" and call["init"] == "mcf"
    assert call["delete_scratch"] is False and isinstance(call["scratchdir"], str)
    # direct protocol call
    out_unw, out_cc = func(
        np.ones((4, 4), np.complex64), np.ones((4, 4), np.float32), 1.0, tmp_path / "s"
    )
    assert out_unw.dtype == np.float32 and out_cc.dtype == np.uint32


def test_unwrap_func_errors(fake_tophu, pair) -> None:
    eng = _engine()
    with pytest.raises(UnwrapError) as ei:
        eng.unwrap(pair.wrapped, pair.coherence, None, {"tophu_unwrap_func": "magic"})
    assert ei.value.finding.rule_id == "UNW-011"
    # snaphu-py requested but not importable
    with pytest.raises(UnwrapError) as ei2:
        eng.unwrap(pair.wrapped, pair.coherence, None, {"tophu_unwrap_func": "snaphu-py"})
    assert ei2.value.finding.rule_id == "UNW-011"
    with pytest.raises(UnwrapError) as ei3:
        th.SnaphuPyCallback(cost="topo")
    assert ei3.value.finding.rule_id == "UNW-004"


def test_run_writes_unw_npz_and_stats(fake_tophu, igrams_npz: Path, tmp_path: Path) -> None:
    eng = _engine()
    out = tmp_path / "unwrap"
    inputs = Artifacts().add(Artifact(name="igrams", path=igrams_npz, kind="npz"))
    arts = eng.run("unwrap", inputs, {"_out_dir": str(out), "nlooks": 4}, out / "logs")
    assert set(arts.items) == {"unw", "unw_stats"}
    stack = load_igram_stack(arts["unw"].path)
    src = load_igram_stack(igrams_npz)
    assert stack.unw is not None and stack.unw.shape == src.wrapped.shape
    assert stack.conncomp is not None and stack.conncomp.dtype.kind == "u"
    assert np.isnan(stack.unw[src.mask]).all()
    assert len(fake_tophu.calls) == src.n_pairs
    stats = json.loads(arts["unw_stats"].path.read_text())
    assert stats["engine"] == "tophu" and set(stats["pairs"]) == set(src.pairs)
    assert (out / "logs" / "tophu.log").exists()
