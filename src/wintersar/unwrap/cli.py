"""``wintersar unwrap`` sub-commands (plan §4.5 / §5.4, R-06, PERF-04).

* ``wintersar unwrap plan --shape NY NX --n N [--memory-gb G] [--cores C]`` — dry-run:
  prints the :class:`~wintersar.unwrap.scheduler.UnwrapPlan` and its reasons.
* ``wintersar unwrap run IGRAMS.npz --out DIR --method truth|identity|snaphu|tophu`` —
  runs :func:`~wintersar.unwrap.api.run_unwrap`.

Both honour the global ``--json`` / ``--lang`` options (``wintersar.util.clistate.state``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from wintersar.i18n import t
from wintersar.io.schemas import Artifact, Finding
from wintersar.pipeline.config import UnwrapCfg
from wintersar.util import sysinfo
from wintersar.util.clistate import state
from wintersar.util.output import console, emit_json, err_console, print_findings
from wintersar.util.sysinfo import MachineSpec

unwrap_app = typer.Typer(
    name="unwrap",
    help="Phase-unwrapping scheduler: memory model, tiling, parallelism (R-06, PERF-04).",
    no_args_is_help=True,
)


def _machine(cores: int | None, memory_gb: float | None) -> MachineSpec:
    return sysinfo.detect().budget(
        cores="auto" if cores is None else cores,
        memory_gb="auto" if memory_gb is None else memory_gb,
    )


def _tiles_value(tiles: str | None, overlap: float, min_overlap_px: int) -> Any:
    if not tiles or tiles == "auto":
        return "auto"
    rows, _, cols = tiles.lower().replace("x", "x").partition("x")
    return {
        "rows": int(rows),
        "cols": int(cols),
        "overlap": overlap,
        "min_overlap_px": min_overlap_px,
    }


def _findings(stats: dict[str, Any]) -> list[Finding]:
    return [Finding.model_validate(f) for f in stats.get("findings", []) if isinstance(f, dict)]


def _machine_dict(machine: MachineSpec) -> dict[str, Any]:
    return {
        "cores": machine.cores,
        "memory_gb": round(machine.memory_gb, 2),
        "gpu": machine.gpu,
        "gpu_name": machine.gpu_name,
    }


@unwrap_app.command("plan")
def plan_cmd(
    shape: Annotated[
        tuple[int, int], typer.Option("--shape", help="Rows and columns of one interferogram.")
    ],
    n: Annotated[int, typer.Option("--n", help="Number of interferograms in the stack.")] = 1,
    memory_gb: Annotated[
        float | None, typer.Option("--memory-gb", help="Memory budget (default: detected x 0.8).")
    ] = None,
    cores: Annotated[
        int | None, typer.Option("--cores", help="CPU cores (default: detected).")
    ] = None,
    method: Annotated[
        str, typer.Option("--method", help="auto | snaphu | tophu | spurt | truth | identity")
    ] = "auto",
    tiles: Annotated[
        str | None, typer.Option("--tiles", help="Explicit ROWSxCOLS (default: auto).")
    ] = None,
    overlap: Annotated[
        float, typer.Option("--overlap", help="Overlap fraction for --tiles.")
    ] = 0.25,
    min_overlap_px: Annotated[int, typer.Option("--min-overlap-px")] = 200,
    memory_mb_per_mpixel: Annotated[
        float | None, typer.Option("--memory-mb-per-mpixel", help="Memory model constant c.")
    ] = None,
    nproc: Annotated[int, typer.Option("--nproc", help="Tile processes per interferogram.")] = 1,
    fringe: Annotated[
        float | None, typer.Option("--fringe", help="Fringe density 0-1 (skip measurement).")
    ] = None,
    available: Annotated[
        list[str] | None,
        typer.Option("--available", help="Assume these backends are installed (repeatable)."),
    ] = None,
) -> None:
    """Dry-run: print the unwrap strategy for a stack of the given size (plan §5.4)."""
    from wintersar.unwrap.api import resolve_plan

    machine = _machine(cores, memory_gb)
    cfg_kwargs: dict[str, Any] = {
        "method": "auto" if method in ("truth", "identity") else method,
        "tiles": _tiles_value(tiles, overlap, min_overlap_px),
        "nproc_per_igram": nproc,
    }
    if memory_mb_per_mpixel is not None:
        cfg_kwargs["memory_mb_per_mpixel"] = memory_mb_per_mpixel
    try:
        cfg = UnwrapCfg.model_validate(cfg_kwargs)
    except ValueError as e:
        err_console.print(f"[red]{t('cli.invalid_config', error=str(e))}[/]")
        raise typer.Exit(code=2) from e
    plan = resolve_plan(
        (shape[0], shape[1]),
        n,
        machine,
        cfg,
        requested_method=method,
        fringe=fringe,
        available=list(available) if available else None,
    )
    data = {
        "plan": plan.to_dict(),
        "reasons": plan.explain(state.lang),
        "machine": _machine_dict(machine),
    }
    if state.json:
        emit_json("unwrap plan", data)
        return
    console.print(f"[bold]{t('unwrap.plan.title')}[/]")
    console.print(t("unwrap.plan.machine", cores=machine.cores, memory_gb=machine.memory_gb))
    table = Table(show_header=False, expand=False)
    table.add_column("k")
    table.add_column("v")
    table.add_row(t("unwrap.plan.method"), plan.method)
    table.add_row(t("unwrap.plan.tiles"), f"{plan.rows}x{plan.cols}")
    table.add_row(t("unwrap.plan.overlap"), str(plan.overlap_px))
    table.add_row(t("unwrap.plan.tile_shape"), f"{plan.tile_shape[0]}x{plan.tile_shape[1]}")
    table.add_row(t("unwrap.plan.n_parallel"), str(plan.n_parallel))
    table.add_row(t("unwrap.plan.nproc_per_igram"), str(plan.nproc_per_igram))
    table.add_row(t("unwrap.plan.single_tile_mb"), f"{plan.single_tile_mb:.0f}")
    table.add_row(t("unwrap.plan.est_mb"), f"{plan.est_mb_per_igram:.0f}")
    table.add_row(t("unwrap.plan.budget"), f"{plan.budget_mb:.0f}")
    table.add_row(t("unwrap.plan.fringe"), "-" if plan.fringe is None else f"{plan.fringe:.2f}")
    console.print(table)
    console.print(f"[bold]{t('unwrap.plan.reasons')}[/]")
    for line in plan.explain(state.lang):
        console.print(f"  • {line}")


@unwrap_app.command("run")
def run_cmd(
    igrams: Annotated[Path, typer.Argument(help="igrams.npz (wintersar.io.igrams format).")],
    out: Annotated[Path, typer.Option("--out", help="Output directory (unw.npz, stats.json).")],
    method: Annotated[
        str, typer.Option("--method", help="auto | snaphu | tophu | spurt | truth | identity")
    ] = "auto",
    coherence_threshold: Annotated[
        float, typer.Option("--coherence-threshold", help="Mask pixels below this coherence.")
    ] = 0.3,
    memory_gb: Annotated[float | None, typer.Option("--memory-gb")] = None,
    cores: Annotated[int | None, typer.Option("--cores")] = None,
    tiles: Annotated[str | None, typer.Option("--tiles", help="Explicit ROWSxCOLS.")] = None,
    overlap: Annotated[float, typer.Option("--overlap")] = 0.25,
    min_overlap_px: Annotated[int, typer.Option("--min-overlap-px")] = 200,
    nproc: Annotated[int, typer.Option("--nproc")] = 1,
    n_parallel: Annotated[
        int | None, typer.Option("--n-parallel", help="Override the planned parallelism.")
    ] = None,
    cost: Annotated[str, typer.Option("--cost", help="defo | smooth | topo")] = "defo",
    init: Annotated[str, typer.Option("--init", help="mst | mcf")] = "mcf",
    log_dir: Annotated[Path | None, typer.Option("--log-dir")] = None,
) -> None:
    """Unwrap every interferogram of a stack with the scheduled strategy."""
    from wintersar.engines.base import EngineNotAvailableError
    from wintersar.unwrap.api import STATS_FILE, UnwrapFailedError, run_unwrap

    machine = _machine(cores, memory_gb)
    params: dict[str, Any] = {
        "method": method,
        "cost": cost,
        "init": init,
        "coherence_threshold": coherence_threshold,
        "tiles": _tiles_value(tiles, overlap, min_overlap_px),
        "nproc_per_igram": nproc,
    }
    if n_parallel is not None:
        params["_n_parallel"] = n_parallel
    artifact = Artifact(name="igrams", path=igrams, kind="npz")
    findings: list[Finding] = []
    try:
        arts = run_unwrap(artifact, params, out, log_dir or out / "logs", machine)
    except (UnwrapFailedError, EngineNotAvailableError, KeyError, TypeError, ValueError) as e:
        stats_path = out / STATS_FILE
        if stats_path.exists():
            raw = json.loads(stats_path.read_text(encoding="utf-8"))
            findings = _findings(raw)
        if state.json:
            emit_json("unwrap run", {"error": str(e), "stats": str(stats_path)}, findings, ok=False)
        else:
            err_console.print(f"[red]{e}[/]")
            print_findings(findings, state.lang)
        raise typer.Exit(code=1) from e
    stats = json.loads(arts["unwrap_stats"].path.read_text(encoding="utf-8"))
    findings = _findings(stats)
    summary = {
        "method": stats["method"],
        "n_pairs": stats["n_pairs"],
        "wall_time_s": stats["wall_time_s"],
        "peak_rss_mb": stats["peak_rss_mb"],
        "n_parallel": stats["n_parallel"],
        "executor": stats["executor"],
        "masked_fraction": stats["masked_fraction"],
        "boundary_jumps": stats["boundary_jumps"],
        "conncomp_counts": stats["conncomp_counts"],
        "plan": stats["plan"],
    }
    if state.json:
        emit_json("unwrap run", {"artifacts": arts.paths(), "stats": summary}, findings)
        return
    counts = list(stats["conncomp_counts"].values())
    median = sorted(counts)[len(counts) // 2] if counts else 0
    console.print(
        t(
            "unwrap.run.done",
            n_pairs=stats["n_pairs"],
            wall_time_s=stats["wall_time_s"],
            peak_rss_mb=stats["peak_rss_mb"],
        )
    )
    console.print(
        t("unwrap.run.conncomp", median_conncomp=median, masked_fraction=stats["masked_fraction"])
    )
    bj = stats["boundary_jumps"]
    if bj["n_boundaries"]:
        console.print(
            t(
                "unwrap.run.jumps",
                n_jump=bj["n_boundaries_with_jump"],
                n_boundaries=bj["n_boundaries"],
            )
        )
    console.print(
        t("unwrap.run.outputs", unw=str(arts["unw"].path), stats=str(arts["unwrap_stats"].path))
    )
    print_findings(findings, state.lang)


def register(app: typer.Typer) -> None:
    app.add_typer(unwrap_app, name="unwrap")
