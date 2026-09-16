"""ISCE2 topsStack ``run_files`` parser + dependency-aware parallel executor (PERF-07).

topsStack (``stackSentinel.py``) only *generates* ``run_files/run_NN_<step>`` shell scripts;
the user is expected to run them one after another ("User needs to execute each run file
in order. The order is specified by the index number of the run file name."). Each line of a run file is one independent job (``SentinelWrapper.py -c
<config>``); when ``--num_proc N`` is given upstream appends ``&`` to the lines and inserts
``wait`` every N lines. This module keeps the **step order** (dependencies live between
steps), runs the **jobs of one step concurrently** bounded by ``cores`` and a per-step cap,
captures every job's stdout/stderr into ``log_dir/<run_file>/job_NNN.log``, retries a failed
job once and stops at the first step that still fails so that :mod:`wintersar.diagnose` can
look at the failing job's log.

Verified facts (ADR-0026 / ADR-0027):

* run file naming ``'run_{:02d}_<step>'`` and step names
  # source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/stackSentinel.py
* job line ``text_cmd + 'SentinelWrapper.py -c ' + configName`` (+ ``' &'`` / ``'wait'`` when
  ``numProcess > 1``) — ``run.write_wrapper_config2run_file``
  # source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/Stack.py
* which step methods pass ``self.numProcess`` (i.e. upstream itself considers their jobs
  independent) — see :data:`DEFAULT_STEP_PARALLEL` (same source).
* "User needs to execute each run file in order. The order is specified by the index number
  of the run file name." Ionosphere steps are capped at 1 conservatively (the README's
  sequential-execution advice for them was not re-verified verbatim, docs/open-questions.md).
  # source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/README.md
* merged interferogram ``fine.int`` is a real multilooked file while ``fine.int.full.vrt`` is a
  VRT over the burst products (``mergeBursts.py``: ``multilook()`` writes ``outfile`` from
  ``outfile + '.full'``); the cleanup guard in :func:`cleanup_targets` relies on this.
  # source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/mergeBursts.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from wintersar.util.masking import mask_mapping, mask_text

RUN_FILE_RE = re.compile(r"^run_(\d{2,3})_(.+)$")  # source: stackSentinel.py 'run_{:02d}_<step>'
JOB_LOG_FMT = "job_{index:03d}.log"
SUMMARY_FILENAME = "runfiles_summary.json"
STATE_FILENAME = "runfiles_state.json"

CleanupPolicy = Literal["none", "stage", "aggressive"]
JobStatus = Literal["ok", "failed", "skipped", "dry_run"]
StepStatus = Literal["ok", "failed", "skipped", "dry_run"]

#: topsStack steps whose jobs upstream runs concurrently when ``--num_proc > 1``
#: (``Stack.py`` passes ``self.numProcess`` to ``write_wrapper_config2run_file``).
#: ``None`` = bounded only by ``cores``. Steps upstream always writes serially (single job or
#: no ``numProcess``) get cap 1. Unknown step names are also capped at 1 (conservative).
# source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/Stack.py
DEFAULT_STEP_PARALLEL: dict[str, int | None] = {
    # interferogram / correlation / slc workflows (stackSentinel.py slcStack + extras)
    "unpack_topo_reference": 1,  # single job; topo.py parallelises internally (numProcess4topo)
    "unpack_secondary_slc": None,  # one job per secondary date, numProcess
    "average_baseline": None,  # one job per secondary date, numProcess
    "extract_burst_overlaps": 1,  # single job (subsetReference.py)
    "overlap_geo2rdr": None,  # geo2rdr_offset, numProcess
    "overlap_resample": None,  # resample_with_carrier, numProcess
    "pairs_misreg": None,  # pairs_misregistration, numProcess
    "timeseries_misreg": 1,  # single job (invertMisreg.py x2)
    "fullBurst_geo2rdr": None,  # geo2rdr_offset(fullBurst), numProcess
    "fullBurst_resample": None,  # resample_with_carrier(fullBurst), numProcess
    "extract_stack_valid_region": 1,  # single job (extractCommonValidRegion.py)
    "merge_reference_secondary_slc": 1,  # mergeReference/mergeSecondarySLC: no numProcess
    "grid_baseline": 1,  # gridBaseline: no numProcess
    "generate_burst_igram": None,  # generate_burstIgram, numProcess
    "merge_burst_igram": None,  # igram_mergeBurst, numProcess
    "filter_coherence": None,  # filter_coherence, numProcess
    "unwrap": None,  # unwrap, numProcess (memory-bound: see Isce2 adapter cap)
    "dense_offsets": 1,  # denseOffsets: no numProcess
    # ionosphere workflow: conservative cap 1 (Stack.py passes numProcess only for
    # subband_and_resamp / generateIgram_ion; override via max_parallel_per_step if needed)
    "subband_and_resamp": 1,
    "generateIgram_ion": 1,
    "mergeBurstsIon": 1,
    "unwrap_ion": 1,
    "look_ion": 1,
    "computeIon": 1,
    "filtIon": 1,
    "invertIon": 1,
    "filtIonShift": 1,
    "invertIonShift": 1,
    "burstRampIon": 1,
    "mergeBurstRampIon": 1,
}

#: interferogram-workflow step order (stackSentinel.py interferogramStack, NESD coregistration)
INTERFEROGRAM_WORKFLOW_STEPS: tuple[str, ...] = (
    "unpack_topo_reference",
    "unpack_secondary_slc",
    "average_baseline",
    "extract_burst_overlaps",
    "overlap_geo2rdr",
    "overlap_resample",
    "pairs_misreg",
    "timeseries_misreg",
    "fullBurst_geo2rdr",
    "fullBurst_resample",
    "extract_stack_valid_region",
    "merge_reference_secondary_slc",
    "generate_burst_igram",
    "merge_burst_igram",
    "filter_coherence",
    "unwrap",
)

#: directories that are never removed by any cleanup policy (inputs of later steps, of
#: MintPy ``prep_isce`` or of topsStack's own update mode, ADR-0027/0029)
NEVER_DELETE: tuple[str, ...] = (
    "reference",
    "geom_reference",
    "coreg_secondarys",
    "misreg",
    "baselines",
    "merged",
    "stack",
    "configs",
    "run_files",
)


# ---------------------------------------------------------------------- data classes


@dataclass(frozen=True)
class RunStep:
    """One ``run_files/run_NN_<name>`` script."""

    index: int
    name: str
    path: Path
    jobs: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def n_jobs(self) -> int:
        return len(self.jobs)


@dataclass
class JobResult:
    step: str
    index: int
    command: str
    log_path: Path
    status: JobStatus = "skipped"
    returncode: int | None = None
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["log_path"] = str(self.log_path)
        d["duration_s"] = self.duration_s
        return d


@dataclass
class StepResult:
    step: RunStep
    status: StepStatus
    jobs: list[JobResult] = field(default_factory=list)
    n_parallel: int = 1
    started_at: float | None = None
    finished_at: float | None = None
    cleanup_removed: list[Path] = field(default_factory=list)

    @property
    def failed_job(self) -> JobResult | None:
        return next((j for j in self.jobs if j.status == "failed"), None)

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.step.index,
            "name": self.step.name,
            "run_file": str(self.step.path),
            "status": self.status,
            "n_jobs": self.step.n_jobs,
            "n_parallel": self.n_parallel,
            "duration_s": self.duration_s,
            "jobs": [j.to_dict() for j in self.jobs],
            "cleanup_removed": [str(p) for p in self.cleanup_removed],
        }


@dataclass(frozen=True)
class Failure:
    """First failing job of a :func:`run_steps` result (for diagnose)."""

    step: RunStep
    job: JobResult

    @property
    def log_path(self) -> Path:
        return self.job.log_path


# ---------------------------------------------------------------------- parsing


def parse_run_file(path: Path) -> list[str]:
    """Job lines of one run file.

    Blank lines and ``#`` comments are dropped, ``wait`` lines (numProcess > 1 format) are
    dropped and a trailing ``&`` is stripped: the executor decides the parallelism itself.
    A ``text_cmd`` prefix (e.g. ``source ~/.bash_profile;``) is kept as part of the command.
    """
    jobs: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "wait":
            continue
        if line.endswith("&"):
            line = line[:-1].rstrip()
        if line:
            jobs.append(line)
    return jobs


def parse_run_files(run_dir: Path) -> list[RunStep]:
    """All ``run_NN_<step>`` scripts of ``run_dir`` in index order.

    Files that do not match the naming (``*.job`` SLURM wrappers, editor backups) are ignored.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        msg = f"run_files directory not found: {mask_text(str(run_dir))}"
        raise FileNotFoundError(msg)
    steps: list[RunStep] = []
    for p in sorted(run_dir.iterdir()):
        if not p.is_file():
            continue
        m = RUN_FILE_RE.match(p.name)
        if m is None or p.suffix in {".job", ".log", ".bak"}:
            continue
        steps.append(
            RunStep(index=int(m.group(1)), name=m.group(2), path=p, jobs=parse_run_file(p))
        )
    steps.sort(key=lambda s: (s.index, s.name))
    return steps


def select_steps(
    steps: Sequence[RunStep],
    start: str | int | None = None,
    until: str | int | None = None,
) -> list[RunStep]:
    """Sub-sequence ``[start, until]`` (inclusive) by step name or index; order preserved."""

    def _pos(key: str | int | None, default: int) -> int:
        if key is None:
            return default
        for i, s in enumerate(steps):
            if (isinstance(key, int) and s.index == key) or s.name == key:
                return i
        msg = f"unknown run step {key!r}; known: {[s.name for s in steps]}"
        raise KeyError(msg)

    if not steps:
        return []
    a = _pos(start, 0)
    b = _pos(until, len(steps) - 1)
    if b < a:
        msg = f"until step {until!r} comes before start step {start!r}"
        raise ValueError(msg)
    return list(steps[a : b + 1])


def parallel_cap(
    step_name: str,
    cores: int,
    overrides: Mapping[str, int | None] | None = None,
    table: Mapping[str, int | None] = DEFAULT_STEP_PARALLEL,
) -> int:
    """Concurrent jobs allowed for ``step_name``: ``min(cores, cap)``; unknown steps → 1."""
    cores = max(1, int(cores))
    if overrides is not None and step_name in overrides:
        cap = overrides[step_name]
    elif step_name in table:
        cap = table[step_name]
    else:
        cap = 1
    return cores if cap is None else max(1, min(cores, int(cap)))


# ---------------------------------------------------------------------- cleanup policy


def _is_vrt(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError:
        return True
    return head.lstrip().startswith(b"<VRTDataset")


def _merged_igram_is_real(merged_pair_dir: Path) -> bool:
    """True when ``merged/interferograms/<pair>/fine.int`` is a real multilooked raster.

    With ``useVirtualFiles='True'`` and 1x1 looks the merged product is only a VRT over the
    burst interferograms, so the burst directory must not be deleted (mergeBursts.py).
    """
    fine = merged_pair_dir / "fine.int"
    if not fine.is_file() or fine.stat().st_size == 0:
        return False
    return not _is_vrt(fine)


def cleanup_targets(step_name: str, policy: CleanupPolicy, workdir: Path) -> list[Path]:
    """Directories that are safe to delete *after* ``step_name`` completed under ``policy``.

    Table (ADR-0027; conservative — only what the topsStack sources prove is not read
    again):

    * ``none``       → nothing.
    * ``stage``      → after ``merge_burst_igram``: burst-level ``interferograms/<pair>/`` of
      every pair whose ``merged/interferograms/<pair>/fine.int`` is a real (non-VRT) file.
      Later steps (``filter_coherence``, ``unwrap``) read only ``merged/`` (Stack.py).
    * ``aggressive`` → ``stage`` plus, after the workflow's last step (``unwrap``, or
      ``filter_coherence`` for the correlation workflow): ``ESD/``, ``coarse_offsets/``,
      ``coarse_interferograms/`` (NESD temporaries consumed by ``pairs_misreg``) and the
      unpacked ``secondarys/`` SLCs (re-unpacked from the SAFE files by
      ``unpack_secondary_slc`` when the stack is updated). Never: :data:`NEVER_DELETE`.
    """
    workdir = Path(workdir)
    targets: list[Path] = []
    if policy == "none":
        return targets
    if step_name == "merge_burst_igram":
        burst_root = workdir / "interferograms"
        merged_root = workdir / "merged" / "interferograms"
        if burst_root.is_dir():
            for pair_dir in sorted(p for p in burst_root.iterdir() if p.is_dir()):
                if _merged_igram_is_real(merged_root / pair_dir.name):
                    targets.append(pair_dir)
    if policy == "aggressive" and step_name in {"unwrap", "filter_coherence"}:
        for name in ("ESD", "coarse_offsets", "coarse_interferograms", "secondarys"):
            d = workdir / name
            if d.is_dir():
                targets.append(d)
    return [t for t in targets if t.name not in NEVER_DELETE or t.parent != workdir]


def make_cleanup(
    policy: CleanupPolicy, workdir: Path, dry_run: bool = False
) -> Callable[[RunStep], list[Path]]:
    """Build the ``cleanup`` hook for :func:`run_steps` from a policy name."""

    def _hook(step: RunStep) -> list[Path]:
        removed: list[Path] = []
        for target in cleanup_targets(step.name, policy, workdir):
            if not dry_run:
                shutil.rmtree(target, ignore_errors=True)
            removed.append(target)
        return removed

    return _hook


# ---------------------------------------------------------------------- execution


def _shell() -> str | None:
    # topsStack run files are shell scripts; ``text_cmd`` may use bash-only ``source``.
    return shutil.which("bash") or shutil.which("sh")


def _write_header(fh: Any, job: JobResult, attempt: int, cwd: Path | None) -> None:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    fh.write(f"# wintersar runfiles job step={job.step} index={job.index} attempt={attempt}\n")
    fh.write(f"# started={ts} cwd={mask_text(str(cwd) if cwd else str(Path.cwd()))}\n")
    fh.write(f"# command: {mask_text(job.command)}\n")
    fh.flush()


def run_job(
    job: JobResult,
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    retries: int,
    timeout_s: float | None,
    runner: Callable[[str, Path | None, Mapping[str, str] | None, Any], int] | None = None,
) -> JobResult:
    """Run one job (``retries`` extra attempts on non-zero exit); log appended per attempt."""
    job.log_path.parent.mkdir(parents=True, exist_ok=True)
    job.started_at = time.monotonic()
    rc = -1
    for attempt in range(1, max(0, retries) + 2):
        job.attempts = attempt
        with job.log_path.open("a", encoding="utf-8") as fh:
            _write_header(fh, job, attempt, cwd)
            if runner is not None:
                rc = runner(job.command, cwd, env, fh)
            else:
                rc = _subprocess_runner(job.command, cwd, env, fh, timeout_s)
            fh.write(f"# exit={rc}\n")
        if rc == 0:
            break
    job.returncode = rc
    job.finished_at = time.monotonic()
    job.status = "ok" if rc == 0 else "failed"
    return job


def _subprocess_runner(
    command: str,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    fh: Any,
    timeout_s: float | None,
) -> int:
    shell = _shell()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable=shell,
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        fh.write(f"# wintersar: job timed out after {timeout_s} s\n")
        return 124
    except OSError as e:
        fh.write(f"# wintersar: could not start job: {mask_text(str(e))}\n")
        return 127
    return int(proc.returncode)


def run_steps(
    steps: Sequence[RunStep],
    cores: int,
    log_dir: Path,
    max_parallel_per_step: Mapping[str, int | None] | None = None,
    retries: int = 1,
    dry_run: bool = False,
    cleanup: Callable[[RunStep], list[Path]] | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float | None = None,
    on_step: Callable[[StepResult], None] | None = None,
    runner: Callable[[str, Path | None, Mapping[str, str] | None, Any], int] | None = None,
) -> list[StepResult]:
    """Execute ``steps`` in order; jobs inside a step run concurrently.

    * concurrency per step = :func:`parallel_cap` (``cores`` x conservative table x
      ``max_parallel_per_step`` overrides),
    * each job's stdout+stderr → ``log_dir/<run_file>/job_NNN.log`` (one header per attempt),
    * a failing job is retried ``retries`` times; if it still fails, no further jobs of that
      step are started, running ones are awaited, the step is marked ``failed`` and the
      function **returns** (the remaining steps are not touched). Use :func:`first_failure`
      to find the job + log path for ``wintersar diagnose``.
    * ``cleanup(step)`` runs after each successful step (disk policy hooks, ADR-0027).
    * ``dry_run`` only records what would run (status ``dry_run``) and writes the summary.

    A ``runfiles_summary.json`` (masked paths) is rewritten after every step so a crash
    still leaves a machine-readable state for diagnose/resume.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    results: list[StepResult] = []
    for step in steps:
        n_par = parallel_cap(step.name, cores, max_parallel_per_step)
        step_log_dir = log_dir / step.filename
        jobs = [
            JobResult(
                step=step.name,
                index=i,
                command=cmd,
                log_path=step_log_dir / JOB_LOG_FMT.format(index=i),
            )
            for i, cmd in enumerate(step.jobs)
        ]
        res = StepResult(
            step=step, status="dry_run" if dry_run else "ok", jobs=jobs, n_parallel=n_par
        )
        res.started_at = time.monotonic()
        if dry_run:
            for j in jobs:
                j.status = "dry_run"
            res.finished_at = time.monotonic()
            results.append(res)
            _write_summary(log_dir, results)
            if on_step is not None:
                on_step(res)
            continue
        failed = _run_step_jobs(
            jobs, n_par, cwd=cwd, env=env, retries=retries, timeout_s=timeout_s, runner=runner
        )
        res.finished_at = time.monotonic()
        if failed:
            res.status = "failed"
            results.append(res)
            _write_summary(log_dir, results)
            if on_step is not None:
                on_step(res)
            return results
        if cleanup is not None:
            res.cleanup_removed = list(cleanup(step))
        results.append(res)
        _write_summary(log_dir, results)
        if on_step is not None:
            on_step(res)
    return results


def _run_step_jobs(
    jobs: list[JobResult],
    n_par: int,
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    retries: int,
    timeout_s: float | None,
    runner: Callable[[str, Path | None, Mapping[str, str] | None, Any], int] | None,
) -> bool:
    """Run ``jobs`` with at most ``n_par`` concurrent; stop submitting after a failure.

    Returns ``True`` when any job failed.
    """
    if not jobs:
        return False
    stop = threading.Event()
    pending = list(jobs)
    running: dict[Future[JobResult], JobResult] = {}
    failed = False
    with ThreadPoolExecutor(max_workers=max(1, n_par)) as pool:
        while pending or running:
            while pending and len(running) < n_par and not stop.is_set():
                job = pending.pop(0)
                fut = pool.submit(
                    run_job,
                    job,
                    cwd=cwd,
                    env=env,
                    retries=retries,
                    timeout_s=timeout_s,
                    runner=runner,
                )
                running[fut] = job
            if not running:
                break
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for fut in done:
                job = running.pop(fut)
                exc = fut.exception()
                if exc is not None:
                    job.status = "failed"
                    job.returncode = job.returncode if job.returncode is not None else -1
                    job.finished_at = time.monotonic()
                    with job.log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"# wintersar: executor error: {mask_text(str(exc))}\n")
                if job.status == "failed":
                    failed = True
                    stop.set()
            if stop.is_set():
                pending.clear()
    return failed


def first_failure(results: Sequence[StepResult]) -> Failure | None:
    for res in results:
        if res.status == "failed":
            job = res.failed_job
            if job is not None:
                return Failure(step=res.step, job=job)
    return None


def _write_summary(log_dir: Path, results: Sequence[StepResult]) -> Path:
    path = log_dir / SUMMARY_FILENAME
    data: dict[str, Any] = {
        "written": datetime.now(UTC).isoformat(timespec="seconds"),
        "steps": [r.to_dict() for r in results],
        "failure": None,
    }
    fail = first_failure(results)
    if fail is not None:
        data["failure"] = {
            "step": fail.step.name,
            "run_file": str(fail.step.path),
            "job_index": fail.job.index,
            "command": fail.job.command,
            "returncode": fail.job.returncode,
            "attempts": fail.job.attempts,
            "log_path": str(fail.job.log_path),
        }
    path.write_text(
        json.dumps(mask_mapping(data), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def load_summary(log_dir: Path) -> dict[str, Any]:
    p = Path(log_dir) / SUMMARY_FILENAME
    if not p.exists():
        return {}
    return dict(json.loads(p.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------- resumable state


def load_state(workdir: Path) -> dict[str, Any]:
    p = Path(workdir) / STATE_FILENAME
    if not p.exists():
        return {"completed": []}
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {"completed": []}


def save_state(workdir: Path, state: Mapping[str, Any]) -> Path:
    p = Path(workdir) / STATE_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def mark_completed(workdir: Path, step: RunStep) -> None:
    state = load_state(workdir)
    done = [d for d in state.get("completed", []) if d.get("name") != step.name]
    done.append(
        {
            "name": step.name,
            "index": step.index,
            "run_file": step.filename,
            "n_jobs": step.n_jobs,
            "finished": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    state["completed"] = done
    save_state(workdir, state)


def completed_steps(workdir: Path) -> set[str]:
    return {str(d.get("name")) for d in load_state(workdir).get("completed", [])}


def pending_steps(steps: Sequence[RunStep], workdir: Path) -> list[RunStep]:
    """Steps not yet recorded as completed in ``workdir/runfiles_state.json`` (in order)."""
    done = completed_steps(workdir)
    return [s for s in steps if s.name not in done]
