"""``wintersar bench`` runner: repeats a site, aggregates per-stage medians, computes quality
metrics and writes ``bench_result.json`` (plan §5.9 / §6.3, ADR-0054).

Two runner functions share the :data:`RunnerFn` signature ``(site, workdir, repeat) ->``
:class:`RunOutcome`:

* :func:`pipeline_runner` — the real path: ``wintersar.pipeline.api.run`` (imported lazily)
  on a fresh work directory per repeat so the DAG cache cannot short-circuit a repeat; the
  per-stage numbers come from the executor's ``StageRecord.resources`` and the whole run is
  wrapped in one :class:`~wintersar.bench.profiler.StageProfiler` for disk/network totals.
* :func:`fake_engine_runner` — calls :class:`~wintersar.engines.fake.FakeEngine` directly,
  stage by stage, each under its own profiler (what the unit tests inject; also
  ``wintersar bench --runner fake`` for a quick CI smoke run).

Metrics (plan §6.3 item 3): ``closure_rms`` (wrapped triplet closure, radians),
``unwrap_error_fraction`` (fraction of pixels ≥ π from the synthetic truth after removing the
constant offset, synthetic sites only), ``gt_rmse`` (LOS RMSE against the ground-truth CSV via
:mod:`wintersar.validate`, when a CSV is configured and the module is importable).
"""

from __future__ import annotations

import json
import platform
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar import __version__
from wintersar.bench.profiler import Measurement, StageProfiler
from wintersar.bench.report import CompareReport, compare, load_result
from wintersar.bench.sites import ENGINE_STAGES, Site, build_config
from wintersar.io.schemas import Artifacts, Finding, Severity
from wintersar.util import sysinfo
from wintersar.util.masking import mask_mapping
from wintersar.util.output import to_jsonable

SCHEMA_VERSION = 1
ARTIFACT_STAGES: dict[str, str] = {
    "igrams": "interferogram",
    "unw": "unwrap",
    "timeseries": "timeseries",
    "velocity": "geocode",
}


@dataclass
class RunOutcome:
    stages: dict[str, Measurement]
    artifacts: dict[str, Path] = field(default_factory=dict)
    total: Measurement | None = None
    ok: bool = True
    error: str | None = None
    failed_stage: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


RunnerFn = Callable[[Site, Path, int], RunOutcome]


def _finding(rule_id: str, severity: Severity, **params: Any) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=f"bench.{rule_id}.cause",
        fix_key=f"bench.{rule_id}.fix",
        params=params,
        evidence=dict(params),
    )


# ============================================================================ runners
def fake_engine_runner(site: Site, workdir: Path, repeat: int) -> RunOutcome:
    """Fake engine, stage by stage, each stage under its own :class:`StageProfiler`."""
    from wintersar.engines.fake import FakeEngine

    eng = FakeEngine()
    run_dir = Path(workdir) / f"run{repeat}"
    run_dir.mkdir(parents=True, exist_ok=True)
    inputs = Artifacts()
    stages: dict[str, Measurement] = {}
    artifacts: dict[str, Path] = {}
    unwrap_cfg = site.config.get("unwrap", {}) if isinstance(site.config, dict) else {}
    with StageProfiler(watch_dir=run_dir) as total:
        for stage in site.stages:
            if stage not in ENGINE_STAGES:
                continue
            params: dict[str, Any] = dict(site.param_overrides.get(stage, {}))
            if stage == "interferogram":
                params.setdefault("seed", repeat)
            if stage == "unwrap" and "coherence_threshold" in unwrap_cfg:
                params.setdefault("coherence_threshold", unwrap_cfg["coherence_threshold"])
            out_dir = run_dir / stage / "out"
            log_dir = run_dir / stage / "logs"
            params["_out_dir"] = str(out_dir)
            try:
                with StageProfiler(watch_dir=run_dir) as prof:
                    produced = eng.run(stage, inputs, params, log_dir)
            except Exception as e:
                return RunOutcome(
                    stages=stages,
                    artifacts=artifacts,
                    ok=False,
                    error=f"{type(e).__name__}: {e}",
                    failed_stage=stage,
                )
            stages[stage] = prof.result
            inputs = inputs.merged(produced)
            for name, art in produced.items.items():
                artifacts[name] = art.path
    return RunOutcome(stages=stages, artifacts=artifacts, total=total.result)


def pipeline_runner(site: Site, workdir: Path, repeat: int) -> RunOutcome:
    """``wintersar.pipeline.api.run`` on a fresh work directory (no cache reuse between repeats)."""
    from wintersar.pipeline import api

    run_dir = Path(workdir) / f"run{repeat}"
    cfg = build_config(site, run_dir)
    overrides = {k: dict(v) for k, v in site.param_overrides.items()}
    if "interferogram" in overrides:
        overrides["interferogram"].setdefault("seed", repeat)
    until = site.stages[-1] if site.stages else None
    try:
        with StageProfiler(watch_dir=cfg.workdir) as total:
            result = api.run(cfg, param_overrides=overrides, until=until)
    except Exception as e:
        return RunOutcome(stages={}, ok=False, error=f"{type(e).__name__}: {e}")
    stages: dict[str, Measurement] = {}
    for rec in result.records:
        if rec.stage not in site.stages or rec.status != "ok":
            continue
        r = rec.resources
        stages[rec.stage] = Measurement(
            wall_time_s=float(r.wall_time_s or 0.0),
            cpu_time_s=0.0,
            peak_rss_gb=float(r.peak_rss_gb or 0.0),
            baseline_rss_gb=0.0,
            network_bytes=int((r.network_gb or 0.0) * 1e9),
            disk_peak_gb=r.disk_gb,
            n_samples=1,
        )
    artifacts = {n: a.path for n, a in result.artifacts.items.items()}
    return RunOutcome(
        stages=stages,
        artifacts=artifacts,
        total=total.result,
        ok=result.ok,
        error=result.error,
        failed_stage=result.failed_stage,
        extra={"run_id": result.run_id},
    )


RUNNERS: dict[str, RunnerFn] = {"pipeline": pipeline_runner, "fake": fake_engine_runner}


# ============================================================================ metrics
def wrap(phase: NDArray[Any]) -> NDArray[np.float64]:
    return np.asarray(np.angle(np.exp(1j * np.asarray(phase, dtype=np.float64))))


def triplets(pairs: Sequence[str]) -> list[tuple[int, int, int]]:
    """Indices ``(ij, jk, ik)`` of every closed triangle ``i<j<k`` in ``pairs``."""
    idx = {p: n for n, p in enumerate(pairs)}
    dates = sorted({d for p in pairs for d in p.split("_")})
    out: list[tuple[int, int, int]] = []
    for a in range(len(dates)):
        for b in range(a + 1, len(dates)):
            ab = f"{dates[a]}_{dates[b]}"
            if ab not in idx:
                continue
            for c in range(b + 1, len(dates)):
                bc, ac = f"{dates[b]}_{dates[c]}", f"{dates[a]}_{dates[c]}"
                if bc in idx and ac in idx:
                    out.append((idx[ab], idx[bc], idx[ac]))
    return out


def closure_rms(
    wrapped: NDArray[Any], pairs: Sequence[str], mask: NDArray[Any] | None = None
) -> float | None:
    """RMS (rad) of the wrapped closure ``phi_ij + phi_jk - phi_ik`` over all triplets and unmasked
    pixels; ``None`` when the network has no triangle."""
    tri = triplets(pairs)
    if not tri:
        return None
    ph = np.asarray(wrapped, dtype=np.float64)
    valid = np.isfinite(ph)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        valid &= ~(m[None] if m.ndim == 2 else m)
    sq: list[float] = []
    n = 0
    for ij, jk, ik in tri:
        v = valid[ij] & valid[jk] & valid[ik]
        if not v.any():
            continue
        c = wrap(ph[ij] + ph[jk] - ph[ik])[v]
        sq.append(float(np.sum(c * c)))
        n += int(v.sum())
    if n == 0:
        return None
    return float(np.sqrt(sum(sq) / n))


def unwrap_error_fraction(
    unw: NDArray[Any], unw_true: NDArray[Any], mask: NDArray[Any] | None = None
) -> float | None:
    """Mean over pairs of the fraction of pixels whose error is ≥ π after removing the
    per-pair median offset (same definition as ``wintersar.research.synth``)."""
    u = np.asarray(unw, dtype=np.float64)
    t = np.asarray(unw_true, dtype=np.float64)
    fr: list[float] = []
    for i in range(u.shape[0]):
        valid = np.isfinite(u[i])
        if mask is not None:
            m = np.asarray(mask, dtype=bool)
            valid &= ~(m if m.ndim == 2 else m[i])
        if not valid.any():
            continue
        diff = u[i] - t[i]
        diff = diff - np.nanmedian(diff[valid])
        fr.append(float(np.mean(np.abs(diff[valid]) >= np.pi)))
    return float(np.mean(fr)) if fr else None


def _npz(path: Path | None) -> dict[str, NDArray[Any]] | None:
    if path is None or not Path(path).exists() or Path(path).suffix != ".npz":
        return None
    with np.load(path, allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def compute_metrics(artifacts: dict[str, Path], site: Site) -> dict[str, float | None]:
    """``{metric: value | None}`` for the metrics listed in ``site.metrics``."""
    out: dict[str, float | None] = {}
    ig = _npz(artifacts.get("igrams"))
    if "closure_rms" in site.metrics:
        out["closure_rms"] = (
            closure_rms(ig["wrapped"], [str(p) for p in ig["pairs"]], ig.get("mask"))
            if ig is not None and "wrapped" in ig and "pairs" in ig
            else None
        )
    if "unwrap_error_fraction" in site.metrics:
        un = _npz(artifacts.get("unw"))
        out["unwrap_error_fraction"] = (
            unwrap_error_fraction(un["unw"], ig["unw_true"], ig.get("mask"))
            if un is not None and ig is not None and "unw_true" in ig and "unw" in un
            else None
        )
    if "gt_rmse" in site.metrics:
        out["gt_rmse"] = _gt_rmse(artifacts.get("timeseries"), site)
    return out


def _gt_rmse(ts_path: Path | None, site: Site) -> float | None:
    gt = site.ground_truth_path
    if gt is None or not gt.exists() or ts_path is None or not Path(ts_path).exists():
        return None
    try:
        from wintersar.io.formats import load_timeseries
        from wintersar.validate.ground_truth import load_csv
        from wintersar.validate.metrics import compare as compare_gt
    except ImportError:
        return None
    try:
        ts = load_timeseries(ts_path)
        res = compare_gt(ts, load_csv(gt))
    except Exception:
        return None
    return None if not np.isfinite(res.rmse_m) else float(res.rmse_m)


# ============================================================================ aggregation
def _median(vals: Sequence[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None and np.isfinite(v)]
    return float(np.median(xs)) if xs else None


def aggregate_stages(outcomes: Sequence[RunOutcome]) -> dict[str, dict[str, Any]]:
    """Per-stage medians over repeats (``*_runs`` keeps the raw values)."""
    names: list[str] = []
    for o in outcomes:
        for s in o.stages:
            if s not in names:
                names.append(s)
    agg: dict[str, dict[str, Any]] = {}
    for s in names:
        ms = [o.stages[s] for o in outcomes if s in o.stages]
        agg[s] = {
            "wall_time_s": _median([m.wall_time_s for m in ms]),
            "cpu_time_s": _median([m.cpu_time_s for m in ms]),
            "peak_rss_gb": _median([m.peak_rss_gb for m in ms]),
            "disk_peak_gb": _median([m.disk_peak_gb for m in ms]),
            "network_bytes": _median([float(m.network_bytes) for m in ms]),
            "n_runs": len(ms),
            "wall_time_s_runs": [m.wall_time_s for m in ms],
            "peak_rss_gb_runs": [m.peak_rss_gb for m in ms],
        }
    return agg


def aggregate_total(outcomes: Sequence[RunOutcome]) -> dict[str, Any]:
    ts = [o.total for o in outcomes if o.total is not None]
    if not ts:
        walls = [sum(m.wall_time_s for m in o.stages.values()) for o in outcomes]
        return {"wall_time_s": _median(walls), "n_runs": len(walls)}
    return {
        "wall_time_s": _median([m.wall_time_s for m in ts]),
        "cpu_time_s": _median([m.cpu_time_s for m in ts]),
        "peak_rss_gb": _median([m.peak_rss_gb for m in ts]),
        "disk_peak_gb": _median([m.disk_peak_gb for m in ts]),
        "network_bytes": _median([float(m.network_bytes) for m in ts]),
        "n_runs": len(ts),
        "wall_time_s_runs": [m.wall_time_s for m in ts],
    }


def aggregate_metrics(per_run: Sequence[dict[str, float | None]]) -> dict[str, float | None]:
    keys: list[str] = []
    for d in per_run:
        for k in d:
            if k not in keys:
                keys.append(k)
    return {k: _median([d.get(k) for d in per_run]) for k in keys}


# ============================================================================ machine / git
def git_sha(repo: Path | None = None) -> str | None:
    root = repo or Path(__file__).resolve().parents[3]
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def machine_info() -> dict[str, Any]:
    spec = sysinfo.detect()
    return {
        "cores": spec.cores,
        "memory_gb": round(spec.memory_gb, 2),
        "gpu": spec.gpu,
        "gpu_name": spec.gpu_name,
        "python": spec.python,
        "os": spec.os,
        "machine": platform.machine(),
        # Deliberately NOT platform.node(): bench_result.json is meant to be shared
        # (plan 6.3) and the hostname identifies the operator, not the hardware.
    }


# ============================================================================ run_site
@dataclass
class BenchResult:
    data: dict[str, Any]
    findings: list[Finding]
    path: Path | None
    comparison: CompareReport | None = None

    @property
    def ok(self) -> bool:
        return not any(f.is_fail for f in self.findings)

    @property
    def stages(self) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("stages", {}))

    @property
    def metrics(self) -> dict[str, float | None]:
        return dict(self.data.get("metrics", {}))


def run_site(
    site: Site,
    out_json: Path | str | None,
    compare_with: Path | str | dict[str, Any] | None = None,
    runner: RunnerFn | str | None = None,
    *,
    repeats: int | None = None,
    workdir: Path | str | None = None,
    threshold: float | None = None,
    fail_on_regression: bool = False,
    allow_network: bool = False,
    progress: Callable[[int, int, RunOutcome], None] | None = None,
) -> BenchResult:
    """Run ``site`` ``repeats`` times, aggregate, compute metrics, write ``bench_result.json``.

    ``runner`` is a :data:`RunnerFn`, a name in :data:`RUNNERS` or ``None`` (``site.runner``).
    ``compare_with`` is a baseline ``bench_result.json`` (path or dict): the report is embedded
    under ``"compare"`` and regressions become ``BENCH-001`` (FAIL with ``fail_on_regression``,
    else WARN). Never raises for site/run problems: check ``BenchResult.findings``.
    """
    findings: list[Finding] = []
    n_rep = int(repeats or site.repeats)
    thr = site.regression_threshold if threshold is None else float(threshold)
    if site.is_template:
        findings.append(
            _finding("BENCH-004", "FAIL", site=site.name, placeholders=", ".join(site.placeholders))
        )
    if site.network and not allow_network:
        findings.append(_finding("BENCH-002", "FAIL", site=site.name))
    if isinstance(runner, str):
        runner_name = runner
        fn = RUNNERS[runner]
    elif runner is None:
        runner_name = site.runner
        fn = RUNNERS[site.runner]
    else:
        runner_name = getattr(runner, "__name__", "custom")
        fn = runner
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "site": site.summary(),
        "git_sha": git_sha(),
        "wintersar_version": __version__,
        "machine": machine_info(),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repeats": n_rep,
        "runner": runner_name,
        "regression_threshold": thr,
    }
    if any(f.is_fail for f in findings):
        early: dict[str, Any] = {**base, "stages": {}, "total": {}, "metrics": {}, "runs": []}
        return BenchResult(_finish(early, findings, out_json), findings, _path(out_json))

    tmp: tempfile.TemporaryDirectory[str] | None = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="wintersar-bench-")
        wd = Path(tmp.name)
    else:
        wd = Path(workdir)
        wd.mkdir(parents=True, exist_ok=True)
    outcomes: list[RunOutcome] = []
    per_run_metrics: list[dict[str, float | None]] = []
    try:
        for i in range(n_rep):
            outcome = fn(site, wd, i)
            outcomes.append(outcome)
            if progress is not None:
                progress(i + 1, n_rep, outcome)
            if not outcome.ok:
                findings.append(
                    _finding(
                        "BENCH-005",
                        "FAIL",
                        site=site.name,
                        repeat=i + 1,
                        stage=outcome.failed_stage or "?",
                        error=outcome.error or "",
                    )
                )
                break
            per_run_metrics.append(compute_metrics(outcome.artifacts, site))
    finally:
        if tmp is not None:
            tmp.cleanup()
    stages = aggregate_stages(outcomes)
    total = aggregate_total(outcomes)
    metrics = aggregate_metrics(per_run_metrics)
    for m in site.metrics:
        if metrics.get(m) is None:
            findings.append(_finding("BENCH-006", "INFO", metric=m, site=site.name))
    budget = site.max_wall_time_s
    total_wall = total.get("wall_time_s")
    if budget is not None and total_wall is not None and float(total_wall) > budget:
        findings.append(
            _finding(
                "BENCH-007",
                "WARN",
                site=site.name,
                wall_time_s=float(total_wall),
                budget_s=budget,
            )
        )
    data: dict[str, Any] = {
        **base,
        "stages": stages,
        "total": total,
        "metrics": metrics,
        "runs": [
            {
                "ok": o.ok,
                "error": o.error,
                "failed_stage": o.failed_stage,
                "wall_time_s": None if o.total is None else o.total.wall_time_s,
                **{k: v for k, v in o.extra.items() if isinstance(v, str | int | float | bool)},
            }
            for o in outcomes
        ],
    }
    report: CompareReport | None = None
    if compare_with is not None:
        baseline: dict[str, Any] | None
        if isinstance(compare_with, dict):
            baseline = compare_with
        else:
            try:
                baseline = load_result(compare_with)
            except (OSError, ValueError) as e:
                baseline = None
                findings.append(_finding("BENCH-003", "WARN", path=str(compare_with), error=str(e)))
        if baseline is not None:
            report = compare(baseline, data, threshold=thr)
            data["compare"] = report.to_dict()
            for r in report.regressions:
                findings.append(
                    _finding(
                        "BENCH-001",
                        "FAIL" if fail_on_regression else "WARN",
                        stage=r.name,
                        before=r.before,
                        after=r.after,
                        pct=r.delta_pct or 0.0,
                        threshold=thr * 100.0,
                    )
                )
    return BenchResult(_finish(data, findings, out_json), findings, _path(out_json), report)


def _path(p: Path | str | None) -> Path | None:
    return None if p is None else Path(p)


def _finish(
    data: dict[str, Any], findings: list[Finding], out_json: Path | str | None
) -> dict[str, Any]:
    data["findings"] = [f.model_dump(mode="json") for f in findings]
    masked = dict(mask_mapping(to_jsonable(data)))
    if out_json is not None:
        p = Path(out_json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(masked, ensure_ascii=False, indent=2), encoding="utf-8")
    return masked
