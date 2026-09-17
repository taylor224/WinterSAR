"""``wintersar research`` sub-commands (plan §4.5 / §5.7, R-07, R-15).

* ``research repr-phase --igram NPZ --method M --factor N --out NPZ [--index i] [--stack NPZ]
  [--pair I J] [-p k=v ...]`` — low-resolution representative phase of one interferogram.
* ``research stitch --tiles NPZ --method M --out NPZ [--lowres-ref NPZ]`` — 2π tile stitching.
* ``research synth --out NPZ [--kind igrams|tiles|slc] [--n-dates N] [--shape NY NX] ...`` —
  synthetic ground-truth data in the formats the two commands above read.
* ``research experiment YAML --out DIR [--docs-dir DIR]`` — YAML experiment → JSON + table.

Every command honours the global ``--json`` / ``--lang`` options (``wintersar.util.clistate``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, cast

import numpy as np
import typer
import yaml
from rich.table import Table

from wintersar.i18n import t
from wintersar.io.igrams import IgramStack, load_igram_stack, save_igram_stack
from wintersar.io.schemas import Finding
from wintersar.util.clistate import state
from wintersar.util.masking import mask_text
from wintersar.util.output import console, emit_json, err_console, print_findings

research_app = typer.Typer(
    name="research",
    help="Representative-phase, tile-stitching and A/B experiments (R-07, R-15).",
    no_args_is_help=True,
)


def _finding(rule_id: str, severity: str = "FAIL", **params: Any) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=f"research.{rule_id}.cause",
        fix_key=f"research.{rule_id}.fix",
        params=params,
        evidence={k: mask_text(str(v)) for k, v in params.items()},
    )


def _fail(command: str, err: Exception, code: int = 1) -> None:
    from wintersar.research.repr_phase import ResearchError

    findings = [_finding(err.rule_id, **err.params)] if isinstance(err, ResearchError) else []
    if state.json:
        emit_json(command, {"error": mask_text(str(err))}, findings, ok=False)
    else:
        err_console.print(f"[red]{mask_text(str(err))}[/]")
        if findings:
            print_findings(findings, state.lang)
    raise typer.Exit(code=code)


def _parse_params(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            msg = f"expected key=value, got {item!r}"
            raise typer.BadParameter(msg)
        out[key.strip()] = yaml.safe_load(value)
    return out


def _load_stack(path: Path) -> np.ndarray:
    """``slc`` (complex) or ``amplitude`` stack from an ``.npz`` (also accepts ``wrapped``
    + ``coherence`` from an igrams file: amplitude proxy = coherence)."""
    with np.load(path, allow_pickle=False) as z:
        for key in ("slc", "amplitude", "amp"):
            if key in z.files:
                return np.asarray(z[key])
        if "coherence" in z.files:
            return np.asarray(z["coherence"])
    msg = f"{path}: no 'slc' / 'amplitude' array"
    raise typer.BadParameter(msg)


def _pair_from_stack(st: IgramStack, index: int) -> tuple[int, int] | None:
    key = st.pairs[index]
    a, b = key.split("_")
    iso = [d.isoformat().replace("-", "") for d in st.dates]
    if a in iso and b in iso:
        return iso.index(a), iso.index(b)
    return None


# ---------------------------------------------------------------- repr-phase
@research_app.command("repr-phase")
def repr_phase_cmd(
    igram: Annotated[Path, typer.Option("--igram", help="igrams.npz (wintersar.io.igrams).")],
    out: Annotated[Path, typer.Option("--out", help="Output .npz (repr_complex, repr_phase).")],
    method: Annotated[
        str, typer.Option("--method", help="ml | coh_weighted | shp | phase_link | filtered")
    ] = "ml",
    factor: Annotated[
        int, typer.Option("--factor", help="Downsample factor (looks per axis).")
    ] = 3,
    index: Annotated[int, typer.Option("--index", help="Interferogram index in the stack.")] = 0,
    stack: Annotated[
        Path | None,
        typer.Option(
            "--stack", help="NPZ with 'slc' or 'amplitude' (n_dates, ny, nx) for shp/phase_link."
        ),
    ] = None,
    pair: Annotated[
        tuple[int, int] | None,
        typer.Option(
            "--pair", help="Date indices (i j) for phase_link (default: from the pair key)."
        ),
    ] = None,
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Extra method parameter k=v (repeatable)."),
    ] = None,
) -> None:
    """Estimate the low-resolution representative phase of one interferogram (R-07)."""
    from wintersar.research import metrics as rmetrics
    from wintersar.research.repr_phase import (
        ResearchError,
        representative_phase,
        truth_lowres_phase,
    )

    command = "research repr-phase"
    try:
        st = load_igram_stack(igram)
        if not 0 <= index < st.n_pairs:
            raise ResearchError(
                "RES-006", name="index", value=index, allowed=f"0..{st.n_pairs - 1}"
            )
        z = np.asarray(st.complex(index), dtype=np.complex128)
        coh = np.asarray(st.coherence[index], dtype=np.float64)
        mask = st.mask_for(index)
        z = np.where(mask, 0.0, z)
        coh = np.where(mask, 0.0, coh)
        kw = _parse_params(param)
        stack_arr = _load_stack(stack) if stack is not None else None
        if method == "phase_link" and pair is None and "pair" not in kw:
            inferred = _pair_from_stack(st, index)
            if inferred is not None:
                kw["pair"] = inferred
        if pair is not None:
            kw["pair"] = (int(pair[0]), int(pair[1]))
        with rmetrics.ResourceTimer() as rt:
            est = representative_phase(method, z, coh, factor, stack=stack_arr, **kw)
        data: dict[str, Any] = {
            "method": method,
            "factor": factor,
            "index": index,
            "pair": st.pairs[index],
            "shape": list(est.shape),
            "out": str(out),
            "mean_magnitude": float(np.mean(np.abs(est))),
            "wall_s": rt.result.wall_s,
            "peak_rss_mb": rt.result.peak_rss_mb,
            "params": kw,
        }
        # inside the guarded block: a truth/estimate grid mismatch must come out as a
        # finding, and the .npz must not be written when anything above failed
        if "unw_true" in st.truth:
            truth_lo = truth_lowres_phase(
                np.asarray(st.truth["unw_true"][index], dtype=np.float64), factor
            )
            data["phase_rmse_rad"] = rmetrics.phase_rmse(est, truth_lo)
            data["phase_mae_rad"] = rmetrics.phase_mae(est, truth_lo)
        out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            out,
            repr_complex=est.astype(np.complex64),
            repr_phase=np.angle(est).astype(np.float32),
            repr_magnitude=np.abs(est).astype(np.float32),
            method=np.array(method),
            factor=np.array(factor),
            pair=np.array(st.pairs[index]),
        )
    except (ResearchError, OSError, KeyError, ValueError, typer.BadParameter) as e:
        _fail(command, e)
        return
    if state.json:
        emit_json(command, data)
        return
    console.print(
        t(
            "research.cli.repr_done",
            method=method,
            factor=factor,
            ny=est.shape[0],
            nx=est.shape[1],
            path=str(out),
        )
    )
    if "pair" in kw:
        console.print(t("research.cli.repr_pair", i=kw["pair"][0], j=kw["pair"][1]))
    if "phase_rmse_rad" in data:
        console.print(
            t(
                "research.cli.repr_rmse",
                rmse=data["phase_rmse_rad"],
                magnitude=data["mean_magnitude"],
            )
        )


# ---------------------------------------------------------------- stitch
@research_app.command("stitch")
def stitch_cmd(
    tiles: Annotated[
        Path, typer.Option("--tiles", help="tiles.npz (research.stitching.save_tiles_npz).")
    ],
    out: Annotated[
        Path, typer.Option("--out", help="Output .npz (merged, offsets) + .json report.")
    ],
    method: Annotated[
        str, typer.Option("--method", help="coarse_ref | overlap_consensus")
    ] = "overlap_consensus",
    lowres_ref: Annotated[
        Path | None,
        typer.Option("--lowres-ref", help="NPZ holding the low-res unwrapped reference."),
    ] = None,
    ref_key: Annotated[
        str, typer.Option("--ref-key", help="Array key inside --lowres-ref.")
    ] = "lowres_ref",
    consensus: Annotated[
        str, typer.Option("--consensus", help="mode | median (overlap_consensus)")
    ] = "mode",
    ref_stat: Annotated[
        str, typer.Option("--ref-stat", help="mean | median (coarse_ref)")
    ] = "mean",
    merge: Annotated[str, typer.Option("--merge", help="feather | core")] = "feather",
) -> None:
    """Determine and remove the integer-2π offsets between independently unwrapped tiles."""
    from wintersar.research import metrics as rmetrics
    from wintersar.research.repr_phase import ResearchError
    from wintersar.research.stitching import load_tiles_npz, stitch

    command = "research stitch"
    try:
        bundle = load_tiles_npz(tiles)
        ref = bundle.get("lowres_ref")
        if lowres_ref is not None:
            with np.load(lowres_ref, allow_pickle=False) as z:
                key = (
                    ref_key
                    if ref_key in z.files
                    else next((k for k in ("lowres_ref", "unw", "phase") if k in z.files), None)
                )
                if key is None:
                    raise ResearchError("RES-004")
                ref = np.asarray(z[key], dtype=np.float64)
        with rmetrics.ResourceTimer() as rt:
            merged, offsets, report = stitch(
                bundle["tiles"],
                bundle["shape"],
                method,
                lowres_ref=ref,
                coh=bundle.get("coh"),
                merge=merge,  # type: ignore[arg-type]
                consensus=consensus,  # type: ignore[arg-type]
                ref_stat=ref_stat,  # type: ignore[arg-type]
            )
    except (ResearchError, OSError, KeyError, ValueError) as e:
        _fail(command, e)
        return
    findings: list[Finding] = []
    if report.get("n_inconsistent_edges", 0) > 0:
        findings.append(
            _finding(
                "RES-005",
                "WARN",
                n_inconsistent=int(report["n_inconsistent_edges"]),
                residual_max=float(report["residual_max_cycles"]),
            )
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        merged=merged.astype(np.float32),
        offsets_cycles=np.asarray(offsets, dtype=np.int64),
        method=np.array(method),
    )
    report_path = out.with_suffix(".json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    data: dict[str, Any] = {
        "method": method,
        "offsets_cycles": offsets,
        "n_tiles": len(offsets),
        "boundary_jumps": report["boundary_jumps"],
        "out": str(out),
        "report": str(report_path),
        "wall_s": rt.result.wall_s,
        "peak_rss_mb": rt.result.peak_rss_mb,
    }
    truth_offsets = bundle.get("offsets_cycles")
    if truth_offsets is not None:
        data["offsets"] = rmetrics.offsets_recovered(offsets, [int(v) for v in truth_offsets])
    if bundle.get("unw_true") is not None:
        data["unwrap_error_fraction"] = rmetrics.unwrap_error_fraction(
            merged.astype(np.float64),
            np.asarray(bundle["unw_true"], dtype=np.float64),
            bundle.get("mask"),
        )
    if state.json:
        emit_json(command, data, findings)
        return
    console.print(
        t(
            "research.cli.stitch_done",
            method=method,
            n_tiles=len(offsets),
            offsets=offsets,
            path=str(out),
        )
    )
    bj = report["boundary_jumps"]
    console.print(
        t(
            "research.cli.stitch_jumps",
            n_jump=bj["n_boundaries_with_jump"],
            n_boundaries=bj["n_boundaries"],
            n_jump_pixels=bj["n_jump_pixels"],
        )
    )
    if "n_inconsistent_edges" in report:
        console.print(
            t(
                "research.cli.stitch_residual",
                residual_max=report["residual_max_cycles"],
                n_inconsistent=report["n_inconsistent_edges"],
            )
        )
    if "offsets" in data:
        console.print(
            t(
                "research.cli.stitch_truth",
                n_wrong=data["offsets"]["n_wrong_offsets"],
                error_fraction=data.get("unwrap_error_fraction", float("nan")),
            )
        )
    print_findings(findings, state.lang)


# ---------------------------------------------------------------- synth
@research_app.command("synth")
def synth_cmd(
    out: Annotated[Path, typer.Option("--out", help="Output .npz")],
    kind: Annotated[str, typer.Option("--kind", help="igrams | tiles | slc")] = "igrams",
    n_dates: Annotated[int, typer.Option("--n-dates")] = 8,
    shape: Annotated[tuple[int, int], typer.Option("--shape", help="Rows and columns.")] = (64, 64),
    seed: Annotated[int, typer.Option("--seed")] = 0,
    atmosphere_std_rad: Annotated[float, typer.Option("--atmosphere-std-rad")] = 0.6,
    coherence_base: Annotated[float, typer.Option("--coherence-base")] = 0.8,
    looks: Annotated[int, typer.Option("--looks")] = 1,
    noise_model: Annotated[
        str,
        typer.Option(
            "--noise-model",
            help="crlb (Cramer-Rao lower bound, default) | exact (true phase distribution).",
        ),
    ] = "crlb",
    water_fraction: Annotated[float, typer.Option("--water-fraction")] = 0.0,
    rows: Annotated[int, typer.Option("--rows", help="tiles: tile rows")] = 2,
    cols: Annotated[int, typer.Option("--cols", help="tiles: tile columns")] = 2,
    overlap: Annotated[int, typer.Option("--overlap", help="tiles: overlap pixels")] = 8,
    max_offset_cycles: Annotated[int, typer.Option("--max-offset-cycles")] = 3,
    noise_std_rad: Annotated[
        float, typer.Option("--noise-std-rad", help="tiles: noise on tiles")
    ] = 0.0,
    factor: Annotated[int, typer.Option("--factor", help="tiles: low-res reference factor")] = 3,
    tau_dates: Annotated[
        float, typer.Option("--tau-dates", help="slc: coherence decay (dates)")
    ] = 4.0,
    coherence_floor: Annotated[
        float, typer.Option("--coherence-floor", help="slc: long-term coherence")
    ] = 0.1,
) -> None:
    """Write synthetic ground-truth data (igrams.npz / tiles.npz / slc.npz)."""
    from wintersar.research import synth
    from wintersar.research.repr_phase import ResearchError, truth_lowres_phase
    from wintersar.research.stitching import save_tiles_npz

    command = "research synth"
    rng = np.random.default_rng(seed)
    shp = (int(shape[0]), int(shape[1]))
    try:
        if noise_model not in ("crlb", "exact"):
            raise ResearchError(
                "RES-006", name="noise_model", value=noise_model, allowed="crlb | exact"
            )
        nm = cast("synth.NoiseModel", noise_model)
        if kind == "igrams":
            st = synth.make_stack(
                n_dates=n_dates,
                shape=shp,
                rng=rng,
                atmosphere_std_rad=atmosphere_std_rad,
                coherence_base=coherence_base,
                looks=looks,
                noise_model=nm,
                water_fraction=water_fraction,
            )
            keys = [p.key for p in st.pairs]
            stack = IgramStack(
                wrapped=np.stack([st.igrams[k].wrapped for k in keys]).astype(np.float32),
                coherence=np.stack([st.igrams[k].coherence for k in keys]).astype(np.float32),
                pairs=keys,
                dates=st.dates,
                mask=np.stack([st.igrams[k].mask for k in keys]),
                truth={
                    "unw_true": np.stack([st.igrams[k].unw_true for k in keys]).astype(np.float32),
                    "velocity_true": st.velocity_m_per_yr.astype(np.float32),
                    "displacement_true": st.displacement_m.astype(np.float32),
                },
            )
            save_igram_stack(stack, out)
            detail = t(
                "research.cli.synth_detail_igrams",
                n_dates=n_dates,
                n_pairs=len(keys),
                ny=shp[0],
                nx=shp[1],
            )
            data: dict[str, Any] = {"n_dates": n_dates, "n_pairs": len(keys), "shape": list(shp)}
        elif kind == "tiles":
            tt = synth.make_tiled_truth(
                shp,
                rows,
                cols,
                overlap,
                rng,
                max_offset_cycles=max_offset_cycles,
                noise_std_rad=noise_std_rad,
                atmosphere_std_rad=atmosphere_std_rad,
                coherence_base=coherence_base,
                looks=looks,
                noise_model=nm,
                water_fraction=water_fraction,
            )
            save_tiles_npz(
                out,
                tt.tiles,
                tt.shape,
                coh=tt.coherence,
                lowres_ref=truth_lowres_phase(tt.unw_true, factor),
                unw_true=tt.unw_true,
                mask=tt.mask,
                offsets_cycles=tt.offsets_cycles,
            )
            detail = t(
                "research.cli.synth_detail_tiles",
                rows=rows,
                cols=cols,
                overlap=overlap,
                ny=shp[0],
                nx=shp[1],
                offsets=tt.offsets_cycles,
            )
            data = {
                "rows": rows,
                "cols": cols,
                "overlap": overlap,
                "shape": list(shp),
                "offsets_cycles": tt.offsets_cycles,
            }
        elif kind == "slc":
            regions = synth.shp_regions(shp, "halves")
            amp = np.asarray([1.0, 3.0])[regions]
            sl = synth.make_slc_stack(
                n_dates,
                shp,
                rng,
                tau_dates=tau_dates,
                coherence_floor=coherence_floor,
                amplitude=amp,
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                out,
                slc=sl.slc.astype(np.complex64),
                amplitude=np.abs(sl.slc).astype(np.float32),
                phase_true=sl.phase_true.astype(np.float32),
                gamma=sl.gamma.astype(np.float32),
                regions=regions.astype(np.int16),
            )
            detail = t(
                "research.cli.synth_detail_slc",
                n_dates=n_dates,
                ny=shp[0],
                nx=shp[1],
                tau=tau_dates,
            )
            data = {"n_dates": n_dates, "shape": list(shp), "tau_dates": tau_dates}
        else:
            raise ResearchError("RES-006", name="kind", value=kind, allowed="igrams | tiles | slc")
    except (ResearchError, OSError, ValueError) as e:
        _fail(command, e)
        return
    data.update({"kind": kind, "seed": seed, "out": str(out)})
    if kind in ("igrams", "tiles"):
        data["noise_model"] = noise_model
    if state.json:
        emit_json(command, data)
        return
    console.print(t("research.cli.synth_done", kind=kind, detail=detail, path=str(out)))


# ---------------------------------------------------------------- experiment
def _resolve_experiment(spec: str) -> Path:
    from wintersar.research.experiments import bundled_experiment_dir, list_bundled
    from wintersar.research.repr_phase import ResearchError

    p = Path(spec)
    if p.exists():
        return p
    candidate = bundled_experiment_dir() / (spec if spec.endswith(".yaml") else f"{spec}.yaml")
    if candidate.exists():
        return candidate
    raise ResearchError(
        "RES-010",
        name=mask_text(spec),
        bundled=", ".join(sorted(b.stem for b in list_bundled())),
    )


@research_app.command("experiment")
def experiment_cmd(
    yaml_path: Annotated[
        str, typer.Argument(help="Experiment YAML path or bundled name (S_synth_repr_phase).")
    ],
    out: Annotated[
        Path, typer.Option("--out", help="Output directory for <name>.json / <name>.md")
    ],
    docs_dir: Annotated[
        Path | None,
        typer.Option(
            "--docs-dir", help="Also copy the table into this directory (docs/research/results)."
        ),
    ] = None,
) -> None:
    """Run a YAML-defined experiment (results JSON + Markdown table)."""
    from wintersar.research.experiments import STATUS_OK, load_experiment, run_experiment
    from wintersar.research.repr_phase import ResearchError

    command = "research experiment"
    try:
        exp = load_experiment(_resolve_experiment(yaml_path))
        result = run_experiment(exp, out, docs_dir)
    except (ResearchError, OSError, ValueError) as e:
        _fail(command, e)
        return
    findings: list[Finding] = []
    if result.status != STATUS_OK and result.missing:
        findings.append(
            _finding("RES-002", "WARN", name=result.name, missing=", ".join(result.missing))
        )
    data = result.to_dict()
    if state.json:
        emit_json(command, data, findings)
        return
    if result.status == STATUS_OK:
        console.print(
            t(
                "research.cli.experiment_done",
                name=result.name,
                n_methods=len(result.method_keys),
                n_seeds=len(result.seeds),
                json=str(result.json_path),
                md=str(result.md_path),
            )
        )
        table = Table(expand=True)
        table.add_column(t("research.cli.table_method"))
        for m in result.metrics:
            table.add_column(m)
        for mk in result.method_keys:
            cells = [mk]
            for m in result.metrics:
                s = result.summary.get(mk, {}).get(m)
                cells.append("-" if s is None else f"{s['mean']:.4g} ± {s['std']:.2g}")
            table.add_row(*cells)
        console.print(table)
    else:
        reason = t(
            f"research.experiment.skipped_reason.{result.reason}", missing=", ".join(result.missing)
        )
        console.print(t("research.cli.experiment_skipped", name=result.name, reason=reason))
        if result.md_path:
            console.print(t("cli.wrote", path=str(result.md_path)))
    if result.docs_md_path:
        console.print(t("research.cli.experiment_docs", path=str(result.docs_md_path)))
    print_findings(findings, state.lang)


@research_app.command("experiments")
def experiments_cmd() -> None:
    """List the bundled experiment definitions."""
    from wintersar.research.experiments import list_bundled, load_experiment

    rows: list[dict[str, Any]] = []
    for p in list_bundled():
        exp = load_experiment(p)
        rows.append(
            {
                "name": exp.name,
                "kind": exp.kind,
                "requires": exp.requires,
                "n_methods": len(exp.methods),
                "seeds": exp.seeds,
                "path": str(p),
            }
        )
    if state.json:
        emit_json("research experiments", {"experiments": rows})
        return
    console.print(f"[bold]{t('research.cli.experiment_list_title')}[/]")
    table = Table(expand=True)
    for col in ("name", "kind", "requires", "n_methods", "seeds"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["name"]),
            str(r["kind"]),
            ", ".join(str(x) for x in r["requires"]) or "-",
            str(r["n_methods"]),
            str(r["seeds"]),
        )
    console.print(table)


def register(app: typer.Typer) -> None:
    app.add_typer(research_app, name="research")
