"""Phase 2 DoD (R-06, PERF-03/04): the unwrap scheduler end-to-end on synthetic stacks.

No external engine: the ``truth`` and ``identity`` test backends stand in for SNAPHU.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tests.unit.unwrap.conftest import make_machine, make_synth_stack
from wintersar.engines.base import EngineNotAvailableError
from wintersar.io.igrams import load_igram_stack, save_igram_stack
from wintersar.io.schemas import Artifact
from wintersar.research.synth import unwrap_error_fraction
from wintersar.unwrap import api
from wintersar.unwrap.api import UnwrapFailedError, run_unwrap


def _artifact(path: Path) -> Artifact:
    return Artifact(name="igrams", path=path, kind="npz")


def test_truth_backend_gives_zero_unwrap_error_and_stats(tmp_path: Path):
    stack = make_synth_stack(n_dates=6, shape=(48, 48), seed=1, water_fraction=0.1)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    out, logs = tmp_path / "unwrap", tmp_path / "logs"
    arts = run_unwrap(
        _artifact(npz),
        {"method": "truth", "coherence_threshold": 0.3},
        out,
        logs,
        make_machine(4, 8.0),
    )
    assert set(arts.items) == {"unw", "unwrap_stats"}
    res = np.load(arts["unw"].path)
    assert set(res.files) >= {"unw", "conncomp", "pairs", "dates"}
    unw, cc = res["unw"], res["conncomp"]
    assert unw.dtype == np.float32 and cc.dtype.kind == "u"
    assert unw.shape == stack.wrapped.shape
    assert [str(p) for p in res["pairs"]] == stack.pairs
    truth = stack.truth["unw_true"]
    for i in range(stack.n_pairs):
        expected_mask = (stack.coherence[i] < 0.3) | stack.mask_for(i)
        assert np.array_equal(np.isnan(unw[i]), expected_mask)
        assert (cc[i][expected_mask] == 0).all() and (cc[i][~expected_mask] > 0).all()
        assert unwrap_error_fraction(unw[i], truth[i], expected_mask) == 0.0
    # water columns (left edge) are NaN
    assert np.isnan(unw[:, :, 0]).all()
    stats = json.loads(arts["unwrap_stats"].path.read_text(encoding="utf-8"))
    assert stats["status"] == "ok"
    assert stats["method"] == "truth" and stats["plan"]["method"] == "truth"
    assert stats["n_pairs"] == stack.n_pairs
    assert stats["wall_time_s"] > 0 and stats["peak_rss_mb"] > 0
    assert stats["executor"] == "thread"
    assert set(stats["conncomp_counts"]) == set(stack.pairs)
    assert all(v == 1 for v in stats["conncomp_counts"].values())
    assert stats["boundary_jumps"] == {
        "n_boundaries": 0,
        "n_boundaries_with_jump": 0,
        "n_jump_pixels": 0,
    }
    assert stats["tile_dirs"] == {}
    assert stats["findings"] == []
    assert 0.0 < stats["masked_fraction"] < 1.0
    assert stats["fringe_density"] is not None
    assert len(stats["per_pair"]) == stack.n_pairs and stats["per_pair"][0]["mask"]["n_nodes"] > 0
    assert stats["reasons"]
    assert (logs / "unwrap.log").exists()
    assert not (out / "parts").exists()  # temporary parts cleaned up
    assert arts["unw"].meta["n_pairs"] == stack.n_pairs


def test_identity_backend_runs_in_parallel(tmp_path: Path):
    stack = make_synth_stack(n_dates=6, shape=(32, 32), seed=2, with_truth=False)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    arts = run_unwrap(
        _artifact(npz),
        {"method": "identity"},
        tmp_path / "out",
        tmp_path / "logs",
        make_machine(4, 16.0),
    )
    stats = json.loads(arts["unwrap_stats"].path.read_text(encoding="utf-8"))
    assert stats["n_parallel"] > 1
    assert stats["plan"]["n_parallel"] == stats["n_parallel"]
    unw = np.load(arts["unw"].path)["unw"]
    valid = ~np.isnan(unw)
    np.testing.assert_allclose(unw[valid], stack.wrapped[valid], atol=1e-6)
    assert len(stats["failed"]) == 0


def test_process_pool_executor_path_with_identity(tmp_path: Path):
    """ProcessPool (spawn) branch of the executor, as used for real engine backends."""
    stack = make_synth_stack(n_dates=4, shape=(16, 16), seed=3, with_truth=False)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    machine = make_machine(2, 4.0)
    cfg = api.cfg_from_params({"method": "identity"})
    plan = api.resolve_plan(stack.shape, stack.n_pairs, machine, cfg, requested_method="identity")
    jobs = [
        api._Job(
            index=i,
            pair=p,
            igram_path=str(npz),
            method="identity",
            plan=plan.to_dict(),
            coherence_threshold=0.3,
            use_geo_mask=True,
            use_coherence=True,
            backend_params={},
            parts_dir=str(tmp_path / "parts"),
            native_tiles=False,
            want_truth=False,
        )
        for i, p in enumerate(stack.pairs)
    ]
    results, failures = api._execute(jobs, 2, use_threads=False)
    assert failures == [] and [r.index for r in results] == list(range(stack.n_pairs))
    assert Path(results[0].unw_path).exists()


def test_executor_tiling_detects_injected_seam_offsets(tmp_path: Path):
    stack = make_synth_stack(n_dates=4, shape=(40, 40), seed=4)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    params = {
        "method": "truth",
        "coherence_threshold": 0.0,
        "tiles": {"rows": 2, "cols": 2, "overlap": 0.1, "min_overlap_px": 4},
        "_tile_offset_cycles": {"1,1": 1},
    }
    arts = run_unwrap(
        _artifact(npz), params, tmp_path / "out", tmp_path / "logs", make_machine(2, 8.0)
    )
    stats = json.loads(arts["unwrap_stats"].path.read_text(encoding="utf-8"))
    assert stats["plan"]["rows"] == 2 and stats["plan"]["cols"] == 2
    assert stats["plan"]["overlap_px"] == 4
    assert stats["native_tiles"] is False
    bj = stats["boundary_jumps"]
    assert bj["n_boundaries"] == 4 * stack.n_pairs
    assert bj["n_boundaries_with_jump"] == 2 * stack.n_pairs  # both boundaries into tile (1,1)
    assert bj["n_jump_pixels"] > 0
    per = stats["per_pair"][0]["boundary_jumps"]
    offsets = {
        (tuple(b["tile_a"]), tuple(b["tile_b"])): b["mode_offset_cycles"] for b in per["boundaries"]
    }
    assert offsets[((0, 1), (1, 1))] == -1 and offsets[((1, 0), (1, 1))] == -1
    assert offsets[((0, 0), (0, 1))] == 0 and offsets[((0, 0), (1, 0))] == 0
    assert [f["rule_id"] for f in stats["findings"]] == ["UNW-003"]
    assert arts["unw"].meta["findings"] == ["UNW-003"]


def test_executor_tiling_without_offsets_is_exact(tmp_path: Path):
    stack = make_synth_stack(n_dates=4, shape=(40, 40), seed=5)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    params = {"method": "truth", "coherence_threshold": 0.3, "tiles": [2, 2]}
    arts = run_unwrap(
        _artifact(npz), params, tmp_path / "out", tmp_path / "logs", make_machine(2, 8.0)
    )
    stats = json.loads(arts["unwrap_stats"].path.read_text(encoding="utf-8"))
    assert stats["boundary_jumps"]["n_boundaries_with_jump"] == 0
    assert stats["findings"] == []
    unw = np.load(arts["unw"].path)["unw"]
    truth = stack.truth["unw_true"]
    for i in range(stack.n_pairs):
        m = np.isnan(unw[i])
        assert unwrap_error_fraction(unw[i], truth[i], m) == 0.0
        np.testing.assert_allclose(unw[i][~m], truth[i][~m], atol=1e-4)


def test_missing_truth_array_reports_unw004_and_raises(tmp_path: Path):
    stack = make_synth_stack(n_dates=4, shape=(16, 16), seed=6, with_truth=False)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    with pytest.raises(UnwrapFailedError) as exc:
        run_unwrap(
            _artifact(npz),
            {"method": "truth"},
            tmp_path / "out",
            tmp_path / "logs",
            make_machine(2, 8.0),
        )
    assert len(exc.value.failed) == stack.n_pairs
    assert all(f.rule_id == "UNW-004" for f in exc.value.findings)
    stats = json.loads((tmp_path / "out" / "stats.json").read_text(encoding="utf-8"))
    assert stats["status"] == "failed" and len(stats["failed"]) == stack.n_pairs
    assert (tmp_path / "out" / "unw.npz").exists()  # partial output is still written


def test_engine_backend_unavailable_or_unknown(tmp_path: Path):
    stack = make_synth_stack(n_dates=3, shape=(16, 16), seed=7, with_truth=False)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    with pytest.raises((KeyError, EngineNotAvailableError, TypeError)):
        run_unwrap(
            _artifact(npz),
            {"method": "snaphu"},
            tmp_path / "out",
            tmp_path / "logs",
            make_machine(2, 8.0),
        )


def test_private_machine_keys_and_n_parallel_override(tmp_path: Path):
    stack = make_synth_stack(n_dates=5, shape=(16, 16), seed=8, with_truth=False)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    params = {"method": "identity", "_cores": 1, "_memory_gb": 2.0}
    arts = run_unwrap(
        _artifact(npz), params, tmp_path / "o1", tmp_path / "l1", make_machine(8, 64.0)
    )
    stats = json.loads(arts["unwrap_stats"].path.read_text(encoding="utf-8"))
    assert stats["n_parallel"] == 1 and stats["executor"] == "inline"
    assert stats["machine"] == {"cores": 1, "memory_gb": 2.0, "gpu": False}
    arts2 = run_unwrap(
        _artifact(npz),
        {"method": "identity", "_n_parallel": 3},
        tmp_path / "o2",
        tmp_path / "l2",
        make_machine(1, 64.0),
    )
    stats2 = json.loads(arts2["unwrap_stats"].path.read_text(encoding="utf-8"))
    assert stats2["n_parallel"] == 3


def test_output_is_loadable_as_igram_stack(tmp_path: Path):
    stack = make_synth_stack(n_dates=4, shape=(16, 16), seed=9)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    arts = run_unwrap(
        _artifact(npz),
        {"method": "truth"},
        tmp_path / "out",
        tmp_path / "logs",
        make_machine(2, 8.0),
    )
    with np.load(arts["unw"].path) as z:
        assert [str(d) for d in z["dates"]] == [d.isoformat() for d in stack.dates]
    reloaded = load_igram_stack(npz)
    assert reloaded.n_pairs == stack.n_pairs
