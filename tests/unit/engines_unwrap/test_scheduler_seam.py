"""Seam between the unwrap scheduler and the engine adapters (plan §5.4, PERF-03, ADR-0047).

``wintersar.unwrap.api._backend_params`` hands the adapters ``_tile_dir``, ``_log_dir`` and
``nproc``; ``UnwrapEngineBase.run`` hands them ``_scratch_dir``, ``_log_path`` and
``nproc_per_igram``. Both spellings must work, otherwise tiles land in a temporary directory
that is deleted (``stats["tile_dirs"]`` stays empty, so PERF-03 assemble-only re-runs can
never be fed), adapter findings are never written to ``log_dir`` and the planned tile
parallelism is silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wintersar.engines import snaphu as sn
from wintersar.engines import tophu as th
from wintersar.engines.base import get_engine
from wintersar.io.schemas import Artifact
from wintersar.unwrap.api import run_unwrap
from wintersar.util.sysinfo import MachineSpec

pytestmark = pytest.mark.engine

MACHINE = MachineSpec(cores=4, memory_gb=16.0, gpu=False, gpu_name=None, python="3.11", os="test")


def _scheduler_params(tile_dir: Path, log_dir: Path, pair: str) -> dict[str, object]:
    """The shape ``_backend_params`` produces (same key names, one pair)."""
    return {
        "method": "snaphu",
        "cost": "smooth",
        "init": "mcf",
        "coherence_threshold": 0.3,
        "ntiles": (2, 2),
        "tile_overlap": 2,
        "nproc": 4,
        "save_cost_file": False,
        "_tile_dir": str(tile_dir),
        "_log_dir": str(log_dir),
        "_pair": pair,
        "_index": 0,
    }


# ------------------------------------------------------------------ adapter side


def test_snaphu_adapter_reads_the_scheduler_key_names(fake_snaphu, pair, tmp_path: Path) -> None:
    eng = get_engine("snaphu")
    assert isinstance(eng, sn.SnaphuEngine)
    tile_dir, log_dir = tmp_path / "tiles", tmp_path / "logs"
    params = _scheduler_params(tile_dir, log_dir, "20240101_20240113")

    res = eng.unwrap(pair.wrapped, pair.coherence, pair.mask, params)

    kw = fake_snaphu.calls[0]
    # _tile_dir + _pair -> a per-pair directory that survives the call (PERF-03)
    expected = tile_dir / "20240101_20240113"
    assert kw["scratchdir"] == str(expected) and kw["delete_scratch"] is False
    assert expected.is_dir()
    assert res.stats["tile_dir"] == str(expected)
    # nproc: the planned value, not UnwrapCfg.nproc_per_igram (= 1)
    assert kw["nproc"] == 4 and res.stats["nproc"] == 4
    # _log_dir -> <log_dir>/snaphu.log, where `wintersar diagnose` looks
    text = (log_dir / "snaphu.log").read_text(encoding="utf-8")
    assert "UNW-007" in text and "snaphu-py unwrap" in text


def test_tophu_adapter_reads_the_scheduler_key_names(fake_tophu, pair, tmp_path: Path) -> None:
    eng = get_engine("tophu")
    assert isinstance(eng, th.TophuEngine)
    tile_dir, log_dir = tmp_path / "tiles", tmp_path / "logs"
    params = _scheduler_params(tile_dir, log_dir, "20240101_20240113")
    params["tophu_unwrap_func"] = "snaphu"

    res = eng.unwrap(pair.wrapped, pair.coherence, pair.mask, params)

    expected = tile_dir / "20240101_20240113"
    assert fake_tophu.calls[0]["scratchdir"] == str(expected)
    assert expected.is_dir()
    assert res.stats["tile_dir"] == str(expected)
    assert "tophu multiscale_unwrap" in (log_dir / "tophu.log").read_text(encoding="utf-8")


def test_run_key_names_still_win_over_the_scheduler_ones(fake_snaphu, pair, tmp_path) -> None:
    """``UnwrapEngineBase.run`` sets the per-pair ``_scratch_dir``/``_log_path`` itself."""
    eng = get_engine("snaphu")
    params = _scheduler_params(tmp_path / "tiles", tmp_path / "logs", "A_B")
    params["_scratch_dir"] = str(tmp_path / "scratch" / "A_B")
    params["_log_path"] = str(tmp_path / "run" / "snaphu.log")
    params["nproc_per_igram"] = 2

    eng.unwrap(pair.wrapped, pair.coherence, pair.mask, params)

    assert fake_snaphu.calls[0]["scratchdir"] == str(tmp_path / "scratch" / "A_B")
    assert (tmp_path / "run" / "snaphu.log").exists()
    assert not (tmp_path / "logs" / "snaphu.log").exists()
    assert fake_snaphu.calls[0]["nproc"] == 4  # planned value still wins over the raw field


def test_snaphu_py_reports_no_tile_dir_when_the_scratch_is_deleted(
    fake_snaphu, pair, tmp_path: Path
) -> None:
    """``keep_tile_dir=false``: snaphu-py deletes the directory, so it must not be reported."""
    eng = get_engine("snaphu")
    params = _scheduler_params(tmp_path / "tiles", tmp_path / "logs", "A_B")
    params["keep_tile_dir"] = False

    res = eng.unwrap(pair.wrapped, pair.coherence, pair.mask, params)

    assert fake_snaphu.calls[0]["delete_scratch"] is True
    assert res.stats["tile_dir"] is None and res.stats["tile_dir_kept"] is False


# ------------------------------------------------------------------ scheduler side


def test_scheduler_run_fills_tile_dirs_and_writes_an_adapter_log(
    fake_snaphu, igrams_npz: Path, tmp_path: Path
) -> None:
    """End-to-end through ``run_unwrap``: PERF-03 tile dirs + the adapter log (UNW-006/007)."""
    out_dir, log_dir = tmp_path / "out", tmp_path / "logs"
    artifacts = run_unwrap(
        Artifact(name="igrams", path=igrams_npz, kind="npz"),
        {"method": "snaphu", "tiles": {"rows": 2, "cols": 2}, "_n_parallel": 1},
        out_dir,
        log_dir,
        MACHINE,
    )

    stats = json.loads((out_dir / "stats.json").read_text(encoding="utf-8"))
    pairs = list(stats["conncomp_counts"])
    assert stats["tile_dirs"] == {p: str(out_dir / "tiles" / p) for p in pairs}
    assert artifacts["unw"].meta["tile_dirs"] == stats["tile_dirs"]
    for p in pairs:
        assert (out_dir / "tiles" / p).is_dir()

    text = (log_dir / "snaphu.log").read_text(encoding="utf-8")
    assert text.count("snaphu-py unwrap") == len(pairs)
    assert "UNW-006" in text  # overlap capped for a 24x32 stack tiled 2x2
    assert sorted(c["scratchdir"] for c in fake_snaphu.calls) == sorted(
        str(out_dir / "tiles" / p) for p in pairs
    )
