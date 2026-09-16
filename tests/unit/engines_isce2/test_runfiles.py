"""PERF-07: run_files parser + dependency-aware parallel executor (ADR-0026/0027)."""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import pytest

from wintersar.engines import runfiles as rf

pytestmark = pytest.mark.engine


# ------------------------------------------------------------------ parsing


def test_parse_run_files_order_names_and_jobs(run_files_dir: Path) -> None:
    steps = rf.parse_run_files(run_files_dir)
    assert [s.index for s in steps] == [1, 2, 3, 4, 5]
    assert [s.name for s in steps] == [
        "unpack_topo_reference",
        "unpack_secondary_slc",
        "average_baseline",
        "extract_burst_overlaps",
        "overlap_geo2rdr",
    ]
    assert steps[0].filename == "run_01_unpack_topo_reference"
    # comments / blank lines dropped, numProcess '&' + 'wait' format normalised
    assert steps[0].jobs == ["fake_job.sh config_reference 0.05"]
    assert steps[1].jobs == [
        "fake_job.sh config_secondary_20240113 0.3",
        "fake_job.sh config_secondary_20240125 0.3",
        "fake_job.sh config_secondary_20240206 0.3",
        "fake_job.sh config_secondary_20240218 0.3",
    ]
    assert steps[1].n_jobs == 4
    # *.job SLURM wrappers and README are ignored
    assert all(not s.filename.endswith(".job") for s in steps)


def test_parse_run_files_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        rf.parse_run_files(tmp_path / "nope")


def test_select_steps_window(run_files_dir: Path) -> None:
    steps = rf.parse_run_files(run_files_dir)
    assert [s.name for s in rf.select_steps(steps, until="average_baseline")] == [
        "unpack_topo_reference",
        "unpack_secondary_slc",
        "average_baseline",
    ]
    assert [s.index for s in rf.select_steps(steps, start=3, until=4)] == [3, 4]
    with pytest.raises(KeyError):
        rf.select_steps(steps, until="unwrap")
    with pytest.raises(ValueError):
        rf.select_steps(steps, start="average_baseline", until="unpack_topo_reference")


# ------------------------------------------------------------------ parallel cap table


def test_parallel_cap_conservative_table() -> None:
    # upstream passes numProcess for these (Stack.py) -> bounded only by cores
    for step in (
        "unpack_secondary_slc",
        "average_baseline",
        "fullBurst_geo2rdr",
        "generate_burst_igram",
        "merge_burst_igram",
        "filter_coherence",
        "unwrap",
    ):
        assert rf.parallel_cap(step, 8) == 8
    # single job / no numProcess upstream -> 1
    for step in (
        "unpack_topo_reference",
        "extract_burst_overlaps",
        "timeseries_misreg",
        "extract_stack_valid_region",
        "merge_reference_secondary_slc",
        "grid_baseline",
        "dense_offsets",
    ):
        assert rf.parallel_cap(step, 8) == 1
    # ionosphere steps conservative, unknown steps conservative
    assert rf.parallel_cap("computeIon", 8) == 1
    assert rf.parallel_cap("some_future_step", 8) == 1
    # overrides win; None override = cores; cores floor 1
    assert rf.parallel_cap("unwrap", 8, {"unwrap": 2}) == 2
    assert rf.parallel_cap("unpack_topo_reference", 8, {"unpack_topo_reference": None}) == 8
    assert rf.parallel_cap("unwrap", 0) == 1
    assert set(rf.INTERFEROGRAM_WORKFLOW_STEPS) <= set(rf.DEFAULT_STEP_PARALLEL)


# ------------------------------------------------------------------ execution


def test_run_steps_keeps_order_and_parallelises_within_step(
    run_files_dir: Path, tmp_path: Path, job_env: dict[str, str]
) -> None:
    steps = rf.select_steps(rf.parse_run_files(run_files_dir), until="average_baseline")
    log_dir = tmp_path / "logs"
    t0 = time.monotonic()
    results = rf.run_steps(steps, cores=4, log_dir=log_dir, env=job_env, cwd=tmp_path)
    elapsed = time.monotonic() - t0
    assert [r.status for r in results] == ["ok", "ok", "ok"]
    assert [r.step.name for r in results] == [s.name for s in steps]
    # step 2: 4 jobs x 0.3 s with 4 cores -> overlapping start/finish windows
    jobs = results[1].jobs
    assert results[1].n_parallel == 4
    assert all(j.status == "ok" and j.attempts == 1 for j in jobs)
    overlaps = [
        (a.started_at, a.finished_at, b.started_at, b.finished_at)
        for a in jobs
        for b in jobs
        if a is not b and a.started_at < b.finished_at and b.started_at < a.finished_at
    ]
    assert overlaps, "jobs of one step must run concurrently"
    assert elapsed < 4 * 0.3 * 0.9 + 1.0  # clearly faster than serial 1.2 s (+ slack)
    # step order: every job of step N finished before any job of step N+1 started
    for prev, nxt in itertools.pairwise(results):
        assert max(j.finished_at for j in prev.jobs) <= min(j.started_at for j in nxt.jobs)
    # per-job logs with header and captured stdout
    log = log_dir / "run_02_unpack_secondary_slc" / "job_000.log"
    text = log.read_text(encoding="utf-8")
    assert "# command: fake_job.sh config_secondary_20240113 0.3" in text
    assert "fake_job start config_secondary_20240113" in text and "# exit=0" in text
    touched = sorted(p.name for p in (tmp_path / "touched").iterdir())
    assert "config_reference" in touched and "config_baseline_20240125" in touched
    summary = json.loads((log_dir / rf.SUMMARY_FILENAME).read_text(encoding="utf-8"))
    assert summary["failure"] is None and len(summary["steps"]) == 3


def test_run_steps_cap_limits_concurrency(
    run_files_dir: Path, tmp_path: Path, job_env: dict[str, str]
) -> None:
    steps = rf.select_steps(rf.parse_run_files(run_files_dir), start=2, until=2)
    results = rf.run_steps(
        steps,
        cores=4,
        log_dir=tmp_path / "logs",
        env=job_env,
        max_parallel_per_step={"unpack_secondary_slc": 1},
    )
    assert results[0].n_parallel == 1
    jobs = sorted(results[0].jobs, key=lambda j: j.started_at)
    for a, b in itertools.pairwise(jobs):
        assert a.finished_at <= b.started_at


def test_failure_retries_once_then_stops(
    run_files_dir: Path, tmp_path: Path, job_env: dict[str, str]
) -> None:
    steps = rf.select_steps(rf.parse_run_files(run_files_dir), start=3, until=5)
    log_dir = tmp_path / "logs"
    results = rf.run_steps(steps, cores=2, log_dir=log_dir, env=job_env, retries=1)
    assert [r.status for r in results] == ["ok", "failed"]  # step 5 never started
    fail = rf.first_failure(results)
    assert fail is not None
    assert fail.step.name == "extract_burst_overlaps" and fail.job.index == 0
    assert fail.job.returncode == 1 and fail.job.attempts == 2
    assert fail.log_path == log_dir / "run_04_extract_burst_overlaps" / "job_000.log"
    text = fail.log_path.read_text(encoding="utf-8")
    assert text.count("attempt=") == 2 and "# exit=1" in text
    assert not (log_dir / "run_05_overlap_geo2rdr").exists()
    summary = rf.load_summary(log_dir)
    assert summary["failure"]["step"] == "extract_burst_overlaps"
    assert summary["failure"]["log_path"].endswith("job_000.log")
    assert not (tmp_path / "touched" / "config_overlap_geo2rdr_20240113").exists()


def test_dry_run_records_without_executing(
    run_files_dir: Path, tmp_path: Path, job_env: dict[str, str]
) -> None:
    steps = rf.parse_run_files(run_files_dir)
    results = rf.run_steps(steps, cores=2, log_dir=tmp_path / "logs", env=job_env, dry_run=True)
    assert [r.status for r in results] == ["dry_run"] * 5
    assert all(j.status == "dry_run" for r in results for j in r.jobs)
    assert not (tmp_path / "touched").exists()


def test_injected_runner_and_timeout(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(command: str, cwd: Path | None, env: dict[str, str] | None, fh) -> int:
        calls.append(command)
        fh.write("hello\n")
        return 0 if "ok" in command else 3

    (tmp_path / "rf").mkdir()
    (tmp_path / "rf" / "run_01_a").write_text("job ok 1\njob ok 2\n", encoding="utf-8")
    (tmp_path / "rf" / "run_02_b").write_text("job bad\n", encoding="utf-8")
    steps = rf.parse_run_files(tmp_path / "rf")
    results = rf.run_steps(
        steps,
        cores=2,
        log_dir=tmp_path / "logs",
        runner=runner,
        retries=0,
        max_parallel_per_step={"a": 2},
    )
    assert [r.status for r in results] == ["ok", "failed"]
    assert calls.count("job bad") == 1  # retries=0
    assert results[1].jobs[0].returncode == 3
    # subprocess runner: timeout -> 124
    (tmp_path / "rf2").mkdir()
    (tmp_path / "rf2" / "run_01_slow").write_text("sleep 5\n", encoding="utf-8")
    res = rf.run_steps(
        rf.parse_run_files(tmp_path / "rf2"),
        cores=1,
        log_dir=tmp_path / "logs2",
        timeout_s=0.2,
        retries=0,
    )
    assert res[0].status == "failed" and res[0].jobs[0].returncode == 124


# ------------------------------------------------------------------ cleanup policy (ADR-0027)


def _mk_workdir(tmp_path: Path, *, real_merged: bool = True) -> Path:
    wd = tmp_path / "wd"
    for pair in ("20240101_20240113", "20240113_20240125"):
        (wd / "interferograms" / pair).mkdir(parents=True)
        (wd / "interferograms" / pair / "burst_01.int").write_bytes(b"x")
        m = wd / "merged" / "interferograms" / pair
        m.mkdir(parents=True)
        if real_merged:
            (m / "fine.int").write_bytes(b"\x00\x01" * 16)
        else:
            (m / "fine.int").write_text('<VRTDataset rasterXSize="1"/>', encoding="utf-8")
    for d in (
        "ESD",
        "coarse_offsets",
        "coarse_interferograms",
        "secondarys",
        "reference",
        "geom_reference",
        "coreg_secondarys",
        "misreg",
        "baselines",
        "stack",
        "configs",
        "run_files",
    ):
        (wd / d).mkdir(parents=True, exist_ok=True)
    return wd


def test_cleanup_targets_table(tmp_path: Path) -> None:
    wd = _mk_workdir(tmp_path)
    assert rf.cleanup_targets("merge_burst_igram", "none", wd) == []
    stage = rf.cleanup_targets("merge_burst_igram", "stage", wd)
    assert sorted(p.name for p in stage) == ["20240101_20240113", "20240113_20240125"]
    assert all(p.parent == wd / "interferograms" for p in stage)
    # stage policy never touches NESD temporaries or SLCs
    assert rf.cleanup_targets("unwrap", "stage", wd) == []
    aggressive = rf.cleanup_targets("unwrap", "aggressive", wd)
    assert sorted(p.name for p in aggressive) == [
        "ESD",
        "coarse_interferograms",
        "coarse_offsets",
        "secondarys",
    ]
    # after other steps nothing is removed
    assert rf.cleanup_targets("filter_coherence", "stage", wd) == []
    assert rf.cleanup_targets("average_baseline", "aggressive", wd) == []
    # never-delete set is never returned
    for policy in ("stage", "aggressive"):
        for step in rf.INTERFEROGRAM_WORKFLOW_STEPS:
            for t in rf.cleanup_targets(step, policy, wd):  # type: ignore[arg-type]
                assert not (t.parent == wd and t.name in rf.NEVER_DELETE)


def test_cleanup_guard_keeps_bursts_when_merged_is_virtual(tmp_path: Path) -> None:
    wd = _mk_workdir(tmp_path, real_merged=False)
    assert rf.cleanup_targets("merge_burst_igram", "stage", wd) == []
    assert rf.cleanup_targets("merge_burst_igram", "aggressive", wd) == []


def test_make_cleanup_hook_removes_dirs(tmp_path: Path) -> None:
    wd = _mk_workdir(tmp_path)
    step = rf.RunStep(
        index=14, name="merge_burst_igram", path=wd / "run_files" / "run_14_merge_burst_igram"
    )
    removed = rf.make_cleanup("stage", wd)(step)
    assert len(removed) == 2 and not (wd / "interferograms" / "20240101_20240113").exists()
    assert (wd / "merged" / "interferograms" / "20240101_20240113" / "fine.int").exists()
    dry = rf.make_cleanup("aggressive", wd, dry_run=True)(
        rf.RunStep(16, "unwrap", wd / "run_files" / "run_16_unwrap")
    )
    assert {p.name for p in dry} == {"ESD", "coarse_offsets", "coarse_interferograms", "secondarys"}
    assert (wd / "ESD").exists()


# ------------------------------------------------------------------ resumable state


def test_state_and_pending_steps(run_files_dir: Path, tmp_path: Path) -> None:
    steps = rf.parse_run_files(run_files_dir)
    assert rf.pending_steps(steps, tmp_path) == steps
    rf.mark_completed(tmp_path, steps[0])
    rf.mark_completed(tmp_path, steps[1])
    assert rf.completed_steps(tmp_path) == {"unpack_topo_reference", "unpack_secondary_slc"}
    assert [s.name for s in rf.pending_steps(steps, tmp_path)] == [
        "average_baseline",
        "extract_burst_overlaps",
        "overlap_geo2rdr",
    ]
    rf.mark_completed(tmp_path, steps[1])  # idempotent
    assert len(rf.load_state(tmp_path)["completed"]) == 2
