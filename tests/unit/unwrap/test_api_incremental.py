"""PERF-06 (ADR-0080): ``run_unwrap`` reuses per-pair results by identity when the pipeline
passes ``_pairs_dir`` / ``_pairs_done``; without them nothing changes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.unwrap.conftest import make_machine, make_synth_stack
from wintersar.io.igrams import IgramStack, save_igram_stack
from wintersar.io.schemas import Artifact
from wintersar.pipeline import incremental
from wintersar.unwrap import api

_REAL_UNWRAP_ONE = api._unwrap_one


def _extend(stack: IgramStack, extra: IgramStack) -> tuple[IgramStack, list[str]]:
    """``stack`` plus the pairs of ``extra`` (one more date) it does not have yet."""
    idx = [i for i, k in enumerate(extra.pairs) if k not in set(stack.pairs)]
    new = [extra.pairs[i] for i in idx]
    truth = {}
    if "unw_true" in stack.truth and "unw_true" in extra.truth:
        truth["unw_true"] = np.concatenate([stack.truth["unw_true"], extra.truth["unw_true"][idx]])
    return IgramStack(
        wrapped=np.concatenate([stack.wrapped, extra.wrapped[idx]]),
        coherence=np.concatenate([stack.coherence, extra.coherence[idx]]),
        pairs=[*stack.pairs, *new],
        dates=extra.dates,
        mask=np.concatenate([stack.mask, extra.mask[idx]]) if stack.mask is not None else None,
        truth=truth,
    ), new


def _run(
    npz: Path, node_dir: Path, params: dict[str, Any], spy: list[str], monkeypatch
) -> tuple[Any, dict[str, Any]]:
    def counting(job: api._Job) -> api._JobResult:
        spy.append(job.pair)
        return _REAL_UNWRAP_ONE(job)

    monkeypatch.setattr(api, "_unwrap_one", counting)
    done = incremental.PairCache.load(node_dir, "unwrap")
    p = {
        **params,
        "method": "truth",
        "_n_parallel": 1,
        incremental.PAIRS_DIR_KEY: str(incremental.pairs_dir(node_dir)),
        incremental.PAIRS_DONE_KEY: done.done_entries(),
    }
    out = node_dir / "out"
    arts = api.run_unwrap(
        Artifact(name="igrams", path=npz, kind="npz"),
        p,
        out,
        node_dir / "logs",
        make_machine(2, 8.0),
    )
    stats = json.loads((out / api.STATS_FILE).read_text(encoding="utf-8"))
    return arts, stats


def test_second_run_unwraps_only_the_new_pairs(tmp_path: Path, monkeypatch) -> None:
    six = make_synth_stack(n_dates=6, shape=(16, 16), seed=3)
    seven = make_synth_stack(n_dates=7, shape=(16, 16), seed=3)
    extended, new_pairs = _extend(six, seven)
    n_new = len(new_pairs)
    assert n_new == 4 and six.n_pairs == 14
    node_dir = tmp_path / "unwrap" / "node"
    spy: list[str] = []
    npz6 = save_igram_stack(six, tmp_path / "six.npz")
    arts, stats = _run(npz6, node_dir, {"coherence_threshold": 0.3}, spy, monkeypatch)
    assert spy == six.pairs and stats["n_reused"] == 0
    assert arts["unw"].meta["pairs_computed"] == sorted(six.pairs)
    assert arts["unw"].meta["pairs_reused"] == []
    manifest = incremental.pairs_dir(node_dir) / incremental.PAIRS_MANIFEST
    assert manifest.exists()
    first_unw = np.load(arts["unw"].path)["unw"]

    spy.clear()
    npz7 = save_igram_stack(extended, tmp_path / "seven.npz")
    arts, stats = _run(npz7, node_dir, {"coherence_threshold": 0.3}, spy, monkeypatch)
    assert spy == new_pairs  # only the pairs touching the new date
    assert stats["n_reused"] == six.n_pairs and stats["reused"] == six.pairs
    assert sorted(arts["unw"].meta["pairs_reused"]) == sorted(six.pairs)
    assert sorted(arts["unw"].meta["pairs_computed"]) == sorted(new_pairs)
    assert arts["unw"].meta["pairs"] == extended.pairs
    with np.load(arts["unw"].path) as z:
        unw = z["unw"]
        assert list(z["pairs"]) == extended.pairs and unw.shape[0] == extended.n_pairs
        assert z["conncomp"].shape == unw.shape
    np.testing.assert_array_equal(unw[: six.n_pairs], first_unw)  # reused bitwise
    per_pair = stats["per_pair"]
    assert [s["index"] for s in per_pair] == list(range(extended.n_pairs))
    assert all(s.get("reused") for s in per_pair[: six.n_pairs])
    assert not any(s.get("reused") for s in per_pair[six.n_pairs :])
    assert all("n_conncomp" in s for s in per_pair)  # stats of reused pairs come back too

    # a third identical run reuses everything and unwraps nothing
    spy.clear()
    arts, stats = _run(npz7, node_dir, {"coherence_threshold": 0.3}, spy, monkeypatch)
    assert spy == [] and stats["n_reused"] == extended.n_pairs
    assert stats["executor"] == "inline"


def test_parameter_change_recomputes_every_pair(tmp_path: Path, monkeypatch) -> None:
    stack = make_synth_stack(n_dates=4, shape=(16, 16), seed=5)
    npz = save_igram_stack(stack, tmp_path / "s.npz")
    node_dir = tmp_path / "node"
    spy: list[str] = []
    _run(npz, node_dir, {"coherence_threshold": 0.3}, spy, monkeypatch)
    spy.clear()
    _, stats = _run(npz, node_dir, {"coherence_threshold": 0.5}, spy, monkeypatch)
    assert spy == stack.pairs and stats["n_reused"] == 0
    # ... and the modified input of one pair invalidates only that pair
    spy.clear()
    changed = IgramStack(
        wrapped=stack.wrapped.copy(),
        coherence=stack.coherence.copy(),
        pairs=stack.pairs,
        dates=stack.dates,
        mask=stack.mask,
        truth=dict(stack.truth),
    )
    changed.coherence[1] = np.clip(changed.coherence[1] * 0.5, 0.01, 1.0)
    npz2 = save_igram_stack(changed, tmp_path / "s2.npz")
    _, stats = _run(npz2, node_dir, {"coherence_threshold": 0.5}, spy, monkeypatch)
    assert spy == [stack.pairs[1]] and stats["n_reused"] == stack.n_pairs - 1


def test_without_pairs_dir_nothing_is_kept(tmp_path: Path) -> None:
    stack = make_synth_stack(n_dates=3, shape=(8, 8), seed=1)
    npz = save_igram_stack(stack, tmp_path / "s.npz")
    out = tmp_path / "out"
    arts = api.run_unwrap(
        Artifact(name="igrams", path=npz, kind="npz"),
        {"method": "truth", "coherence_threshold": 0.3},
        out,
        tmp_path / "logs",
        make_machine(2, 8.0),
    )
    assert "pairs_reused" not in arts["unw"].meta and arts["unw"].meta["pairs"] == stack.pairs
    assert not (out / "pairs").exists() and not (out / api.PARTS_DIR).exists()
    stats = json.loads((out / api.STATS_FILE).read_text(encoding="utf-8"))
    assert stats["n_reused"] == 0 and stats["reused"] == []


def test_pair_param_hash_tracks_the_resolved_plan_not_parallelism() -> None:
    cfg = api.cfg_from_params({"coherence_threshold": 0.3})
    machine = make_machine(4, 8.0)
    plan = api.resolve_plan((32, 32), 3, machine, cfg, requested_method="truth")
    base = api.pair_param_hash({"coherence_threshold": 0.3}, cfg, plan)
    assert (
        api.pair_param_hash({"coherence_threshold": 0.3, "_n_parallel": 4, "_cores": 8}, cfg, plan)
        == base
    )
    assert api.pair_param_hash({"coherence_threshold": 0.3, "nlooks": 4}, cfg, plan) != base
    assert (
        api.pair_param_hash(
            {"coherence_threshold": 0.3, "_tile_offset_cycles": {"0,0": 1}}, cfg, plan
        )
        != base
    )
    other_cfg = api.cfg_from_params({"coherence_threshold": 0.5})
    assert api.pair_param_hash({"coherence_threshold": 0.5}, other_cfg, plan) != base
    retiled = api.resolve_plan(
        (32, 32),
        3,
        make_machine(4, 8.0),
        api.cfg_from_params({"tiles": "2x2"}),
        requested_method="truth",
    )
    assert api.pair_param_hash({}, cfg, retiled) != base


def test_pair_input_hash_covers_mask_and_truth() -> None:
    stack = make_synth_stack(n_dates=3, shape=(8, 8), seed=2)
    a = api.pair_input_hash(stack, 0)
    assert a == api.pair_input_hash(stack, 0) and a != api.pair_input_hash(stack, 1)
    assert api.pair_input_hash(stack, 0, want_truth=True) != a
    no_mask = IgramStack(
        wrapped=stack.wrapped, coherence=stack.coherence, pairs=stack.pairs, dates=stack.dates
    )
    assert api.pair_input_hash(no_mask, 0) != a


@pytest.mark.parametrize("key", [incremental.PAIRS_DIR_KEY, incremental.PAIRS_DONE_KEY])
def test_backend_params_never_carry_the_pair_cache_keys(key: str) -> None:
    cfg = api.cfg_from_params({})
    plan = api.resolve_plan((8, 8), 1, make_machine(2, 8.0), cfg, requested_method="identity")
    out = api._backend_params(
        {key: "x", "coherence_threshold": 0.3}, cfg, plan, Path("t"), Path("l")
    )
    assert key not in out
