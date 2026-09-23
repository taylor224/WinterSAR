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
from wintersar.util.clihelp import h
from wintersar.util.clistate import state
from wintersar.util.masking import mask_text
from wintersar.util.output import (
    CLI_BAD_VALUE,
    CLI_CONFIG_INVALID,
    CLI_CONFIG_MISSING,
    CLI_INPUT_MISSING,
    cli_finding,
    console,
    emit_json,
    exit_with_findings,
    print_findings,
)

TsOpt = Annotated[Path, typer.Option("--ts", help=h("cli_help.common.ts"))]


def _require(path: Path | None, key: str, command: str, option: str) -> None:
    """A named input must exist: CLI-006 with the module's own cause text, exit 2."""
    if path is not None and not path.exists():
        finding = cli_finding(CLI_INPUT_MISSING, message_key=key, path=str(path), option=option)
        raise exit_with_findings(command, [finding])


def _bad_value(command: str, option: str, value: Any, allowed: str) -> typer.Exit:
    finding = cli_finding(
        CLI_BAD_VALUE, option=option, value=str(value), allowed=allowed, command=command
    )
    return exit_with_findings(command, [finding])


def _load_ts(ts: Path, heading: float | None, incidence: float | None, command: str) -> Any:
    from wintersar.validate.api import ValidateError, load_timeseries

    _require(ts, "validate.cli.ts_not_found", command, "--ts")
    try:
        return load_timeseries(ts, heading_deg=heading, incidence_deg=incidence)
    except ValidateError as exc:
        _fail([exc.finding], command)


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
        Path | None, typer.Option("--leveling", help=h("cli_help.validate.leveling"))
    ] = None,
    gnss: Annotated[Path | None, typer.Option("--gnss", help=h("cli_help.validate.gnss"))] = None,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help=h("cli_help.common.out_dir"))
    ] = None,
    radius: Annotated[float, typer.Option("--radius", help=h("cli_help.validate.radius"))] = 100.0,
    method: Annotated[str, typer.Option("--method", help=h("cli_help.validate.method"))] = "mean",
    align: Annotated[str, typer.Option("--align", help=h("cli_help.validate.align"))] = "nearest",
    max_gap_days: Annotated[
        int, typer.Option("--max-gap-days", help=h("cli_help.validate.max_gap_days"))
    ] = 6,
    heading: Annotated[
        float | None, typer.Option("--heading", help=h("cli_help.validate.heading"))
    ] = None,
    incidence: Annotated[
        float | None, typer.Option("--incidence", help=h("cli_help.validate.incidence"))
    ] = None,
    no_plots: Annotated[
        bool, typer.Option("--no-plots", help=h("cli_help.validate.no_plots"))
    ] = False,
) -> None:
    """Compare the InSAR time series with levelling / GNSS ground truth (R-10)."""
    from wintersar.validate.api import ValidateError, validate_timeseries
    from wintersar.validate.ground_truth import GroundTruthError

    command = "validate"
    _require(ts, "validate.cli.ts_not_found", command, "--ts")
    _require(leveling, "validate.cli.csv_not_found", command, "--leveling")
    _require(gnss, "validate.cli.csv_not_found", command, "--gnss")
    if method not in ("mean", "median"):
        raise _bad_value(command, "--method", method, "mean | median")
    if align not in ("nearest", "interp"):
        raise _bad_value(command, "--align", align, "nearest | interp")
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
    aoi: Annotated[Path | None, typer.Option("--aoi", help=h("cli_help.refpoint.aoi"))] = None,
    top: Annotated[int, typer.Option("--top", help=h("cli_help.refpoint.top"))] = 5,
    coherence: Annotated[
        Path | None, typer.Option("--coherence", help=h("cli_help.refpoint.coherence"))
    ] = None,
    conncomp: Annotated[
        Path | None, typer.Option("--conncomp", help=h("cli_help.refpoint.conncomp"))
    ] = None,
    dem: Annotated[Path | None, typer.Option("--dem", help=h("cli_help.refpoint.dem"))] = None,
    weights: Annotated[
        str | None, typer.Option("--weights", help=h("cli_help.refpoint.weights"))
    ] = None,
    min_coherence: Annotated[
        float, typer.Option("--min-coherence", help=h("cli_help.refpoint.min_coherence"))
    ] = 0.0,
    mintpy_threshold: Annotated[
        float, typer.Option("--mintpy-threshold", help=h("cli_help.refpoint.mintpy_threshold"))
    ] = 0.85,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help=h("cli_help.refpoint.out"))
    ] = None,
) -> None:
    """Recommend reference-point candidates and compare with MintPy's auto rule (R-09)."""
    from wintersar.validate.refpoint import (
        DEFAULT_WEIGHTS,
        aoi_mask_from_geojson,
        compare_with_mintpy_auto,
        recommend,
    )

    command = "refpoint"
    _require(aoi, "validate.cli.aoi_not_found", command, "--aoi")
    for opt, p in (("--coherence", coherence), ("--conncomp", conncomp), ("--dem", dem)):
        _require(p, "cli.CLI-006.cause", command, opt)
    tsobj = _load_ts(ts, None, None, command)
    w: dict[str, float] | None = None
    if weights:
        w = dict(DEFAULT_WEIGHTS)
        for item in weights.split(","):
            k, _, v = item.partition("=")
            try:
                w[k.strip()] = float(v)
            except ValueError:
                allowed = ", ".join(f"{name}=<float>" for name in DEFAULT_WEIGHTS)
                raise _bad_value(command, "--weights", weights, allowed) from None
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
        # e.g. an unknown weight name; the module's message is the detail
        allowed = f"{', '.join(DEFAULT_WEIGHTS)} — {mask_text(str(exc))}"
        raise _bad_value(command, "--weights", weights, allowed) from None
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
    config: Annotated[Path, typer.Option("--config", "-c", help=h("cli_help.common.config"))],
    grid: Annotated[Path, typer.Option("--grid", help=h("cli_help.sweep.grid"))],
    out: Annotated[Path | None, typer.Option("--out", "-o", help=h("cli_help.sweep.out"))] = None,
    leveling: Annotated[
        Path | None, typer.Option("--leveling", help=h("cli_help.sweep.leveling"))
    ] = None,
    gnss: Annotated[Path | None, typer.Option("--gnss", help=h("cli_help.sweep.gnss"))] = None,
    radius: Annotated[float, typer.Option("--radius", help=h("cli_help.sweep.radius"))] = 100.0,
    set_: Annotated[list[str] | None, typer.Option("--set", help=h("cli_help.sweep.set"))] = None,
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

    command = "sweep"
    _require(grid, "validate.cli.grid_not_found", command, "--grid")
    _require(leveling, "validate.cli.csv_not_found", command, "--leveling")
    _require(gnss, "validate.cli.csv_not_found", command, "--gnss")
    try:
        cfg = load_config(config)
    except FileNotFoundError:
        finding = cli_finding(CLI_CONFIG_MISSING, path=str(config), option="--config")
        raise exit_with_findings(command, [finding]) from None
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        finding = cli_finding(
            CLI_CONFIG_INVALID, path=str(config), error=str(exc), option="--config"
        )
        raise exit_with_findings(command, [finding]) from None
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
    overrides = parse_set(set_, command)
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
    igrams: Annotated[Path, typer.Option("--igrams", help=h("cli_help.closure.igrams"))],
    unw: Annotated[Path | None, typer.Option("--unw", help=h("cli_help.closure.unw"))] = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help=h("cli_help.closure.out"))] = None,
    wrapped: Annotated[bool, typer.Option("--wrapped", help=h("cli_help.closure.wrapped"))] = False,
    top: Annotated[int, typer.Option("--top", help=h("cli_help.closure.top"))] = 10,
) -> None:
    """Loop-closure statistics: per-triplet/pixel RMS and suspicious interferograms (§12.1)."""
    from wintersar.validate.closure import (
        closure_phase,
        write_closure_maps,
        write_dashboard_json,
    )

    command = "closure"
    _require(igrams, "validate.cli.igrams_not_found", command, "--igrams")
    _require(unw, "validate.cli.unw_not_found", command, "--unw")
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
    app.command("validate", help=h("cli_help.validate.help"))(validate_cmd)
    app.command("refpoint", help=h("cli_help.refpoint.help"))(refpoint_cmd)
    app.command("sweep", help=h("cli_help.sweep.help"))(sweep_cmd)
    app.command("closure", help=h("cli_help.closure.help"))(closure_cmd)
