"""Parameter sweep (plan §5.6, R-08/R-11, ADR-0044).

A sweep YAML lists dotted ``config.yaml`` keys with candidate values::

    grid:
      unwrap.coherence_threshold: [0.3, 0.5]
      engine.looks: [[4, 1], [8, 2]]
      engine.filter.alpha: [0.4, 0.6]
      unwrap.method: [snaphu, tophu]
      timeseries.troposphere: [era5, none]
    objectives: [gt_rmse, wall_time_s]     # optional (default: see DEFAULT_OBJECTIVES)
    max_points: 64                         # optional safety cap

:func:`make_grid` returns the cartesian product as nested per-section dicts
(``{"unwrap": {"coherence_threshold": 0.3}, "engine": {"filter": {"alpha": 0.4}}}``).
:func:`run_sweep` applies each point to a copy of the :class:`Config`, runs the pipeline
(default runner ``wintersar.pipeline.api.run``; the DAG cache re-runs only the changed stages
and their downstream, PERF-03) and collects the metrics of ADR-0044:

``closure_rms`` (rad)         RMS of the per-pixel loop-closure RMS of the unwrapped stack
``temporal_coherence``        mean of |Σ exp(j·closure)| / N (closure coherence proxy)
``residual_rms`` (m)          RMS of unw-pair displacement - time-series pair difference
``gt_rmse`` (m)               :func:`wintersar.validate.metrics.compare` overall RMSE
``wall_time_s``               Σ stage wall time recorded in the run manifests
``cache_hits``                number of stages served from the cache

:func:`pareto_front` returns the non-dominated rows for the chosen objectives.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import yaml
from numpy.typing import NDArray

from wintersar.i18n import t
from wintersar.io.igrams import IgramStack, load_igram_stack
from wintersar.io.schemas import Finding, GroundTruthRecord
from wintersar.io.timeseries import TimeSeries
from wintersar.research.synth import PHASE_PER_M_LOS
from wintersar.util.masking import mask_mapping, mask_text
from wintersar.validate.closure import closure_coherence, closure_phase
from wintersar.validate.ground_truth import make_finding
from wintersar.validate.metrics import DEFAULT_RADIUS_M, compare

if TYPE_CHECKING:
    from wintersar.pipeline.config import Config

FloatArray = NDArray[np.float64]
Point = dict[str, Any]  # nested per config section
Runner = Callable[["Config", dict[str, dict[str, Any]] | None], Any]

METRIC_KEYS: tuple[str, ...] = (
    "closure_rms",
    "temporal_coherence",
    "residual_rms",
    "gt_rmse",
    "wall_time_s",
    "cache_hits",
)
# Objective sense: "min" unless listed here.
MAXIMISE: frozenset[str] = frozenset({"temporal_coherence", "cache_hits"})
DEFAULT_OBJECTIVES: tuple[str, ...] = ("gt_rmse", "closure_rms", "wall_time_s")
DEFAULT_MAX_POINTS: int = 256
# LOS displacement per radian of unwrapped phase (positive = towards satellite), the inverse
# of research.synth.PHASE_PER_M_LOS (= -4π/λ, Sentinel-1 C-band λ = 0.05546576 m).
LOS_M_PER_RAD: float = 1.0 / PHASE_PER_M_LOS


@dataclass
class SweepSpec:
    grid: dict[str, list[Any]]
    objectives: list[str] = field(default_factory=lambda: list(DEFAULT_OBJECTIVES))
    max_points: int = DEFAULT_MAX_POINTS


@dataclass
class SweepRow:
    index: int
    params: dict[str, Any]  # flattened dotted keys
    metrics: dict[str, float | None] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None
    run_id: str | None = None
    findings: list[Finding] = field(default_factory=list)

    def metric(self, name: str) -> float | None:
        v = self.metrics.get(name)
        return None if v is None or not np.isfinite(v) else float(v)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "params": dict(self.params),
            "metrics": {k: self.metric(k) for k in METRIC_KEYS if k in self.metrics},
            "ok": self.ok,
            "error": self.error,
            "run_id": self.run_id,
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }


# ------------------------------------------------------------------ grid handling


def flatten(nested: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """``{"engine": {"filter": {"alpha": 0.4}}} -> {"engine.filter.alpha": 0.4}``."""
    out: dict[str, Any] = {}
    for k, v in nested.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def expand(dotted: Mapping[str, Any]) -> Point:
    """Inverse of :func:`flatten`."""
    out: Point = {}
    for key, value in dotted.items():
        parts = key.split(".")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    return out


def deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for k, v in extra.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_sweep_yaml(path: Path | str) -> SweepSpec:
    p = Path(path)
    if not p.exists():
        raise ValueError(
            t("validate.VAL-015.cause", path=mask_text(str(p)), reason="file not found")
        )
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            t(
                "validate.VAL-015.cause",
                path=mask_text(str(p)),
                reason="top level must be a mapping",
            )
        )
    grid_raw = raw.get("grid", raw if "objectives" not in raw and "max_points" not in raw else {})
    if not isinstance(grid_raw, dict) or not grid_raw:
        raise ValueError(
            t("validate.VAL-015.cause", path=mask_text(str(p)), reason="no 'grid' mapping")
        )
    grid: dict[str, list[Any]] = {}
    for key, values in grid_raw.items():
        if not isinstance(key, str) or "." not in key:
            raise ValueError(
                t(
                    "validate.VAL-015.cause",
                    path=mask_text(str(p)),
                    reason=f"key {key!r} is not dotted",
                )
            )
        vals = list(values) if isinstance(values, list | tuple) else [values]
        if not vals:
            raise ValueError(
                t(
                    "validate.VAL-015.cause",
                    path=mask_text(str(p)),
                    reason=f"key {key!r} has no values",
                )
            )
        grid[key] = vals
    objectives = raw.get("objectives")
    spec = SweepSpec(grid=grid)
    if isinstance(objectives, list) and objectives:
        spec.objectives = [str(o) for o in objectives]
    if "max_points" in raw:
        spec.max_points = int(raw["max_points"])
    return spec


def grid_points(spec: SweepSpec) -> list[Point]:
    """Cartesian product of the grid values as nested per-section dicts (first key slowest)."""
    keys = list(spec.grid)
    combos = list(itertools.product(*(spec.grid[k] for k in keys)))
    if len(combos) > spec.max_points:
        msg = f"sweep grid has {len(combos)} points > max_points {spec.max_points}"
        raise ValueError(msg)
    return [expand(dict(zip(keys, combo, strict=True))) for combo in combos]


def make_grid(sweep_yaml: Path | str) -> list[Point]:
    """Load the sweep YAML and return the list of parameter points."""
    return grid_points(load_sweep_yaml(sweep_yaml))


def apply_point(cfg: Config, point: Mapping[str, Any]) -> Config:
    """A validated copy of ``cfg`` with ``point`` deep-merged into its sections.

    Works on the JSON dump so pydantic re-validates every value (e.g. ``engine.looks``
    lists become tuples, unknown keys are rejected with ``extra='forbid'``).
    # source: src/wintersar/pipeline/config.py::Config (extra="forbid", alias 'validate')
    """
    from wintersar.pipeline.config import Config as _Config

    data = cfg.model_dump(mode="json", by_alias=True, exclude={"config_path"})
    merged = deep_merge(data, point)
    new = _Config.model_validate(merged)
    new.config_path = cfg.config_path
    return new


# ------------------------------------------------------------------ metrics


def network_residual_rms(
    unw: NDArray[Any],
    pairs: Sequence[str],
    dates: Sequence[date],
    ts: TimeSeries,
) -> float:
    """RMS (m) of ``unw_pair * LOS_M_PER_RAD - (disp[j] - disp[i])`` over valid pixels.

    Each interferogram is first re-referenced to its own spatial median so that a constant
    reference offset between the stack and the time series does not count as residual.
    """
    idx = {d.isoformat(): i for i, d in enumerate(dates)}
    disp = np.asarray(ts.displacement_m, dtype=np.float64)
    res: list[FloatArray] = []
    for k, key in enumerate(pairs):
        a, b = key.split("_")
        ia = idx.get(f"{a[:4]}-{a[4:6]}-{a[6:]}")
        ib = idx.get(f"{b[:4]}-{b[4:6]}-{b[6:]}")
        if ia is None or ib is None:
            continue
        model = disp[ib] - disp[ia]
        obs = np.asarray(unw[k], dtype=np.float64) * LOS_M_PER_RAD
        r = obs - model
        ok = np.isfinite(r)
        if not ok.any():
            continue
        r = r - np.nanmedian(r[ok])
        res.append(r[ok])
    if not res:
        return float("nan")
    allr = np.concatenate(res)
    return float(np.sqrt(np.mean(allr * allr)))


def stack_from_artifacts(igrams_path: Path, unw_path: Path | None) -> IgramStack:
    stack = load_igram_stack(Path(igrams_path))
    if unw_path is not None:
        with np.load(Path(unw_path), allow_pickle=False) as z:
            if "unw" in z.files:
                stack.unw = np.asarray(z["unw"], dtype=np.float32)
            if "conncomp" in z.files:
                stack.conncomp = z["conncomp"]
    return stack


def closure_metrics(stack: IgramStack) -> tuple[float, float]:
    """``(closure_rms, temporal_coherence)`` for a stack (unwrapped closure when available)."""
    result = closure_phase(stack, use_unw=stack.unw is not None)
    ppr = result.per_pixel_rms[np.isfinite(result.per_pixel_rms)]
    closure_rms = float(np.sqrt(np.mean(ppr * ppr))) if ppr.size else float("nan")
    tc = closure_coherence(stack)
    tcv = tc[np.isfinite(tc)]
    return closure_rms, float(np.mean(tcv)) if tcv.size else float("nan")


def metrics_from_run(
    result: Any,
    gt: Sequence[GroundTruthRecord] | None = None,
    *,
    radius_m: float = DEFAULT_RADIUS_M,
    geometry: Mapping[str, Any] | None = None,
) -> dict[str, float | None]:
    """Metrics of ADR-0044 from a :class:`wintersar.pipeline.api.RunResult`-like object.

    Reads ``records`` (wall time, cache hits) and the ``igrams`` / ``unw`` / ``timeseries``
    artifacts. Missing pieces give ``None``.
    """
    metrics: dict[str, float | None] = dict.fromkeys(METRIC_KEYS)
    records = list(getattr(result, "records", []) or [])
    if records:
        metrics["wall_time_s"] = float(
            sum(float(r.resources.wall_time_s or 0.0) for r in records if r.resources is not None)
        )
        metrics["cache_hits"] = float(sum(1 for r in records if r.extra.get("cache_hit")))
    arts = getattr(result, "artifacts", None)
    if arts is None:
        return metrics
    stack: IgramStack | None = None
    if "igrams" in arts:
        stack = stack_from_artifacts(
            arts["igrams"].path, arts["unw"].path if "unw" in arts else None
        )
        metrics["closure_rms"], metrics["temporal_coherence"] = closure_metrics(stack)
    if "timeseries" in arts:
        from wintersar.validate.api import load_timeseries

        ts = load_timeseries(arts["timeseries"].path, **dict(geometry or {}))
        if stack is not None and stack.unw is not None:
            metrics["residual_rms"] = network_residual_rms(stack.unw, stack.pairs, stack.dates, ts)
        if gt:
            metrics["gt_rmse"] = compare(ts, gt, radius_m=radius_m).rmse_m
    return metrics


# ------------------------------------------------------------------ sweep


def _default_runner() -> Runner:
    from wintersar.pipeline.api import run

    def runner(cfg: Config, overrides: dict[str, dict[str, Any]] | None) -> Any:
        return run(cfg, param_overrides=overrides)

    return runner


def run_sweep(
    cfg: Config,
    grid: Sequence[Mapping[str, Any]],
    gt: Sequence[GroundTruthRecord] | None = None,
    runner: Runner | None = None,
    *,
    param_overrides: dict[str, dict[str, Any]] | None = None,
    radius_m: float = DEFAULT_RADIUS_M,
    geometry: Mapping[str, Any] | None = None,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[SweepRow]:
    """Run every grid point and collect a :class:`SweepRow` per point.

    ``runner(cfg_point, param_overrides)`` must return an object with ``ok`` and either a
    ``metrics`` mapping (tests / external runners) or ``records`` + ``artifacts`` (the
    pipeline :class:`RunResult`). A failing point yields ``ok=False`` with a ``VAL-016``
    finding; the sweep continues.
    """
    run = runner or _default_runner()
    rows: list[SweepRow] = []
    n = len(grid)
    for i, point in enumerate(grid):
        flat = flatten(point)
        if progress is not None:
            progress(i + 1, n, flat)
        row = SweepRow(index=i, params=flat)
        try:
            cfg_i = apply_point(cfg, point)
            result = run(cfg_i, param_overrides)
            row.run_id = getattr(result, "run_id", None)
            row.ok = bool(getattr(result, "ok", True))
            if not row.ok:
                row.error = mask_text(str(getattr(result, "error", None) or "run failed"))
            explicit = getattr(result, "metrics", None)
            if isinstance(explicit, Mapping):
                row.metrics = {
                    k: (None if explicit.get(k) is None else float(explicit[k]))
                    for k in METRIC_KEYS
                }
            else:
                row.metrics = metrics_from_run(result, gt, radius_m=radius_m, geometry=geometry)
            for f in getattr(result, "findings", []) or []:
                if isinstance(f, Finding) and f.is_fail:
                    row.findings.append(f)
        except Exception as exc:
            row.ok = False
            row.error = mask_text(f"{type(exc).__name__}: {exc}")
            row.metrics = dict.fromkeys(METRIC_KEYS)
        if not row.ok:
            row.findings.append(
                make_finding(
                    "VAL-016",
                    "WARN",
                    index=i,
                    params=json.dumps(flat, ensure_ascii=False),
                    error=row.error,
                )
            )
        rows.append(row)
    return rows


def _sense(objectives: Mapping[str, str] | Iterable[str]) -> dict[str, str]:
    if isinstance(objectives, Mapping):
        return {
            k: ("max" if str(v).lower().startswith("max") else "min") for k, v in objectives.items()
        }
    return {o: ("max" if o in MAXIMISE else "min") for o in objectives}


def _dominates(a: SweepRow, b: SweepRow, sense: Mapping[str, str]) -> bool:
    better_somewhere = False
    for name, s in sense.items():
        va, vb = a.metric(name), b.metric(name)
        if va is None or vb is None:
            return False
        if s == "max":
            va, vb = -va, -vb
        if va > vb:
            return False
        if va < vb:
            better_somewhere = True
    return better_somewhere


def pareto_front(
    rows: Sequence[SweepRow], objectives: Mapping[str, str] | Iterable[str] | None = None
) -> list[SweepRow]:
    """Non-dominated rows for the objectives (``{"name": "min"|"max"}`` or a name list;
    ``temporal_coherence``/``cache_hits`` default to max). Rows lacking an objective are
    excluded."""
    sense = _sense(objectives if objectives is not None else DEFAULT_OBJECTIVES)
    usable = [r for r in rows if r.ok and all(r.metric(n) is not None for n in sense)]
    front = [r for r in usable if not any(_dominates(o, r, sense) for o in usable if o is not r)]
    return sorted(front, key=lambda r: r.index)


def _fmt(name: str, value: float | None) -> str:
    if value is None:
        return "-"
    if name in ("residual_rms", "gt_rmse"):
        return f"{value * 1000.0:.1f}"
    if name == "cache_hits":
        return f"{int(value)}"
    return f"{value:.3f}" if abs(value) < 100 else f"{value:.1f}"


def to_markdown(
    rows: Sequence[SweepRow],
    objectives: Mapping[str, str] | Iterable[str] | None = None,
    lang: str | None = None,
    pareto: Sequence[SweepRow] | None = None,
) -> str:
    """Markdown table of the rows (Pareto rows marked)."""
    front = pareto if pareto is not None else pareto_front(rows, objectives)
    front_ids = {r.index for r in front}
    cols = [t(f"validate.sweep.col_{k}", lang) for k in METRIC_KEYS]
    head = [
        t("validate.sweep.col_index", lang),
        t("validate.sweep.col_params", lang),
        *cols,
        t("validate.sweep.col_pareto", lang),
        t("validate.sweep.col_status", lang),
    ]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        params = ", ".join(f"{k}={v}" for k, v in r.params.items())
        vals = [_fmt(k, r.metric(k)) for k in METRIC_KEYS]
        mark = "*" if r.index in front_ids else ""
        status = t("validate.sweep.status_ok" if r.ok else "validate.sweep.status_failed", lang)
        lines.append(f"| {r.index} | {params} | " + " | ".join(vals) + f" | {mark} | {status} |")
    sense = _sense(objectives if objectives is not None else DEFAULT_OBJECTIVES)
    note = t(
        "validate.sweep.pareto_note",
        lang,
        objectives=", ".join(f"{k} ({v})" for k, v in sense.items()),
    )
    return "\n".join(lines) + "\n\n" + note + "\n"


def to_html_table(
    rows: Sequence[SweepRow],
    objectives: Mapping[str, str] | Iterable[str] | None = None,
    lang: str | None = None,
) -> str:
    """Minimal HTML table (for the validation report)."""
    import html

    front_ids = {r.index for r in pareto_front(rows, objectives)}
    head = [t("validate.sweep.col_index", lang), t("validate.sweep.col_params", lang)]
    head += [t(f"validate.sweep.col_{k}", lang) for k in METRIC_KEYS]
    head += [t("validate.sweep.col_pareto", lang), t("validate.sweep.col_status", lang)]
    out = [
        "<table><thead><tr>"
        + "".join(f"<th>{html.escape(h)}</th>" for h in head)
        + "</tr></thead><tbody>"
    ]
    for r in rows:
        params = html.escape(", ".join(f"{k}={v}" for k, v in r.params.items()))
        cells = [str(r.index), params, *(_fmt(k, r.metric(k)) for k in METRIC_KEYS)]
        cells.append("*" if r.index in front_ids else "")
        cells.append(
            t("validate.sweep.status_ok" if r.ok else "validate.sweep.status_failed", lang)
        )
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def plot_pareto(
    rows: Sequence[SweepRow],
    objectives: Sequence[str],
    path: Path | str,
    lang: str | None = None,
) -> Path | None:
    """Scatter of the first two objectives with the Pareto front highlighted (needs matplotlib)."""
    if len(objectives) < 2:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    x_name, y_name = objectives[0], objectives[1]
    usable = [r for r in rows if r.metric(x_name) is not None and r.metric(y_name) is not None]
    if not usable:
        return None
    front = {r.index for r in pareto_front(rows, list(objectives[:2]))}
    # ASCII-only text: the default matplotlib fonts have no Hangul glyphs.
    fig, ax = plt.subplots(figsize=(5.6, 4.0))
    xs: list[float] = [r.metric(x_name) or 0.0 for r in usable]
    ys: list[float] = [r.metric(y_name) or 0.0 for r in usable]
    ax.scatter(xs, ys, c=["#d62728" if r.index in front else "#1f77b4" for r in usable], s=36)
    for r, x, y in zip(usable, xs, ys, strict=True):
        ax.annotate(str(r.index), (x, y), fontsize=7, xytext=(3, 3), textcoords="offset points")
    ax.set_xlabel(x_name)
    ax.set_ylabel(y_name)
    ax.set_title("sweep: Pareto front (red)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, dpi=100)
    plt.close(fig)
    return p


def write_sweep(
    rows: Sequence[SweepRow],
    out_dir: Path | str,
    objectives: Sequence[str] | None = None,
    lang: str | None = None,
) -> dict[str, Path]:
    """``sweep.json``, ``sweep.md`` and (with matplotlib) ``sweep_pareto.png``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    objs = list(objectives) if objectives else list(DEFAULT_OBJECTIVES)
    front = pareto_front(rows, objs)
    payload = {
        "objectives": {k: v for k, v in _sense(objs).items()},
        "rows": [r.to_dict() for r in rows],
        "pareto": [r.index for r in front],
    }
    js = out / "sweep.json"
    js.write_text(json.dumps(mask_mapping(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    md = out / "sweep.md"
    md.write_text(to_markdown(rows, objs, lang, pareto=front), encoding="utf-8")
    paths = {"json": js, "markdown": md}
    png = plot_pareto(rows, objs, out / "sweep_pareto.png", lang)
    if png is not None:
        paths["plot"] = png
    return paths


__all__ = [
    "DEFAULT_MAX_POINTS",
    "DEFAULT_OBJECTIVES",
    "LOS_M_PER_RAD",
    "MAXIMISE",
    "METRIC_KEYS",
    "SweepRow",
    "SweepSpec",
    "apply_point",
    "closure_metrics",
    "deep_merge",
    "expand",
    "flatten",
    "grid_points",
    "load_sweep_yaml",
    "make_grid",
    "metrics_from_run",
    "network_residual_rms",
    "pareto_front",
    "plot_pareto",
    "run_sweep",
    "stack_from_artifacts",
    "to_html_table",
    "to_markdown",
    "write_sweep",
]
