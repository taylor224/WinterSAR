"""YAML-defined experiments → results JSON + Markdown table (plan §5.7 "experiments/",
Phase 6 DoD; ADR-0060, ADR-0064).

An experiment file looks like::

    name: S_synth_repr_phase
    kind: repr_phase                # repr_phase | stitching | seq_estimator_ab
    description: ...
    requires: []                    # importable modules needed; missing → skipped
    factor: 3
    seeds: [0, 1, 2]
    data: {kind: synthetic_slc_stack, n_dates: 12, shape: [48, 48], pair: [0, 3], ...}
    methods:
      - {name: ml}
      - {name: coh_weighted, params: {p: 2}}
    metrics: [phase_rmse_rad, wall_s]

:func:`run_experiment` runs every method for every seed, writes ``<out>/<name>.json`` (all
rows + summary) and ``<out>/<name>.md`` (summary table only — numbers live in the JSON,
prose interpretation goes to ADRs, rule 11.8) and optionally copies the Markdown into
``docs/research/results/``. Experiments whose ``requires`` are not importable are reported
as *skipped* with the missing packages (R-15 dolphin-vs-MintPy A/B, ADR-0064).
"""

from __future__ import annotations

import importlib.util
import json
import platform
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from wintersar import __version__
from wintersar.i18n import t
from wintersar.research import metrics as rmetrics
from wintersar.research import synth
from wintersar.research.repr_phase import (
    METHODS as REPR_METHODS,
)
from wintersar.research.repr_phase import (
    STACK_METHODS,
    ResearchError,
    representative_phase,
    truth_lowres_phase,
)
from wintersar.research.stitching import METHODS as STITCH_METHODS
from wintersar.research.stitching import stitch
from wintersar.util.masking import mask_mapping

KINDS: tuple[str, ...] = ("repr_phase", "stitching", "seq_estimator_ab")
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
#: Timing / memory metrics. They are measured and written to ``<name>.json`` but never
#: rendered into the committed Markdown table: rule 11.8 allows performance numbers only
#: with a ``bench_result.json`` behind them, which an experiment run does not produce.
PERF_METRICS: frozenset[str] = frozenset({"wall_s", "cpu_s", "peak_rss_mb"})

__all__ = [
    "KINDS",
    "PERF_METRICS",
    "Experiment",
    "ExperimentResult",
    "MethodSpec",
    "bundled_experiment_dir",
    "list_bundled",
    "load_experiment",
    "missing_requirements",
    "results_markdown",
    "run_experiment",
    "summarise",
    "write_results",
]


@dataclass
class MethodSpec:
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    label: str | None = None

    @property
    def key(self) -> str:
        if self.label:
            return self.label
        if not self.params:
            return self.name
        inner = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}({inner})"


@dataclass
class Experiment:
    name: str
    kind: str
    description: str = ""
    requires: list[str] = field(default_factory=list)
    factor: int = 3
    seeds: list[int] = field(default_factory=lambda: [0])
    data: dict[str, Any] = field(default_factory=dict)
    methods: list[MethodSpec] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    protocol: str | None = None
    path: Path | None = None
    ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path) if self.path else None
        return d


@dataclass
class ExperimentResult:
    name: str
    kind: str
    status: str
    reason: str | None = None
    missing: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    metrics: list[str] = field(default_factory=list)
    seeds: list[int] = field(default_factory=list)
    experiment: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    json_path: Path | None = None
    md_path: Path | None = None
    docs_md_path: Path | None = None

    @property
    def method_keys(self) -> list[str]:
        seen: list[str] = []
        for r in self.rows:
            if r["method"] not in seen:
                seen.append(r["method"])
        return seen

    def to_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "missing": list(self.missing),
            "metrics": list(self.metrics),
            "seeds": list(self.seeds),
            "experiment": self.experiment,
            "environment": self.environment,
            "rows": self.rows,
            "summary": self.summary,
            # file names only: committed results must not carry the author's directory layout
            "json_path": self.json_path.name if self.json_path else None,
            "md_path": self.md_path.name if self.md_path else None,
            "docs_md_path": self.docs_md_path.name if self.docs_md_path else None,
        }
        return dict(mask_mapping(d))


# ---------------------------------------------------------------- loading
def bundled_experiment_dir() -> Path:
    return Path(__file__).resolve().parent / "experiments"


def list_bundled() -> list[Path]:
    return sorted(bundled_experiment_dir().glob("*.yaml"))


def _method_specs(raw: Any) -> list[MethodSpec]:
    out: list[MethodSpec] = []
    for m in raw or []:
        if isinstance(m, str):
            out.append(MethodSpec(name=m))
        elif isinstance(m, dict) and "name" in m:
            out.append(
                MethodSpec(
                    name=str(m["name"]),
                    params=dict(m.get("params") or {}),
                    label=str(m["label"]) if m.get("label") else None,
                )
            )
        else:
            raise ResearchError("RES-008", detail=f"method entry {m!r} needs a name")
    return out


def load_experiment(path: Path | str) -> Experiment:
    """Parse and validate an experiment YAML (``RES-008`` on any structural problem)."""
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ResearchError("RES-008", detail=f"{p.name}: {e}") from e
    if not isinstance(raw, dict):
        raise ResearchError("RES-008", detail=f"{p.name}: top level must be a mapping")
    name = str(raw.get("name") or p.stem)
    kind = str(raw.get("kind") or "")
    if kind not in KINDS:
        raise ResearchError("RES-008", detail=f"kind {kind!r} not in {KINDS}")
    methods = _method_specs(raw.get("methods"))
    known = REPR_METHODS.keys() if kind == "repr_phase" else STITCH_METHODS
    if kind != "seq_estimator_ab":
        bad = [m.name for m in methods if m.name not in known]
        if bad:
            raise ResearchError("RES-008", detail=f"unknown methods {bad} for kind {kind}")
        if not methods:
            raise ResearchError("RES-008", detail="methods list is empty")
    seeds = [int(s) for s in (raw.get("seeds") or [0])]
    factor = int(raw["factor"]) if raw.get("factor") is not None else 3
    if factor < 1:
        raise ResearchError("RES-008", detail=f"factor must be >= 1, got {factor}")
    metrics = [str(m) for m in (raw.get("metrics") or [])]
    unknown_metrics = [m for m in metrics if m not in rmetrics.METRIC_NAMES]
    if unknown_metrics:
        raise ResearchError(
            "RES-008",
            detail=f"unknown metrics {unknown_metrics}; known: {list(rmetrics.METRIC_NAMES)}",
        )
    return Experiment(
        name=name,
        kind=kind,
        description=str(raw.get("description") or ""),
        requires=[str(r) for r in (raw.get("requires") or [])],
        factor=factor,
        seeds=seeds,
        data=dict(raw.get("data") or {}),
        methods=methods,
        metrics=metrics,
        protocol=str(raw["protocol"]) if raw.get("protocol") else None,
        path=p,
        ids=[str(i) for i in (raw.get("ids") or [])],
    )


def missing_requirements(exp: Experiment) -> list[str]:
    """Names in ``requires`` that are not importable (checked with ``find_spec``, no import)."""
    missing: list[str] = []
    for name in exp.requires:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            missing.append(name)
    return missing


# ---------------------------------------------------------------- data builders
def _shape(data: dict[str, Any], default: tuple[int, int]) -> tuple[int, int]:
    s = data.get("shape", default)
    return (int(s[0]), int(s[1]))


def _repr_dataset(data: dict[str, Any], seed: int) -> dict[str, Any]:
    """``{"igram", "coh", "truth", "stack_amp", "stack_slc", "pair", "mask"}`` for one seed."""
    rng = np.random.default_rng(seed)
    kind = str(data.get("kind", "synthetic_slc_stack"))
    if kind == "synthetic_slc_stack":
        n = int(data.get("n_dates", 12))
        shape = _shape(data, (48, 48))
        regions = synth.shp_regions(shape, str(data.get("region_kind", "halves")), rng)
        scales = np.asarray(data.get("region_scales", [1.0, 3.0]), dtype=np.float64)
        amp = scales[np.minimum(regions, scales.size - 1)]
        st = synth.make_slc_stack(
            n,
            shape,
            rng,
            tau_dates=float(data.get("tau_dates", 4.0)),
            coherence_floor=float(data.get("coherence_floor", 0.1)),
            amplitude=amp,
        )
        i, j = (int(v) for v in data.get("pair", [0, min(3, n - 1)]))
        igram = st.slc[j] * np.conj(st.slc[i])
        coh = np.full(shape, float(st.gamma[i, j]), dtype=np.float64)
        return {
            "igram": np.asarray(igram, dtype=np.complex128),
            "coh": coh,
            "truth": st.pair_phase_true(i, j),
            "stack_amp": np.abs(st.slc),
            "stack_slc": st.slc,
            "pair": (i, j),
            "mask": None,
        }
    if kind == "synthetic_igram":
        shape = _shape(data, (96, 96))
        kw = {
            k: v
            for k, v in data.items()
            if k
            in (
                "deformation_kind",
                "deformation_amplitude_m",
                "deformation_ramp",
                "deformation_sigma_px",
                "atmosphere_std_rad",
                "coherence_base",
                "looks",
                "water_fraction",
                "dem_error_rad",
                "noise_model",
            )
        }
        ig = synth.make_interferogram(shape, rng, **kw)
        return {
            "igram": ig.complex,
            "coh": ig.coherence,
            "truth": ig.unw_true,
            "stack_amp": None,
            "stack_slc": None,
            "pair": None,
            "mask": ig.mask,
        }
    raise ResearchError("RES-008", detail=f"unknown data kind {kind!r} for repr_phase")


def _run_repr_phase(exp: Experiment, seed: int) -> list[dict[str, Any]]:
    ds = _repr_dataset(exp.data, seed)
    f = exp.factor
    truth_lo = truth_lowres_phase(ds["truth"], f)
    mask_lo = None
    if ds["mask"] is not None:
        mask_lo = truth_lowres_phase(np.asarray(ds["mask"], dtype=np.float64), f) > 0
    rows: list[dict[str, Any]] = []
    for m in exp.methods:
        row: dict[str, Any] = {"method": m.key, "seed": seed, "status": STATUS_OK}
        stack = ds["stack_slc"] if m.name == "phase_link" else ds["stack_amp"]
        if m.name in STACK_METHODS and stack is None:
            row.update({"status": STATUS_SKIPPED, "reason": "stack_required"})
            rows.append(row)
            continue
        params = dict(m.params)
        if m.name == "phase_link":
            params.setdefault("pair", ds["pair"])
        with rmetrics.ResourceTimer() as rt:
            est = representative_phase(m.name, ds["igram"], ds["coh"], f, stack=stack, **params)
        meas = rt.result
        row.update(
            {
                "phase_rmse_rad": rmetrics.phase_rmse(est, truth_lo, mask_lo),
                "phase_mae_rad": rmetrics.phase_mae(est, truth_lo, mask_lo),
                "mean_magnitude": float(np.mean(np.abs(est))),
                "wall_s": meas.wall_s,
                "cpu_s": meas.cpu_s,
                "peak_rss_mb": meas.peak_rss_mb,
            }
        )
        rows.append(row)
    return rows


def _run_stitching(exp: Experiment, seed: int) -> list[dict[str, Any]]:
    data = exp.data
    rng = np.random.default_rng(seed)
    shape = _shape(data, (128, 128))
    igram_kw = {
        k: v
        for k, v in data.items()
        if k
        in (
            "atmosphere_std_rad",
            "coherence_base",
            "looks",
            "noise_model",
            "deformation_amplitude_m",
            "deformation_kind",
            "deformation_ramp",
        )
    }
    tt = synth.make_tiled_truth(
        shape,
        int(data.get("rows", 2)),
        int(data.get("cols", 2)),
        int(data.get("overlap", 16)),
        rng,
        max_offset_cycles=int(data.get("max_offset_cycles", 3)),
        noise_std_rad=float(data.get("noise_std_rad", 0.0)),
        **igram_kw,
    )
    ref = truth_lowres_phase(tt.unw_true, exp.factor)
    ref_noise = float(data.get("ref_noise_std_rad", 0.0))
    if ref_noise > 0:
        ref = ref + rng.standard_normal(ref.shape) * ref_noise
    rows: list[dict[str, Any]] = []
    for m in exp.methods:
        row: dict[str, Any] = {"method": m.key, "seed": seed, "status": STATUS_OK}
        with rmetrics.ResourceTimer() as rt:
            merged, offsets, report = stitch(
                tt.tiles,
                tt.shape,
                m.name,
                lowres_ref=ref if m.name == "coarse_ref" else None,
                coh=tt.coherence if m.name == "overlap_consensus" else None,
                **m.params,
            )
        meas = rt.result
        rec = rmetrics.offsets_recovered(offsets, tt.offsets_cycles)
        bj = report["boundary_jumps"]
        row.update(
            {
                "offsets_exact": rec["offsets_exact"],
                "n_wrong_offsets": rec["n_wrong_offsets"],
                "seam_boundaries_with_jump": float(bj["n_boundaries_with_jump"]),
                "seam_jump_pixels": float(bj["n_jump_pixels"]),
                "unwrap_error_fraction": rmetrics.unwrap_error_fraction(
                    merged.astype(np.float64), tt.unw_true, tt.mask
                ),
                "wall_s": meas.wall_s,
                "cpu_s": meas.cpu_s,
                "peak_rss_mb": meas.peak_rss_mb,
            }
        )
        rows.append(row)
    return rows


# ---------------------------------------------------------------- summary / report
def summarise(
    rows: list[dict[str, Any]], metric_names: list[str]
) -> dict[str, dict[str, dict[str, float]]]:
    """``{method: {metric: {mean, std, min, max, n}}}`` over seeds (NaN-aware)."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    methods: list[str] = []
    for r in rows:
        if r["method"] not in methods:
            methods.append(r["method"])
    for mk in methods:
        out[mk] = {}
        for metric in metric_names:
            vals = np.asarray(
                [
                    float(r[metric])
                    for r in rows
                    if r["method"] == mk and metric in r and r[metric] is not None
                ],
                dtype=np.float64,
            )
            vals = vals[np.isfinite(vals)]
            if vals.size == 0:
                continue
            out[mk][metric] = {
                "mean": float(np.mean(vals)),
                "std": float(np.std(vals, ddof=1)) if vals.size > 1 else 0.0,
                "min": float(np.min(vals)),
                "max": float(np.max(vals)),
                "n": float(vals.size),
            }
    return out


def _fmt(v: float) -> str:
    if not np.isfinite(v):
        return "nan"
    if v == 0 or abs(v) >= 100:
        return f"{v:.1f}"
    if abs(v) >= 1:
        return f"{v:.3f}"
    return f"{v:.4f}"


def results_markdown(result: ExperimentResult, lang: str | None = None) -> str:
    """Summary table (mean ± std over seeds per method and metric). Tables only."""
    lines = [f"# {result.name}", ""]
    if result.experiment.get("description"):
        lines += [str(result.experiment["description"]), ""]
    if result.experiment.get("ids"):
        lines += [", ".join(str(i) for i in result.experiment["ids"]), ""]
    json_name = result.json_path.name if result.json_path else f"{result.name}.json"
    lines += [t("research.experiment.generated_from", lang, json=json_name), ""]
    if result.status != STATUS_OK:
        reason = t(
            f"research.experiment.skipped_reason.{result.reason}",
            lang,
            missing=", ".join(result.missing),
        )
        lines += [t("research.experiment.status_skipped", lang, reason=reason), ""]
        if result.missing:
            lines += [
                t("research.experiment.missing_packages", lang, missing=", ".join(result.missing)),
                "",
            ]
        if result.experiment.get("protocol"):
            lines += [
                f"## {t('research.experiment.protocol', lang)}",
                "",
                str(result.experiment["protocol"]).rstrip(),
                "",
            ]
        return "\n".join(lines) + "\n"
    lines += [
        f"- {t('research.experiment.seeds', lang)}: {', '.join(str(s) for s in result.seeds)}",
        f"- {t('research.experiment.data', lang)}: `{json.dumps(result.experiment.get('data', {}), ensure_ascii=False, sort_keys=True)}`",
        f"- factor: {result.experiment.get('factor')}",
        "",
    ]
    metric_names = [m for m in result.metrics if m not in PERF_METRICS]
    dropped = [m for m in result.metrics if m in PERF_METRICS]
    if dropped:
        lines += [t("research.experiment.perf_in_json", lang, metrics=", ".join(dropped)), ""]
    header = [t("research.experiment.method", lang), *metric_names]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for mk in result.method_keys:
        cells = [mk]
        for metric in metric_names:
            s = result.summary.get(mk, {}).get(metric)
            if s is None:
                cells.append("-")
            elif s["n"] > 1:
                cells.append(f"{_fmt(s['mean'])} ± {_fmt(s['std'])} (n={int(s['n'])})")
            else:
                cells.append(f"{_fmt(s['mean'])} (n=1)")
        lines.append("| " + " | ".join(cells) + " |")
    skipped = [r for r in result.rows if r.get("status") == STATUS_SKIPPED]
    if skipped:
        lines += [
            "",
            "| "
            + t("research.experiment.method", lang)
            + " | "
            + t("research.experiment.status", lang)
            + " |",
            "|---|---|",
        ]
        seen: set[str] = set()
        for r in skipped:
            if r["method"] in seen:
                continue
            seen.add(r["method"])
            lines.append(f"| {r['method']} | {STATUS_SKIPPED}: {r.get('reason', '')} |")
    return "\n".join(lines) + "\n"


def write_results(
    result: ExperimentResult, out_dir: Path | str, docs_dir: Path | str | None = None
) -> ExperimentResult:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result.json_path = out / f"{result.name}.json"
    result.md_path = out / f"{result.name}.md"
    result.json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    result.md_path.write_text(results_markdown(result), encoding="utf-8")
    if docs_dir is not None:
        d = Path(docs_dir)
        d.mkdir(parents=True, exist_ok=True)
        result.docs_md_path = d / f"{result.name}.md"
        shutil.copyfile(result.md_path, result.docs_md_path)
        shutil.copyfile(result.json_path, d / f"{result.name}.json")
    return result


def _environment() -> dict[str, Any]:
    return {
        "wintersar": __version__,
        "python": platform.python_version(),
        "numpy": np.__version__,
        # platform family + architecture only: the kernel release fingerprints the machine
        "os": f"{platform.system().lower()}-{platform.machine()}",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def run_experiment(
    exp: Experiment, out_dir: Path | str | None = None, docs_dir: Path | str | None = None
) -> ExperimentResult:
    """Run all methods x seeds; write results when ``out_dir`` is given."""
    missing = missing_requirements(exp)
    metric_names = list(exp.metrics) or list(rmetrics.METRIC_NAMES)
    result = ExperimentResult(
        name=exp.name,
        kind=exp.kind,
        status=STATUS_OK,
        metrics=metric_names,
        seeds=list(exp.seeds),
        experiment=exp.to_dict(),
        environment=_environment(),
    )
    if missing:
        result.status = STATUS_SKIPPED
        result.reason = "requires"
        result.missing = missing
    elif exp.kind == "seq_estimator_ab":
        # The A/B needs real data and two external engines; the YAML documents the protocol.
        result.status = STATUS_SKIPPED
        result.reason = "manual_protocol"
    else:
        runner = _run_repr_phase if exp.kind == "repr_phase" else _run_stitching
        for seed in exp.seeds:
            result.rows.extend(runner(exp, seed))
        present = [
            m
            for m in metric_names
            if any(m in r for r in result.rows if r.get("status") == STATUS_OK)
        ]
        result.metrics = present or metric_names
        result.summary = summarise(result.rows, result.metrics)
    if out_dir is not None:
        write_results(result, out_dir, docs_dir)
    return result
