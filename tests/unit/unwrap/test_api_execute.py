"""Executor robustness (PERF-04) and backend eligibility of the ``auto`` plan (R-06)."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, ClassVar

import numpy as np
import pytest

from tests.unit.unwrap.conftest import make_machine, make_synth_stack
from wintersar.engines._unwrap_common import StackOnlyEngineError, make_finding
from wintersar.engines.base import Engine, EngineNotAvailableError, register_engine
from wintersar.i18n import t
from wintersar.io.igrams import save_igram_stack
from wintersar.io.schemas import Artifact
from wintersar.unwrap import api, backends
from wintersar.unwrap.scheduler import REASON_PREFIX

STACK_ONLY_NAME = "_test_stack_only_unwrapper"


def _job(index: int) -> api._Job:
    return api._Job(
        index=index,
        pair=f"2024010{index % 9}_2024020{index % 9}",
        igram_path="unused-by-the-fake-worker",
        method="identity",
        plan={"rows": 1, "cols": 1, "overlap_px": 0},
        coherence_threshold=0.3,
        use_geo_mask=False,
        use_coherence=True,
        backend_params={},
        parts_dir="unused",
        native_tiles=False,
        want_truth=False,
    )


def test_keyboard_interrupt_cancels_the_queued_interferograms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C must not be queued behind the rest of the stack.

    ``Executor.__exit__`` alone (``shutdown(wait=True)``) runs every already submitted
    interferogram to completion, so the interrupt surfaces only after the whole stack.
    """
    jobs = [_job(i) for i in range(16)]
    started: list[int] = []
    lock = threading.Lock()

    def fake_unwrap_one(job: api._Job) -> api._JobResult:
        with lock:
            started.append(job.index)
        if job.index == 0:
            time.sleep(0.05)
            raise KeyboardInterrupt
        time.sleep(0.2)
        return api._JobResult(job.index, job.pair, "unw", "cc", {})

    monkeypatch.setattr(api, "_unwrap_one", fake_unwrap_one)
    with pytest.raises(KeyboardInterrupt):
        api._execute(jobs, n_parallel=2, use_threads=True)
    assert len(started) <= len(jobs) // 2, f"queued jobs kept running: {sorted(started)}"


def test_worker_exceptions_are_still_collected_as_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The abort path must not swallow ordinary per-interferogram failures."""
    jobs = [_job(i) for i in range(4)]

    def fake_unwrap_one(job: api._Job) -> api._JobResult:
        if job.index % 2:
            msg = "boom"
            raise RuntimeError(msg)
        return api._JobResult(job.index, job.pair, "unw", "cc", {})

    monkeypatch.setattr(api, "_unwrap_one", fake_unwrap_one)
    results, failures = api._execute(jobs, n_parallel=2, use_threads=True)
    assert [r.index for r in results] == [0, 2]
    assert [f"{p}" for p, _ in failures] == [jobs[1].pair, jobs[3].pair]
    assert all("RuntimeError: boom" in err for _, err in failures)


@pytest.fixture(scope="module")
def stack_only_engine() -> str:
    @register_engine
    class _StackOnlyEngine(Engine):
        name: ClassVar[str] = STACK_ONLY_NAME
        stages: ClassVar[tuple[str, ...]] = ("unwrap",)
        stack_only: ClassVar[bool] = True

        def detect_version(self) -> str | None:
            return "1.0"

        def run(self, stage: str, inputs: Any, params: dict[str, Any], log_dir: Any) -> Any:
            raise NotImplementedError

        def unwrap(self, igram: Any, coh: Any, mask: Any, params: dict[str, Any]) -> Any:
            raise StackOnlyEngineError(make_finding("UNW-009", engine=self.name))

    return STACK_ONLY_NAME


def test_stack_only_engines_are_not_offered_to_the_two_d_plan(stack_only_engine: str) -> None:
    assert backends.is_stack_only(stack_only_engine) is True
    assert backends.is_stack_only("spurt") is True  # 3-D space-time unwrapper
    assert backends.is_stack_only("truth") is False
    assert backends.is_stack_only("no-such-backend") is False
    assert backends.two_d_backends(["tophu", stack_only_engine]) == ["tophu"]
    assert backends.available_backends((stack_only_engine,)) == []
    assert backends.available_backends((stack_only_engine,), include_stack_only=True) == [
        stack_only_engine
    ]


def test_auto_plan_falls_back_to_none_available_instead_of_a_stack_only_engine(
    stack_only_engine: str,
) -> None:
    cfg = api.cfg_from_params({"method": "auto"})
    plan = api.resolve_plan((64, 64), 3, make_machine(4, 8.0), cfg, available=[stack_only_engine])
    assert plan.method != stack_only_engine
    assert REASON_PREFIX + "method_none_available" in plan.reason_keys
    # an explicitly requested 2-D engine is untouched
    plan_tophu = api.resolve_plan(
        (64, 64), 3, make_machine(4, 8.0), cfg, available=["tophu", stack_only_engine]
    )
    assert plan_tophu.method == "tophu"


def test_stack_only_backend_would_fail_every_interferogram(stack_only_engine: str) -> None:
    """Why the plan must not pick it: the 2-D call raises for every pair."""
    unwrapper = backends.get_unwrapper(stack_only_engine)
    with pytest.raises(StackOnlyEngineError):
        unwrapper.unwrap(np.zeros((4, 4), np.float32), np.ones((4, 4), np.float32), None, {})


def test_unw001_detail_is_rule_ids_not_a_python_list_repr(tmp_path) -> None:
    """UNW-001 text must not leak ``str(EngineNotAvailableError)`` = ``[...]`` (rule 11.6)."""
    stack = make_synth_stack(n_dates=2, shape=(8, 8), seed=21, with_truth=False)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    with pytest.raises(EngineNotAvailableError):
        api.run_unwrap(
            Artifact(name="igrams", path=npz, kind="npz"),
            {"method": "snaphu"},
            tmp_path / "out",
            tmp_path / "logs",
            make_machine(2, 8.0),
        )
    stats = json.loads((tmp_path / "out" / "stats.json").read_text(encoding="utf-8"))
    findings = stats["findings"]
    assert [f["rule_id"] for f in findings] == ["UNW-001", "ENV-001"]
    unw001 = findings[0]["params"]
    assert unw001["detail"] == "ENV-001"
    assert unw001["install_hint"]
    text = t("unwrap.UNW-001.cause", "en", **unw001)
    assert "[" not in text and "ENV-001" in text
    assert t("unwrap.UNW-001.fix", "en", **unw001).count("{") == 0


def test_unavailable_detail_falls_back_to_the_error_text() -> None:
    class _NoCheck:
        name = "_test_no_check"

        def unwrap(self, igram: Any, coh: Any, mask: Any, params: dict[str, Any]) -> Any:
            raise NotImplementedError

    detail, extra = api._unavailable_detail(
        _NoCheck(),  # type: ignore[arg-type]
        EngineNotAvailableError("engine '_test_no_check' is not available"),
    )
    assert extra == [] and detail == "engine '_test_no_check' is not available"
