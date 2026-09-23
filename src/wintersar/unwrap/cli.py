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
from wintersar.util.clihelp import HelpGroup, h
from wintersar.util.clistate import state
from wintersar.util.output import (
    CLI_BAD_VALUE,
    cli_finding,
    console,
    emit_json,
    err_console,
    exit_with_findings,
    print_findings,
)
from wintersar.util.sysinfo import MachineSpec

unwrap_app = typer.Typer(
    name="unwrap",
    cls=HelpGroup,
    help=h("cli_help.unwrap.help"),
    no_args_is_help=True,
)
METHOD_CHOICES: tuple[str, ...] = ("auto", "snaphu", "tophu", "spurt", "truth", "identity")


def _machine(cores: int | None, memory_gb: float | None) -> MachineSpec:
    return sysinfo.detect().budget(
        cores="auto" if cores is None else cores,
        memory_gb="auto" if memory_gb is None else memory_gb,
    )


def _tiles_value(tiles: str | None, overlap: float, min_overlap_px: int, command: str) -> Any:
    """``--tiles ROWSxCOLS`` → UnwrapCfg.tiles mapping; a malformed value is CLI-008 (exit 2)."""
    if not tiles or tiles == "auto":
        return "auto"
    rows, _, cols = tiles.lower().partition("x")
    try:
        return {
            "rows": int(rows),
            "cols": int(cols),
            "overlap": overlap,
            "min_overlap_px": min_overlap_px,
        }
    except ValueError:
        finding = cli_finding(
            CLI_BAD_VALUE,
            option="--tiles",
            value=tiles,
            allowed="ROWSxCOLS | auto",
            command=command,
        )
        raise exit_with_findings(command, [finding]) from None


def _findings(stats: dict[str, Any]) -> list[Finding]:
    return [Finding.model_validate(f) for f in stats.get("findings", []) if isinstance(f, dict)]


def _machine_dict(machine: MachineSpec) -> dict[str, Any]:
    return {
        "cores": machine.cores,
        "memory_gb": round(machine.memory_gb, 2),
        "gpu": machine.gpu,
        "gpu_name": machine.gpu_name,
    }


@unwrap_app.command("plan", help=h("cli_help.unwrap_plan.help"))
def plan_cmd(
    shape: Annotated[
        tuple[int, int], typer.Option("--shape", help=h("cli_help.unwrap_plan.shape"))
    ],
    n: Annotated[int, typer.Option("--n", help=h("cli_help.unwrap_plan.n"))] = 1,
    memory_gb: Annotated[
        float | None, typer.Option("--memory-gb", help=h("cli_help.unwrap_plan.memory_gb"))
    ] = None,
    cores: Annotated[
        int | None, typer.Option("--cores", help=h("cli_help.unwrap_plan.cores"))
    ] = None,
    method: Annotated[
        str, typer.Option("--method", help=h("cli_help.common.method_unwrap"))
    ] = "auto",
    tiles: Annotated[
        str | None, typer.Option("--tiles", help=h("cli_help.unwrap_plan.tiles"))
    ] = None,
    overlap: Annotated[
        float, typer.Option("--overlap", help=h("cli_help.unwrap_plan.overlap"))
    ] = 0.25,
    min_overlap_px: Annotated[
        int, typer.Option("--min-overlap-px", help=h("cli_help.unwrap_plan.min_overlap_px"))
    ] = 200,
    memory_mb_per_mpixel: Annotated[
        float | None,
        typer.Option("--memory-mb-per-mpixel", help=h("cli_help.unwrap_plan.memory_mb_per_mpixel")),
    ] = None,
    nproc: Annotated[int, typer.Option("--nproc", help=h("cli_help.unwrap_plan.nproc"))] = 1,
    fringe: Annotated[
        float | None, typer.Option("--fringe", help=h("cli_help.unwrap_plan.fringe"))
    ] = None,
    available: Annotated[
        list[str] | None, typer.Option("--available", help=h("cli_help.unwrap_plan.available"))
    ] = None,
) -> None:
    """Dry-run: print the unwrap strategy for a stack of the given size (plan §5.4)."""
    from wintersar.unwrap.api import resolve_plan

    command = "unwrap plan"
    if method not in METHOD_CHOICES:
        finding = cli_finding(
            CLI_BAD_VALUE,
            option="--method",
            value=method,
            allowed=" | ".join(METHOD_CHOICES),
            command=command,
        )
        raise exit_with_findings(command, [finding])
    machine = _machine(cores, memory_gb)
    cfg_kwargs: dict[str, Any] = {
        "method": "auto" if method in ("truth", "identity") else method,
        "tiles": _tiles_value(tiles, overlap, min_overlap_px, command),
        "nproc_per_igram": nproc,
    }
    if memory_mb_per_mpixel is not None:
        cfg_kwargs["memory_mb_per_mpixel"] = memory_mb_per_mpixel
    try:
        cfg = UnwrapCfg.model_validate(cfg_kwargs)
    except ValueError as e:
        # the unwrap section of config.yaml rejected the combination (pydantic detail)
        finding = cli_finding(
            CLI_BAD_VALUE,
            option="unwrap.*",
            value=str(cfg_kwargs),
            allowed=str(e),
            command=command,
        )
        raise exit_with_findings(command, [finding]) from None
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


@unwrap_app.command("run", help=h("cli_help.unwrap_run.help"))
def run_cmd(
    igrams: Annotated[Path, typer.Argument(help=h("cli_help.unwrap_run.igrams"))],
    out: Annotated[Path, typer.Option("--out", help=h("cli_help.unwrap_run.out"))],
    method: Annotated[
        str, typer.Option("--method", help=h("cli_help.common.method_unwrap"))
    ] = "auto",
    coherence_threshold: Annotated[
        float,
        typer.Option("--coherence-threshold", help=h("cli_help.unwrap_run.coherence_threshold")),
    ] = 0.3,
    memory_gb: Annotated[
        float | None, typer.Option("--memory-gb", help=h("cli_help.unwrap_run.memory_gb"))
    ] = None,
    cores: Annotated[
        int | None, typer.Option("--cores", help=h("cli_help.unwrap_run.cores"))
    ] = None,
    tiles: Annotated[
        str | None, typer.Option("--tiles", help=h("cli_help.unwrap_run.tiles"))
    ] = None,
    overlap: Annotated[
        float, typer.Option("--overlap", help=h("cli_help.unwrap_run.overlap"))
    ] = 0.25,
    min_overlap_px: Annotated[
        int, typer.Option("--min-overlap-px", help=h("cli_help.unwrap_run.min_overlap_px"))
    ] = 200,
    nproc: Annotated[int, typer.Option("--nproc", help=h("cli_help.unwrap_run.nproc"))] = 1,
    n_parallel: Annotated[
        int | None, typer.Option("--n-parallel", help=h("cli_help.unwrap_run.n_parallel"))
    ] = None,
    cost: Annotated[str, typer.Option("--cost", help=h("cli_help.unwrap_run.cost"))] = "defo",
    init: Annotated[str, typer.Option("--init", help=h("cli_help.unwrap_run.init"))] = "mcf",
    log_dir: Annotated[
        Path | None, typer.Option("--log-dir", help=h("cli_help.unwrap_run.log_dir"))
    ] = None,
) -> None:
    """Unwrap every interferogram of a stack with the scheduled strategy."""
    from wintersar.engines.base import EngineNotAvailableError
    from wintersar.unwrap.api import STATS_FILE, UnwrapFailedError, run_unwrap

    command = "unwrap run"
    machine = _machine(cores, memory_gb)
    params: dict[str, Any] = {
        "method": method,
        "cost": cost,
        "init": init,
        "coherence_threshold": coherence_threshold,
        "tiles": _tiles_value(tiles, overlap, min_overlap_px, command),
        "nproc_per_igram": nproc,
    }
    if n_parallel is not None:
        params["_n_parallel"] = n_parallel
    artifact = Artifact(name="igrams", path=igrams, kind="npz")
    findings: list[Finding] = []
    try:
        arts = run_unwrap(artifact, params, out, log_dir or out / "logs", machine)
    # OSError: a missing/unreadable stack must still leave a --json envelope, never a traceback
    except (
        UnwrapFailedError,
        EngineNotAvailableError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
    ) as e:
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
