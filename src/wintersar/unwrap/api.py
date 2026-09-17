"""Unwrap stage executor (plan §5.4, R-06, PERF-03/PERF-04).

:func:`run_unwrap` is what the pipeline (and ``wintersar unwrap run``) calls:

1. load the interferogram stack (``igrams.npz`` or an engine directory,
   :mod:`wintersar.unwrap.inputs`),
2. build the :class:`~wintersar.pipeline.config.UnwrapCfg` from ``params``,
3. :func:`~wintersar.unwrap.scheduler.choose_strategy` → :class:`UnwrapPlan`,
4. unwrap every interferogram in a process pool (``n_parallel`` workers; a thread pool
   for the in-process test backends), tiling in the executor when the backend cannot tile
   natively,
5. write ``out_dir/unw.npz`` (``unw``, ``conncomp``, ``pairs``, ``dates``) and
   ``out_dir/stats.json`` (plan, connected-component counts, tile-seam jumps, wall time,
   peak RSS, tile scratch directories for SNAPHU assemble-only re-use).

``params`` keys: ``method``, ``cost``, ``init``, ``coherence_threshold``, ``tiles``,
``memory_mb_per_mpixel``, ``nproc_per_igram``, ``save_cost_file``, ``mask`` (the ``unwrap``
config section) plus executor private keys (``_out_dir``, ``_cores``, ``_memory_gb``,
``_n_parallel``, ``_tile_offset_cycles`` — the last one is a test hook for the seam
detector). The pipeline executor passes the config section **nested**
(``params["unwrap"]["cost"]``, ``canonical_params`` in :mod:`wintersar.pipeline.dag`) while
the CLI and the tests pass it flat; :func:`flatten_params` accepts both (top level wins,
the same rule as :func:`wintersar.engines._unwrap_common.unwrap_cfg`).

The ``.npz`` interchange loads whole arrays per worker; the chunked Zarr store (PERF-08)
replaces it for real stacks.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing as mp
import shutil
import threading
import time
from collections.abc import Mapping
from concurrent.futures import (
    Executor,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.engines.base import EngineNotAvailableError
from wintersar.i18n import t
from wintersar.io.igrams import IgramStack
from wintersar.io.schemas import Artifact, Artifacts, Finding, Severity
from wintersar.pipeline.config import MaskCfg, TilesCfg, UnwrapCfg
from wintersar.unwrap import backends
from wintersar.unwrap.inputs import (
    NPZ_FORMAT,
    UnwrapInputError,
    load_igrams,
    write_worker_stack,
)
from wintersar.unwrap.masks import apply_mask, combine_masks, mask_stats, masked_conncomp
from wintersar.unwrap.scheduler import REASON_PREFIX, UnwrapPlan, choose_strategy, fringe_density
from wintersar.unwrap.tiling import boundary_jumps, merge_labels, merge_tiles, tile_grid
from wintersar.util.masking import mask_mapping, mask_text
from wintersar.util.sysinfo import MachineSpec

__all__ = [
    "PARAM_KEYS",
    "STATS_FILE",
    "UNW_FILE",
    "UnwrapFailedError",
    "UnwrapInputError",
    "cfg_from_params",
    "flatten_params",
    "resolve_plan",
    "run_unwrap",
    "stack_fringe_density",
]

#: every ``UnwrapCfg`` field (derived, so a new config field is never silently ignored)
PARAM_KEYS: tuple[str, ...] = tuple(UnwrapCfg.model_fields)
#: the config section name the pipeline executor nests the parameters under
SECTION = "unwrap"
STATS_FILE = "stats.json"
UNW_FILE = "unw.npz"
INPUT_DIR = "input"
PARTS_DIR = "parts"
TILES_DIR = "tiles"
LOG_FILE = "unwrap.log"
_FRINGE_SAMPLES = 3


class UnwrapFailedError(RuntimeError):
    """Some interferograms failed; ``findings`` carries one ``UNW-004`` per failure."""

    def __init__(self, failed: list[str], findings: list[Finding]) -> None:
        super().__init__(f"unwrap failed for {len(failed)} interferogram(s): {failed}")
        self.failed = failed
        self.findings = findings


# ---------------------------------------------------------------------------- config


def _parse_tiles(value: Any) -> Any:
    if value is None or value == "auto" or isinstance(value, TilesCfg):
        return value if value is not None else "auto"
    if isinstance(value, str):
        rows, _, cols = value.lower().replace("x", "x").partition("x")
        return {"rows": int(rows), "cols": int(cols)}
    if isinstance(value, list | tuple) and len(value) == 2:
        return {"rows": int(value[0]), "cols": int(value[1])}
    return value


def flatten_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the nested ``unwrap`` config section into the top level (top level wins).

    ``Config.stage_params("unwrap")`` — and therefore the executor — yields
    ``{"unwrap": {...}}``; ``wintersar unwrap run`` and the tests pass the flat form. Same
    precedence rule as :func:`wintersar.engines._unwrap_common.unwrap_cfg`, so the adapters
    and the scheduler always see the same values. Private keys pass through untouched.
    """
    nested = params.get(SECTION)
    out: dict[str, Any] = dict(nested) if isinstance(nested, Mapping) else {}
    out.update({k: v for k, v in params.items() if k != SECTION})
    return out


def cfg_from_params(params: Mapping[str, Any]) -> UnwrapCfg:
    """``UnwrapCfg`` from the stage parameter mapping (unknown/private keys ignored).

    Accepts the flat and the nested (``{"unwrap": {...}}``) form via
    :func:`flatten_params`. Test backends (``truth``/``identity``) are not valid
    ``unwrap.method`` values; they are planned as ``auto`` and substituted afterwards by
    :func:`resolve_plan`.
    """
    flat = flatten_params(params)
    picked: dict[str, Any] = {}
    for key in PARAM_KEYS:
        value = flat.get(key)
        if value is None:
            continue
        picked[key] = value
    method = str(picked.get("method", "auto"))
    if backends.is_test_backend(method):
        picked["method"] = "auto"
    if "tiles" in picked:
        picked["tiles"] = _parse_tiles(picked["tiles"])
    mask = picked.get("mask")
    if isinstance(mask, bool):
        picked["mask"] = MaskCfg(water=mask, layover=mask, coherence=mask)
    return UnwrapCfg.model_validate(picked)


def _machine_from_params(machine: MachineSpec, params: dict[str, Any]) -> MachineSpec:
    """Apply the executor's ``_cores`` / ``_memory_gb`` private keys (already budgeted)."""
    cores = params.get("_cores")
    memory_gb = params.get("_memory_gb")
    if cores is None and memory_gb is None:
        return machine
    return machine.budget(
        cores="auto" if cores is None else int(cores),
        memory_gb="auto" if memory_gb is None else float(memory_gb),
        memory_fraction=1.0,
    )


def stack_fringe_density(stack: IgramStack, cfg: UnwrapCfg) -> float | None:
    """Median fringe density over up to three interferograms (first, middle, last)."""
    n = stack.n_pairs
    if n == 0:
        return None
    idx = sorted({0, n // 2, n - 1})[:_FRINGE_SAMPLES]
    values: list[float] = []
    for i in idx:
        mask = combine_masks(
            stack.coherence[i],
            cfg.coherence_threshold if cfg.mask.coherence else 0.0,
            water=stack.mask_for(i) if (cfg.mask.water or cfg.mask.layover) else None,
        )
        f = fringe_density(stack.wrapped[i], mask)
        if np.isfinite(f):
            values.append(f)
    return float(np.median(values)) if values else None


def resolve_plan(
    shape: tuple[int, int],
    n_igrams: int,
    machine: MachineSpec,
    cfg: UnwrapCfg,
    requested_method: str | None = None,
    fringe: float | None = None,
    available: list[str] | None = None,
    n_parallel_override: int | None = None,
) -> UnwrapPlan:
    """:func:`choose_strategy` plus the test-backend / ``_n_parallel`` substitutions."""
    requested = requested_method or str(cfg.method)
    test_backend = backends.is_test_backend(requested)
    if available is None and cfg.method == "auto" and not test_backend:
        available = backends.available_backends()
    if available is not None:
        # the plan unwraps one interferogram at a time, so a stack-only (3-D space-time)
        # engine such as spurt can never serve it — dropping it here makes ``auto`` fall
        # through to ``method_none_available`` instead of planning a run that must fail.
        available = backends.two_d_backends(available)
    plan = choose_strategy(shape, n_igrams, machine, cfg, fringe, available)
    if test_backend:
        reasons = [k for k in plan.reason_keys if not k.startswith(REASON_PREFIX + "method_")]
        plan = replace(
            plan,
            method=requested,
            reason_keys=[*reasons, REASON_PREFIX + "method_explicit"],
            executor="thread",
        )
    if n_parallel_override is not None and n_parallel_override >= 1:
        plan = replace(plan, n_parallel=int(n_parallel_override))
    return plan


# ---------------------------------------------------------------------------- workers


@dataclass
class _Job:
    index: int
    pair: str
    igram_path: str
    method: str
    plan: dict[str, Any]
    coherence_threshold: float
    use_geo_mask: bool
    use_coherence: bool
    backend_params: dict[str, Any]
    parts_dir: str
    native_tiles: bool
    want_truth: bool
    tile_offset_cycles: dict[str, float] = field(default_factory=dict)


@dataclass
class _JobResult:
    index: int
    pair: str
    unw_path: str
    conncomp_path: str
    stats: dict[str, Any]


def _jump_summary(jumps: dict[str, Any] | None) -> dict[str, Any] | None:
    if jumps is None:
        return None
    return {k: v for k, v in jumps.items() if k != "boundaries"} | {
        "boundaries": [
            {k: v for k, v in b.items() if k != "histogram"} for b in jumps["boundaries"]
        ]
    }


def _unwrap_one(job: _Job) -> _JobResult:
    """Unwrap interferogram ``job.index`` (runs in a worker process or thread)."""
    t0 = time.perf_counter()
    with np.load(job.igram_path, allow_pickle=False) as z:
        wrapped = np.asarray(z["wrapped"][job.index], dtype=np.float32)
        coh = np.asarray(z["coherence"][job.index], dtype=np.float32)
        geo_mask: NDArray[np.bool_] | None = None
        if job.use_geo_mask and "mask" in z.files:
            m = z["mask"]
            geo_mask = np.asarray(m[job.index] if m.ndim == 3 else m, dtype=bool)
        truth: NDArray[np.float32] | None = None
        if job.want_truth:
            if "unw_true" not in z.files:
                msg = "the 'truth' backend needs an 'unw_true' array in the stack"
                raise ValueError(msg)
            truth = np.asarray(z["unw_true"][job.index], dtype=np.float32)

    threshold = job.coherence_threshold if job.use_coherence else 0.0
    mask = combine_masks(coh, threshold, water=geo_mask)
    unwrapper = backends.get_unwrapper(job.method)
    params = dict(job.backend_params)
    params["_pair"] = job.pair
    params["_index"] = job.index
    rows, cols, overlap = int(job.plan["rows"]), int(job.plan["cols"]), int(job.plan["overlap_px"])
    shape = (int(wrapped.shape[0]), int(wrapped.shape[1]))
    n_tiles = rows * cols
    jumps: dict[str, Any] | None = None
    tile_dir: str | None = None
    backend_stats: dict[str, Any]

    if n_tiles > 1 and not job.native_tiles:
        tiles = tile_grid(shape, rows, cols, overlap)
        params["ntiles"] = (1, 1)
        params["tile_overlap"] = 0
        unws: list[NDArray[np.float32]] = []
        labels: list[NDArray[np.integer[Any]]] = []
        for tile in tiles:
            p = dict(params)
            p["_tile"] = tile.to_dict()
            if truth is not None:
                p["_truth"] = truth[tile.extent]
            k = job.tile_offset_cycles.get(f"{tile.row},{tile.col}")
            if k:
                p["_offset_cycles"] = k
            res = backends.call_unwrapper(
                unwrapper, wrapped[tile.extent], coh[tile.extent], mask[tile.extent], p
            )
            unws.append(res.unw)
            labels.append(res.conncomp)
        jumps = boundary_jumps(unws, tiles)
        unw = merge_tiles(unws, tiles, shape)
        conncomp: NDArray[np.integer[Any]] = merge_labels(labels, tiles, shape)
        backend_stats = {"backend": unwrapper.name, "n_tiles": n_tiles, "tiled_by": "executor"}
    else:
        if truth is not None:
            params["_truth"] = truth
        k0 = job.tile_offset_cycles.get("0,0")
        if k0 and n_tiles == 1:
            params["_offset_cycles"] = k0
        res = backends.call_unwrapper(unwrapper, wrapped, coh, mask, params)
        unw, conncomp, backend_stats = res.unw, res.conncomp, dict(res.stats)
        if n_tiles > 1:
            jumps = boundary_jumps(unw, tile_grid(shape, rows, cols, overlap))
        td = backend_stats.get("tile_dir")
        tile_dir = str(td) if td else None

    unw = apply_mask(unw, mask)
    conncomp = masked_conncomp(conncomp, mask)
    n_conncomp = int(np.unique(conncomp[conncomp > 0]).size)
    parts = Path(job.parts_dir)
    parts.mkdir(parents=True, exist_ok=True)
    unw_path = parts / f"{job.index:05d}_unw.npy"
    cc_path = parts / f"{job.index:05d}_conncomp.npy"
    np.save(unw_path, unw)
    np.save(cc_path, conncomp)
    stats: dict[str, Any] = {
        "pair": job.pair,
        "index": job.index,
        "n_conncomp": n_conncomp,
        "mask": mask_stats(mask),
        "boundary_jumps": _jump_summary(jumps),
        "backend": backend_stats,
        "tile_dir": tile_dir,
        "wall_time_s": time.perf_counter() - t0,
    }
    return _JobResult(job.index, job.pair, str(unw_path), str(cc_path), stats)


def _abort(ex: Executor, futures: dict[Future[_JobResult], _Job]) -> None:
    """Drop the queued jobs and kill the workers (Ctrl-C / SIGINT must not be queued behind
    the whole stack).

    ``Executor.__exit__`` only calls ``shutdown(wait=True)``, which lets every already
    submitted interferogram run to completion; a ``KeyboardInterrupt`` would then surface
    hours later. ``cancel_futures`` (3.9+) discards what has not started and, for the
    process pool, the running children are terminated as well.

    Order matters: the executor cancels the pending work items itself first — cancelling
    the futures by hand and *then* terminating makes the pool's manager thread trip over
    an already-cancelled future (``InvalidStateError`` printed from a daemon thread).
    """
    with contextlib.suppress(Exception):
        ex.shutdown(wait=False, cancel_futures=True)
    processes = getattr(ex, "_processes", None)  # ProcessPoolExecutor internals
    if isinstance(processes, dict):
        for proc in list(processes.values()):
            with contextlib.suppress(Exception):
                proc.terminate()
    for fut in futures:
        fut.cancel()


def _execute(
    jobs: list[_Job], n_parallel: int, use_threads: bool
) -> tuple[list[_JobResult], list[tuple[str, str]]]:
    results: list[_JobResult] = []
    failures: list[tuple[str, str]] = []
    if n_parallel <= 1 or len(jobs) <= 1:
        for job in jobs:
            try:
                results.append(_unwrap_one(job))
            except Exception as e:
                failures.append((job.pair, f"{type(e).__name__}: {e}"))
        return results, failures
    executor: Executor
    if use_threads:
        executor = ThreadPoolExecutor(max_workers=n_parallel)
    else:
        # spawn: safe with the RSS sampler thread and on macOS
        executor = ProcessPoolExecutor(max_workers=n_parallel, mp_context=mp.get_context("spawn"))
    with executor as ex:
        futures = {ex.submit(_unwrap_one, job): job for job in jobs}
        try:
            for fut in as_completed(futures):
                job = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as e:
                    failures.append((job.pair, f"{type(e).__name__}: {e}"))
        except BaseException:  # KeyboardInterrupt / SystemExit / cancellation
            _abort(ex, futures)
            raise
    results.sort(key=lambda r: r.index)
    return results, failures


# ---------------------------------------------------------------------------- RSS sampler


class _RssSampler:
    """Peak RSS of this process plus its children, sampled on a daemon thread (psutil)."""

    def __init__(self, interval_s: float = 0.1) -> None:
        self.interval_s = interval_s
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wintersar-rss", daemon=True)

    def _sample(self) -> int:
        import psutil  # source: .venv/lib/python3.11/site-packages/psutil/__init__.py

        proc = psutil.Process()
        total = int(proc.memory_info().rss)
        try:
            for child in proc.children(recursive=True):
                try:
                    total += int(child.memory_info().rss)
                except psutil.Error:
                    continue
        except psutil.Error:
            pass
        return total

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.peak_bytes = max(self.peak_bytes, self._sample())
            except Exception:
                continue

    def start(self) -> None:
        try:
            self.peak_bytes = self._sample()
        except Exception:
            self.peak_bytes = 0
        self._thread.start()

    def stop(self) -> float:
        self._stop.set()
        self._thread.join(timeout=2.0)
        with contextlib.suppress(Exception):
            self.peak_bytes = max(self.peak_bytes, self._sample())
        return self.peak_bytes / 1e6


# ---------------------------------------------------------------------------- main entry


def _backend_params(
    params: dict[str, Any],
    cfg: UnwrapCfg,
    plan: UnwrapPlan,
    tiles_dir: Path,
    log_dir: Path,
) -> dict[str, Any]:
    """Parameters handed to ``Unwrapper.unwrap`` (user keys + plan-derived keys).

    ``params`` must already be flat (:func:`flatten_params`): the adapters merge their own
    ``unwrap`` section with top level winning, so the resolved ``cfg`` values below have to
    be at the top level or the adapter would silently run with the ``UnwrapCfg`` defaults.

    Key names follow snaphu-py (``ntiles``, ``tile_overlap``, ``nproc``, ``cost``,
    ``init``; source: https://github.com/isce-framework/snaphu-py/blob/main/src/snaphu/_unwrap.py)
    and tophu (``ntiles``; source:
    https://github.com/isce-framework/tophu/blob/main/src/tophu/_multiscale.py). Adapters
    ignore what they do not use.
    """
    out: dict[str, Any] = {k: v for k, v in params.items() if k not in ("tiles", "mask", SECTION)}
    out.update(
        {
            "method": plan.method,
            "cost": cfg.cost,
            "init": cfg.init,
            "coherence_threshold": cfg.coherence_threshold,
            "ntiles": (plan.rows, plan.cols),
            "tile_overlap": plan.overlap_px,
            "nproc": plan.nproc_per_igram,
            "save_cost_file": cfg.save_cost_file,
            "_plan": plan.to_dict(),
            "_tile_dir": str(tiles_dir),
            "_log_dir": str(log_dir),
        }
    )
    return out


def _finding(rule_id: str, severity: Severity, scope: str | None = None, **params: Any) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=f"unwrap.{rule_id}.cause",
        fix_key=f"unwrap.{rule_id}.fix",
        params=params,
        evidence=dict(params),
        scope=scope,
    )


def _unavailable_detail(
    unwrapper: backends.Unwrapper, error: EngineNotAvailableError
) -> tuple[str, list[Finding]]:
    """``(detail, install findings)`` for ``UNW-001``.

    ``str(EngineNotAvailableError)`` embeds a Python list repr (``['ENV-001']``), which has
    no place in user-facing text: the rule ids go into ``{detail}`` and the engine's own
    ``ENV-00x`` findings are reported next to ``UNW-001`` so the reader gets the
    install hint in their language.
    """
    check = getattr(unwrapper, "check_install", None)
    install: list[Finding] = []
    if callable(check):
        with contextlib.suppress(Exception):  # probing must never mask the real error
            install = [f for f in check() if f.is_fail]
    rules = ", ".join(dict.fromkeys(f.rule_id for f in install))
    return rules or mask_text(str(error)), install


def _write_stats(path: Path, stats: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(mask_mapping(stats), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def run_unwrap(
    igrams: Artifact,
    params: dict[str, Any],
    out_dir: Path,
    log_dir: Path,
    machine: MachineSpec,
) -> Artifacts:
    """Run the unwrap stage on the ``igrams`` artifact (npz or engine dir). See module doc."""
    t_start = time.perf_counter()
    out_dir = Path(out_dir)
    log_dir = Path(log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILE

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {mask_text(msg)}\n")

    params = flatten_params(params)
    stats_path = out_dir / STATS_FILE
    try:
        stack, igram_format = load_igrams(igrams)
    except UnwrapInputError as e:
        finding = _finding("UNW-005", "FAIL", **mask_mapping(e.params))
        log(f"UNW-005 {e}")
        _write_stats(
            stats_path,
            {
                "status": "failed",
                "params": {k: v for k, v in params.items() if not k.startswith("_")},
                "igrams": mask_mapping(e.params),
                "findings": [finding.model_dump(mode="json")],
                "log": str(log_path),
            },
        )
        e.findings = [finding]
        raise
    # the workers re-open the stack per interferogram, so a directory input is converted
    # once to the ``.npz`` interchange in a scratch directory (removed after the run)
    input_dir = out_dir / INPUT_DIR
    igram_path = Path(igrams.path)
    if igram_format != NPZ_FORMAT:
        igram_path = write_worker_stack(stack, input_dir / "igrams.npz")
        log(f"converted {igram_format} input {igrams.path} -> {igram_path}")

    cfg = cfg_from_params(params)
    requested = str(params.get("method") or cfg.method)
    machine = _machine_from_params(machine, params)
    fringe = stack_fringe_density(stack, cfg)
    n_par = params.get("_n_parallel")
    plan = resolve_plan(
        stack.shape,
        stack.n_pairs,
        machine,
        cfg,
        requested_method=requested,
        fringe=fringe,
        n_parallel_override=int(n_par) if n_par is not None else None,
    )
    log(f"plan {json.dumps(plan.to_dict(), default=str)}")
    findings: list[Finding] = []
    base_stats: dict[str, Any] = {
        "plan": plan.to_dict(),
        "reasons": plan.explain(),
        "method": plan.method,
        "n_pairs": stack.n_pairs,
        "shape": list(stack.shape),
        "fringe_density": fringe,
        "machine": {"cores": machine.cores, "memory_gb": machine.memory_gb, "gpu": machine.gpu},
        "params": {k: v for k, v in params.items() if not k.startswith("_")},
        "cfg": cfg.model_dump(mode="json"),
        "igrams": {"path": str(igrams.path), "format": igram_format, "kind": igrams.kind},
        "log": str(log_path),
    }

    unwrapper = backends.get_unwrapper(plan.method)
    try:
        backends.require_available(unwrapper)
    except EngineNotAvailableError as e:
        detail, install_findings = _unavailable_detail(unwrapper, e)
        findings.append(
            _finding(
                "UNW-001",
                "FAIL",
                method=plan.method,
                detail=detail,
                install_hint=str(getattr(unwrapper, "install_hint", "") or "-"),
            )
        )
        findings.extend(install_findings)
        log(f"UNW-001 {plan.method}: {detail}")
        _write_stats(
            stats_path,
            base_stats
            | {"findings": [f.model_dump(mode="json") for f in findings], "status": "failed"},
        )
        raise
    if plan.tiled and plan.tile_mb > plan.budget_mb > 0.0:
        findings.append(
            _finding(
                "UNW-002",
                "WARN",
                tile_mb=plan.tile_mb,
                budget_mb=plan.budget_mb,
                rows=plan.rows,
                cols=plan.cols,
            )
        )

    native_tiles = backends.supports_native_tiles(unwrapper)
    use_threads = backends.is_test_backend(plan.method)
    parts_dir = out_dir / PARTS_DIR
    tiles_dir = out_dir / TILES_DIR
    backend_params = _backend_params(params, cfg, plan, tiles_dir, log_dir)
    offsets_raw = params.get("_tile_offset_cycles") or {}
    offsets = {str(k): float(v) for k, v in dict(offsets_raw).items()}
    jobs = [
        _Job(
            index=i,
            pair=pair,
            igram_path=str(igram_path),
            method=plan.method,
            plan=plan.to_dict(),
            coherence_threshold=cfg.coherence_threshold,
            use_geo_mask=bool(cfg.mask.water or cfg.mask.layover),
            use_coherence=bool(cfg.mask.coherence),
            backend_params=backend_params,
            parts_dir=str(parts_dir),
            native_tiles=native_tiles,
            want_truth=plan.method == "truth",
            tile_offset_cycles=offsets,
        )
        for i, pair in enumerate(stack.pairs)
    ]
    log(
        t(
            "unwrap.run.start",
            n_pairs=stack.n_pairs,
            method=plan.method,
            rows=plan.rows,
            cols=plan.cols,
            n_parallel=plan.n_parallel,
        )
    )
    sampler = _RssSampler()
    sampler.start()
    results, failures = _execute(jobs, plan.n_parallel, use_threads)
    peak_rss_mb = sampler.stop()

    for pair, error in failures:
        findings.append(
            _finding("UNW-004", "FAIL", scope=pair, pair=pair, error=error, log=str(log_path))
        )
        log(f"UNW-004 {pair}: {error}")

    # -- assemble -------------------------------------------------------------------
    n, (ny, nx) = stack.n_pairs, stack.shape
    unw = np.full((n, ny, nx), np.nan, dtype=np.float32)
    conncomp = np.zeros((n, ny, nx), dtype=np.uint16)
    per_pair: list[dict[str, Any]] = []
    tile_dirs: dict[str, str] = {}
    for r in results:
        unw[r.index] = np.load(r.unw_path)
        cc = np.load(r.conncomp_path)
        if cc.max(initial=0) > np.iinfo(conncomp.dtype).max:
            conncomp = conncomp.astype(np.uint32)
        conncomp[r.index] = cc
        per_pair.append(r.stats)
        if r.stats.get("tile_dir"):
            tile_dirs[r.pair] = str(r.stats["tile_dir"])
    shutil.rmtree(parts_dir, ignore_errors=True)
    shutil.rmtree(input_dir, ignore_errors=True)  # converted engine input (scratch only)
    unw_path = out_dir / UNW_FILE
    np.savez_compressed(
        unw_path,
        unw=unw,
        conncomp=conncomp,
        pairs=np.array(stack.pairs),
        dates=np.array([d.isoformat() for d in stack.dates]),
    )

    jump_total = {"n_boundaries": 0, "n_boundaries_with_jump": 0, "n_jump_pixels": 0}
    for s in per_pair:
        bj = s.get("boundary_jumps")
        if bj:
            for k in jump_total:
                jump_total[k] += int(bj.get(k, 0))
    if jump_total["n_boundaries_with_jump"] > 0:
        findings.append(
            _finding(
                "UNW-003",
                "WARN",
                n_jump=jump_total["n_boundaries_with_jump"],
                n_boundaries=jump_total["n_boundaries"],
                n_jump_pixels=jump_total["n_jump_pixels"],
            )
        )
    masked = float(np.isnan(unw).mean()) if unw.size else 0.0
    wall = time.perf_counter() - t_start
    stats = base_stats | {
        "status": "failed" if failures else "ok",
        "wall_time_s": wall,
        "peak_rss_mb": peak_rss_mb,
        "n_parallel": plan.n_parallel,
        "executor": (
            "inline"
            if plan.n_parallel <= 1 or len(jobs) <= 1
            else ("thread" if use_threads else "process")
        ),
        "native_tiles": native_tiles,
        "masked_fraction": masked,
        "conncomp_counts": {s["pair"]: s["n_conncomp"] for s in per_pair},
        "boundary_jumps": jump_total,
        "tile_dirs": tile_dirs,
        "per_pair": per_pair,
        "failed": [p for p, _ in failures],
        "findings": [f.model_dump(mode="json") for f in findings],
        "outputs": {"unw": str(unw_path), "stats": str(stats_path)},
    }
    _write_stats(stats_path, stats)
    log(t("unwrap.run.done", n_pairs=stack.n_pairs, wall_time_s=wall, peak_rss_mb=peak_rss_mb))
    if failures:
        raise UnwrapFailedError([p for p, _ in failures], findings)
    meta = {
        "n_pairs": n,
        "shape": [ny, nx],
        "method": plan.method,
        "masked_fraction": masked,
        "coherence_threshold": cfg.coherence_threshold,
        "tiles": [plan.rows, plan.cols],
        "tile_dirs": tile_dirs,
        "findings": [f.rule_id for f in findings],
    }
    return (
        Artifacts()
        .add(Artifact(name="unw", path=unw_path, kind="npz", meta=meta))
        .add(
            Artifact(
                name="unwrap_stats",
                path=stats_path,
                kind="json",
                meta={"wall_time_s": wall, "peak_rss_mb": peak_rss_mb},
            )
        )
    )
