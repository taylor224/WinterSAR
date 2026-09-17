"""``wintersar validate | refpoint | sweep | closure`` (plan §4.5, §5.6; R-09, R-10, R-11).

* ``validate --ts PATH --leveling CSV [--gnss CSV] [--out DIR] [--radius M]``
* ``refpoint --ts PATH [--aoi GEOJSON] [--top 5] [--coherence NPY] [--dem NPY] [--out JSON]``
* ``sweep --config config.yaml --grid sweep.yaml [--out DIR] [--leveling CSV] [--gnss CSV]``
* ``closure --igrams NPZ [--unw NPZ] [--out DIR] [--wrapped]``

All honour the global ``--json`` / ``--lang`` state (``wintersar.util.clistate.state``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import typer
from rich.table import Table

from wintersar.i18n import t
from wintersar.io.igrams import load_igram_stack
from wintersar.io.schemas import Finding
from wintersar.util.clistate import state
from wintersar.util.masking import mask_text
from wintersar.util.output import console, emit_json, err_console, print_findings

TsOpt = Annotated[
    Path,
    typer.Option(
        "--ts", help="Time series (.npz from the fake engine, or any format io.formats reads)."
    ),
]


def _require(path: Path | None, key: str) -> None:
    if path is not None and not path.exists():
        err_console.print(f"[red]{t(key, path=mask_text(str(path)))}[/]")
        raise typer.Exit(code=2)


def _load_ts(ts: Path, heading: float | None, incidence: float | None) -> Any:
    from wintersar.validate.api import ValidateError, load_timeseries

    _require(ts, "validate.cli.ts_not_found")
    try:
        return load_timeseries(ts, heading_deg=heading, incidence_deg=incidence)
    except ValidateError as exc:
        _fail([exc.finding], "validate")


def _fail(findings: list[Finding], command: str) -> None:
    if state.json:
        emit_json(command, None, findings, ok=False)
    else:
        print_findings(findings)
    raise typer.Exit(code=1)


def _load_npy(path: Path | None) -> Any:
    if path is None:
        return None
    if path.suffix == ".npz":
        with np.load(path, allow_pickle=False) as z:
            return z[z.files[0]]
    return np.load(path, allow_pickle=False)


def validate_cmd(
    ts: TsOpt,
    leveling: Annotated[
        Path | None, typer.Option("--leveling", help="Levelling CSV (plan §5.6 schema).")
    ] = None,
    gnss: Annotated[Path | None, typer.Option("--gnss", help="GNSS CSV (ENU columns).")] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Report directory (default: <ts dir>/validate)."),
    ] = None,
    radius: Annotated[
        float, typer.Option("--radius", help="Pixel averaging radius around each site (m).")
    ] = 100.0,
    method: Annotated[str, typer.Option("--method", help="mean | median")] = "mean",
    align: Annotated[str, typer.Option("--align", help="nearest | interp")] = "nearest",
    max_gap_days: Annotated[
        int, typer.Option("--max-gap-days", help="Tolerance for nearest-date alignment.")
    ] = 6,
    heading: Annotated[
        float | None,
        typer.Option("--heading", help="Override satellite heading (deg, clockwise from north)."),
    ] = None,
    incidence: Annotated[
        float | None,
        typer.Option("--incidence", help="Override incidence angle (deg from vertical)."),
    ] = None,
    no_plots: Annotated[bool, typer.Option("--no-plots", help="Skip PNG plots.")] = False,
) -> None:
    """Compare the InSAR time series with levelling / GNSS ground truth (R-10)."""
    from wintersar.validate.api import ValidateError, validate_timeseries
    from wintersar.validate.ground_truth import GroundTruthError

    _require(ts, "validate.cli.ts_not_found")
    _require(leveling, "validate.cli.csv_not_found")
    _require(gnss, "validate.cli.csv_not_found")
    if method not in ("mean", "median") or align not in ("nearest", "interp"):
        raise typer.BadParameter("--method mean|median, --align nearest|interp")
    try:
        result, paths = validate_timeseries(
            ts,
            leveling,
            gnss,
            out,
            state.lang,
            radius_m=radius,
            method=method,  # type: ignore[arg-type]
            align=align,  # type: ignore[arg-type]
            max_gap_days=max_gap_days,
            heading_deg=heading,
            incidence_deg=incidence,
            plots=not no_plots,
        )
    except (GroundTruthError, ValidateError) as exc:
        _fail([exc.finding], "validate")
        return
    findings = list(result.findings)
    data = {
        **result.to_dict(),
        "paths": {k: mask_text(str(v)) for k, v in paths.items()},
    }
    if state.json:
        emit_json("validate", data, findings, ok=not any(f.is_fail for f in findings))
        return
    console.print(f"[bold]{t('validate.cli.validate_title')}[/] — {mask_text(str(ts))}")
    if result.n_sites == 0:
        console.print(t("validate.cli.no_sites"))
    else:
        table = Table(expand=True)
        for key in (
            "validate.report.col_site",
            "validate.report.col_method",
            "validate.report.col_n",
            "validate.report.col_rmse",
            "validate.report.col_bias",
            "validate.report.col_corr",
            "validate.report.col_v_diff",
            "validate.report.col_distance",
        ):
            table.add_column(t(key))
        for s in result.sites_compared:
            table.add_row(
                s.site_id,
                s.method,
                str(s.n),
                f"{s.rmse_m * 1000:.1f}",
                f"{s.bias_m * 1000:.1f}",
                "-" if not np.isfinite(s.corr) else f"{s.corr:.2f}",
                "-"
                if not np.isfinite(s.velocity_diff_m_per_yr)
                else f"{s.velocity_diff_m_per_yr * 1000:.1f}",
                f"{s.distance_to_pixel_m:.0f}",
            )
        console.print(table)
        console.print(
            t(
                "validate.cli.summary",
                rmse_mm=f"{result.rmse_m * 1000:.1f}",
                bias_mm=f"{result.bias_m * 1000:.1f}",
                n_sites=result.n_sites,
                n_points=result.n_points,
                radius_m=f"{result.radius_m:.0f}",
            )
        )
    print_findings([f for f in findings if f.rule_id != "VAL-013"])
    for p in paths.values():
        console.print(t("cli.wrote", path=mask_text(str(p))))


def refpoint_cmd(
    ts: TsOpt,
    aoi: Annotated[
        Path | None,
        typer.Option("--aoi", help="AOI GeoJSON (lon/lat); default: all valid pixels."),
    ] = None,
    top: Annotated[int, typer.Option("--top", help="Number of candidates.")] = 5,
    coherence: Annotated[
        Path | None, typer.Option("--coherence", help="Mean coherence map (.npy/.npz).")
    ] = None,
    conncomp: Annotated[
        Path | None, typer.Option("--conncomp", help="Connected-component map (.npy/.npz).")
    ] = None,
    dem: Annotated[
        Path | None, typer.Option("--dem", help="Elevation map (.npy/.npz, metres).")
    ] = None,
    weights: Annotated[
        str | None,
        typer.Option(
            "--weights", help="Override weights, e.g. coherence=0.4,conncomp=0.3 (ADR-0041)."
        ),
    ] = None,
    min_coherence: Annotated[
        float, typer.Option("--min-coherence", help="Discard pixels below this mean coherence.")
    ] = 0.0,
    mintpy_threshold: Annotated[
        float,
        typer.Option("--mintpy-threshold", help="MintPy minCoherence for the comparison."),
    ] = 0.85,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write candidates JSON here.")
    ] = None,
) -> None:
    """Recommend reference-point candidates and compare with MintPy's auto rule (R-09)."""
    from wintersar.validate.refpoint import (
        DEFAULT_WEIGHTS,
        aoi_mask_from_geojson,
        compare_with_mintpy_auto,
        recommend,
    )

    _require(aoi, "validate.cli.aoi_not_found")
    tsobj = _load_ts(ts, None, None)
    w: dict[str, float] | None = None
    if weights:
        w = dict(DEFAULT_WEIGHTS)
        for item in weights.split(","):
            k, _, v = item.partition("=")
            w[k.strip()] = float(v)
    mask = aoi_mask_from_geojson(aoi, tsobj) if aoi is not None else None
    coh = _load_npy(coherence)
    kw: dict[str, Any] = {
        "coherence": coh,
        "conncomp": _load_npy(conncomp),
        "dem": _load_npy(dem),
        "min_coherence": min_coherence,
    }
    try:
        candidates = recommend(tsobj, mask, w, top_k=top, **kw)
    except ValueError as exc:
        err_console.print(f"[red]{mask_text(str(exc))}[/]")
        raise typer.Exit(code=2) from None
    comparison: dict[str, Any] | None = None
    if coh is not None or tsobj.coherence is not None:
        comparison = compare_with_mintpy_auto(
            tsobj, coh, threshold=mintpy_threshold, candidates=candidates
        )
    data = {
        "ts": mask_text(str(ts)),
        "aoi": mask_text(str(aoi)) if aoi else None,
        "n_aoi_pixels": int(mask.sum()) if mask is not None else None,
        "weights": w or dict(DEFAULT_WEIGHTS),
        "candidates": [c.to_dict() for c in candidates],
        "mintpy": comparison,
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        data["path"] = mask_text(str(out))
    if state.json:
        emit_json("refpoint", data, [], ok=bool(candidates))
        return
    console.print(f"[bold]{t('validate.cli.refpoint_title')}[/] — {mask_text(str(ts))}")
    console.print(
        t(
            "validate.cli.loaded_ts",
            n_dates=tsobj.n_dates,
            ny=tsobj.shape[0],
            nx=tsobj.shape[1],
            source=mask_text(str(tsobj.attrs.get("source", ts))),
        )
    )
    if not candidates:
        console.print(t("validate.cli.refpoint_none"))
        raise typer.Exit(code=1)
    table = Table(expand=True)
    for key in (
        "validate.cli.col_rank",
        "validate.cli.col_row",
        "validate.cli.col_col",
        "validate.cli.col_lat",
        "validate.cli.col_lon",
        "validate.cli.col_score",
        "validate.cli.col_coherence",
        "validate.cli.col_components",
    ):
        table.add_column(t(key))
    for i, c in enumerate(candidates):
        comps = " ".join(f"{k[:4]}={v:.2f}" for k, v in c.components.items())
        table.add_row(
            str(i + 1),
            str(c.row),
            str(c.col),
            f"{c.lat:.5f}",
            f"{c.lon:.5f}",
            f"{c.score:.3f}",
            "-" if c.coherence is None else f"{c.coherence:.2f}",
            comps,
        )
    console.print(table)
    best = candidates[0]
    console.print(
        t("validate.cli.refpoint_apply_hint", lat=f"{best.lat:.5f}", lon=f"{best.lon:.5f}")
    )
    if comparison is not None:
        console.print(
            t(
                "validate.cli.mintpy_rule",
                threshold=comparison["threshold"],
                n=comparison["n_candidates_mintpy"],
                fraction=f"{comparison['fraction_of_scene']:.1%}",
                inside=t("validate.report.yes")
                if comparison["top1_in_mintpy_set"]
                else t("validate.report.no"),
            )
        )
    if out is not None:
        console.print(t("cli.wrote", path=mask_text(str(out))))


def sweep_cmd(
    config: Annotated[Path, typer.Option("--config", "-c", help="config.yaml (plan §4.4)")],
    grid: Annotated[Path, typer.Option("--grid", help="sweep.yaml (grid of dotted config keys).")],
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Output directory (default: <workdir>/sweep)."),
    ] = None,
    leveling: Annotated[
        Path | None,
        typer.Option(
            "--leveling",
            help="Levelling CSV for gt_rmse (default: config validate.leveling_csv).",
        ),
    ] = None,
    gnss: Annotated[Path | None, typer.Option("--gnss", help="GNSS CSV for gt_rmse.")] = None,
    radius: Annotated[float, typer.Option("--radius", help="Site averaging radius (m).")] = 100.0,
    set_: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Stage parameter override stage.key=value (same as 'wintersar run --set').",
        ),
    ] = None,
) -> None:
    """Run the parameter grid through the cached DAG and rank the results (R-08, R-11)."""
    from wintersar.pipeline.cli import parse_set
    from wintersar.pipeline.config import load_config
    from wintersar.validate.api import load_ground_truth
    from wintersar.validate.ground_truth import GroundTruthError
    from wintersar.validate.sweep import (
        grid_points,
        load_sweep_yaml,
        objectives_finding,
        pareto_front,
        run_sweep,
        write_sweep,
    )

    _require(grid, "validate.cli.grid_not_found")
    _require(leveling, "validate.cli.csv_not_found")
    _require(gnss, "validate.cli.csv_not_found")
    try:
        cfg = load_config(config)
    except FileNotFoundError:
        err_console.print(f"[red]{t('cli.config_not_found', path=mask_text(str(config)))}[/]")
        raise typer.Exit(code=2) from None
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        err_console.print(f"[red]{t('cli.invalid_config', error=mask_text(str(exc)))}[/]")
        raise typer.Exit(code=2) from None
    try:
        spec = load_sweep_yaml(grid)
        points = grid_points(spec)
    except ValueError as exc:
        _fail(
            [
                Finding(
                    rule_id="VAL-015",
                    severity="FAIL",
                    message_key="validate.VAL-015.cause",
                    fix_key="validate.VAL-015.fix",
                    params={"path": mask_text(str(grid)), "reason": mask_text(str(exc))},
                )
            ],
            "sweep",
        )
        return
    lev = leveling if leveling is not None else cfg.validation.leveling_csv
    gn = gnss if gnss is not None else (cfg.validation.gnss.path if cfg.validation.gnss else None)
    gt = None
    if lev is not None or gn is not None:
        try:
            gt = load_ground_truth(lev, gn)
        except GroundTruthError as exc:
            _fail([exc.finding], "sweep")
            return
    out_dir = out if out is not None else cfg.workdir / "sweep"
    overrides = parse_set(set_)
    if not state.json:
        console.print(
            f"[bold]{t('validate.cli.sweep_title')}[/] — {t('validate.cli.sweep_points', n=len(points))}"
        )

    def progress(i: int, n: int, params: dict[str, Any]) -> None:
        if not state.json:
            console.print(
                t(
                    "validate.cli.sweep_running",
                    index=i,
                    n=n,
                    params=json.dumps(params, ensure_ascii=False),
                )
            )

    rows = run_sweep(
        cfg, points, gt, param_overrides=overrides or None, radius_m=radius, progress=progress
    )
    front = pareto_front(rows, spec.objectives)
    paths = write_sweep(rows, out_dir, spec.objectives, state.lang)
    findings = [f for r in rows for f in r.findings]
    dropped = objectives_finding(rows, spec.objectives)
    if dropped is not None:  # e.g. gt_rmse without ground truth: say so, do not rank on it
        findings.append(dropped)
    data = {
        "n_points": len(points),
        "objectives": spec.objectives,
        "rows": [r.to_dict() for r in rows],
        "pareto": [r.index for r in front],
        "paths": {k: mask_text(str(v)) for k, v in paths.items()},
    }
    if state.json:
        emit_json("sweep", data, findings, ok=any(r.ok for r in rows))
        return
    console.print((out_dir / "sweep.md").read_text(encoding="utf-8"))
    console.print(
        t(
            "validate.cli.sweep_done",
            n=len(rows),
            n_ok=sum(1 for r in rows if r.ok),
            n_pareto=len(front),
        )
    )
    print_findings(findings)
    for p in paths.values():
        console.print(t("cli.wrote", path=mask_text(str(p))))


def closure_cmd(
    igrams: Annotated[
        Path,
        typer.Option(
            "--igrams", help="Interferogram stack .npz (wrapped, coherence, pairs, dates)."
        ),
    ],
    unw: Annotated[
        Path | None,
        typer.Option("--unw", help="Unwrapped stack .npz (unw, optional conncomp)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write closure_dashboard.json (+ maps .npz) here."),
    ] = None,
    wrapped: Annotated[
        bool,
        typer.Option("--wrapped", help="Use the wrapped phase even when unw is available."),
    ] = False,
    top: Annotated[int, typer.Option("--top", help="Rows to print.")] = 10,
) -> None:
    """Loop-closure statistics: per-triplet/pixel RMS and suspicious interferograms (§12.1)."""
    from wintersar.validate.closure import (
        closure_phase,
        write_closure_maps,
        write_dashboard_json,
    )

    _require(igrams, "validate.cli.igrams_not_found")
    _require(unw, "validate.cli.unw_not_found")
    stack = load_igram_stack(igrams)
    if unw is not None:
        with np.load(unw, allow_pickle=False) as z:
            stack.unw = np.asarray(z["unw"], dtype=np.float32) if "unw" in z.files else None
            if "conncomp" in z.files:
                stack.conncomp = z["conncomp"]
    result = closure_phase(stack, use_unw=not wrapped)
    dash = result.to_dashboard()
    paths: dict[str, str] = {}
    if out is not None:
        paths["json"] = mask_text(str(write_dashboard_json(result, out / "closure_dashboard.json")))
        paths["maps"] = mask_text(str(write_closure_maps(result, out / "closure_maps.npz")))
    data = {**dash, "igrams_path": mask_text(str(igrams)), "paths": paths}
    if state.json:
        emit_json("closure", data, [], ok=True)
        return
    console.print(f"[bold]{t('validate.cli.closure_title')}[/] — {mask_text(str(igrams))}")
    rms = dash["summary"].get("rms_mean_rad")
    console.print(
        t(
            "validate.cli.closure_summary",
            mode=result.mode,
            n_igrams=len(result.pairs),
            n_triplets=result.n_triplets,
            rms_rad="-" if rms is None else f"{rms:.3f}",
            suspicious=", ".join(result.suspicious) or t("common.none"),
        )
    )
    table = Table(expand=True)
    for key in (
        "validate.cli.col_rank",
        "validate.cli.col_pair",
        "validate.cli.col_score",
        "validate.cli.col_n_triplets",
        "validate.cli.col_suspicious",
    ):
        table.add_column(t(key))
    for r in dash["igrams"][:top]:
        table.add_row(
            str(r["rank"]),
            str(r["pair"]),
            f"{r['score']:.3f}",
            str(r["n_triplets"]),
            t("validate.report.yes") if r["suspicious"] else t("validate.report.no"),
        )
    console.print(table)
    for p in paths.values():
        console.print(t("cli.wrote", path=p))


def register(app: typer.Typer) -> None:
    """Mount the four commands on the root app (same shape as pipeline/cli.py)."""
    app.command("validate")(validate_cmd)
    app.command("refpoint")(refpoint_cmd)
    app.command("sweep")(sweep_cmd)
    app.command("closure")(closure_cmd)
