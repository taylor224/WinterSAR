"""PERF-10 seam (ADR-0095): ``config.compute.gpu`` must reach the unwrap *workers*, and a GPU
request the machine cannot honour must surface as ``ENV-005`` in the stage report.

``stage_gpu`` is a ``contextvars`` context; Python 3.11 does not copy it into
``ThreadPoolExecutor`` / spawned ``ProcessPoolExecutor`` workers, so before the fix every
``combine_masks`` call inside ``_unwrap_one`` resolved ``auto`` whenever ``n_parallel > 1``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.unwrap.conftest import make_machine, make_synth_stack
from wintersar.compute import xp as xpmod
from wintersar.io.igrams import save_igram_stack
from wintersar.io.schemas import Artifact
from wintersar.unwrap import api, masks, scheduler


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WINTERSAR_GPU", raising=False)
    xpmod.reset_backend_cache()
    yield
    xpmod.reset_backend_cache()


def _no_cupy(monkeypatch: pytest.MonkeyPatch) -> None:
    xpmod.reset_backend_cache()
    monkeypatch.setattr(xpmod, "cupy_available", lambda: False)
    monkeypatch.setattr(xpmod, "cupy_installed", lambda: False)


def _spy_resolutions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, str]]]:
    """Record ``(thread, resolve_request())`` of every kernel-boundary backend decision."""
    seen: list[tuple[str, tuple[Any, str]]] = []
    real = xpmod.resolve_backend

    def spy(gpu: Any = None, *, strict: bool = False) -> Any:
        seen.append((threading.current_thread().name, xpmod.resolve_request(gpu)))
        return real(gpu, strict=strict)

    monkeypatch.setattr(masks, "resolve_backend", spy)
    monkeypatch.setattr(scheduler, "resolve_backend", spy)
    return seen


def _run(tmp_path: Path, params: dict[str, Any]) -> dict[str, Any]:
    stack = make_synth_stack(n_dates=4, shape=(16, 16), seed=1)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    arts = api.run_unwrap(
        Artifact(name="igrams", path=npz, kind="npz"),
        {"method": "truth", "coherence_threshold": 0.3, **params},
        tmp_path / "out",
        tmp_path / "logs",
        make_machine(4, 8.0),
    )
    return json.loads(arts["unwrap_stats"].path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("gpu", [False, True])
def test_thread_pool_workers_inherit_the_stage_gpu_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gpu: bool
) -> None:
    """Every kernel call — main thread and pool workers alike — resolves the executor's
    ``_gpu`` (source ``argument``/``stage``), never ``auto``."""
    _no_cupy(monkeypatch)
    seen = _spy_resolutions(monkeypatch)
    with xpmod.stage_gpu(gpu):  # what Executor._execute_locked wraps _dispatch in
        stats = _run(tmp_path, {"_n_parallel": 2, "_gpu": gpu})
    assert stats["executor"] == "thread" and stats["n_parallel"] == 2
    workers = {thread for thread, _ in seen if thread != "MainThread"}
    assert workers, "no kernel ran on a pool worker"
    assert {req for _, req in seen} <= {(gpu, "argument"), (gpu, "stage")}, seen
    assert not any(src == "auto" for _, (_, src) in seen)
    # the per-pair stats say which backend the worker used and where the request came from
    assert {s["compute"]["gpu_source"] for s in stats["per_pair"]} == {"stage"}
    assert {s["compute"]["backend"] for s in stats["per_pair"]} == {"numpy"}


def test_the_gpu_param_alone_is_enough_without_a_stage_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that passes ``_gpu`` but does not enter ``stage_gpu`` (tests, the standalone
    CLI) gets the same propagation."""
    _no_cupy(monkeypatch)
    seen = _spy_resolutions(monkeypatch)
    stats = _run(tmp_path, {"_n_parallel": 2, "_gpu": False})
    assert {req for _, req in seen} == {(False, "argument"), (False, "stage")}
    assert stats["compute"] == {
        "backend": "numpy",
        "gpu_requested": False,
        "gpu_source": "argument",
        "degraded": False,
    }


def test_spawned_process_workers_re_enter_the_stage_context(tmp_path: Path) -> None:
    """The process pool (snaphu/tophu path) starts every worker with an empty context: the
    job carries the request and ``_unwrap_one`` re-enters ``stage_gpu`` itself."""
    stack = make_synth_stack(n_dates=3, shape=(8, 8), seed=2, with_truth=False)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    parts = tmp_path / "parts"
    jobs = [
        api._Job(
            index=i,
            pair=pair,
            igram_path=str(npz),
            method="identity",
            plan={"rows": 1, "cols": 1, "overlap_px": 0},
            coherence_threshold=0.3,
            use_geo_mask=False,
            use_coherence=True,
            backend_params={},
            parts_dir=str(parts),
            native_tiles=False,
            want_truth=False,
            gpu=False,
        )
        for i, pair in enumerate(stack.pairs)
    ]
    results, failures = api._execute(jobs, n_parallel=2, use_threads=False)
    assert failures == [] and len(results) == len(jobs)
    assert {r.stats["compute"]["gpu_source"] for r in results} == {"stage"}
    assert {r.stats["compute"]["backend"] for r in results} == {"numpy"}


def test_gpu_request_without_cupy_reports_env_005_and_the_backend_that_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``compute.gpu: true`` on a CPU-only box: the stage runs on numpy (plan §6.1 CPU
    fallback) and says so — one WARN ``ENV-005`` in the findings, ``machine.gpu`` reports
    what the kernels got, ``compute`` the request and the degrade."""
    _no_cupy(monkeypatch)
    stats = _run(tmp_path, {"_n_parallel": 1, "_gpu": True})
    rules = [(f["rule_id"], f["severity"]) for f in stats["findings"]]
    assert rules.count(("ENV-005", "WARN")) == 1, rules
    env = next(f for f in stats["findings"] if f["rule_id"] == "ENV-005")
    assert env["evidence"]["requested_by"] == "argument" and env["evidence"]["backend"] == "numpy"
    assert stats["machine"]["gpu"] is False
    assert stats["compute"] == {
        "backend": "numpy",
        "gpu_requested": True,
        "gpu_source": "argument",
        "degraded": True,
    }
    assert stats["status"] == "ok"
    # no request, no finding
    stats2 = _run(tmp_path / "b", {"_n_parallel": 1})
    assert not any(f["rule_id"] == "ENV-005" for f in stats2["findings"])
    assert stats2["compute"]["degraded"] is False


def test_stack_fringe_density_forwards_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_cupy(monkeypatch)
    seen = _spy_resolutions(monkeypatch)
    stack = make_synth_stack(n_dates=3, shape=(8, 8), seed=3)
    cfg = api.cfg_from_params({"coherence_threshold": 0.3})
    f = api.stack_fringe_density(stack, cfg, gpu=False)
    assert f is None or np.isfinite(f)
    assert seen and all(req == (False, "argument") for _, req in seen)
    assert api.stage_gpu_request({"_gpu": True}) is True
    assert api.stage_gpu_request({}) is None
    with xpmod.stage_gpu(False):
        assert api.stage_gpu_request({}) is False
        assert api.stage_gpu_request({"_gpu": True}) is True  # the param wins inside
