"""``wintersar plan`` / ``wintersar run`` / ``wintersar cache {ls,gc}`` (plan §4.5).

Every command honours the global ``--json`` / ``--lang`` state
(:data:`wintersar.util.clistate.state`). On a failed run the findings are printed
cause -> fix and the process exits with status 1; a usage error (missing config, unknown
stage, bad ``--set``) exits 2 through :func:`usage_error`, which still emits the JSON
envelope so that machine callers never see an empty stdout.

Command *help* strings are plain English like every other module CLI: ``register()`` runs at
import time, before ``--lang`` is parsed, so ``t()`` would freeze them in the default
language and print Korean rows inside ``wintersar --lang en --help``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.table import Table

from wintersar.i18n import t
from wintersar.io.schemas import Finding, Plan, Resources, StageRecord
from wintersar.pipeline import api, cache
from wintersar.pipeline.config import Config, load_config
from wintersar.pipeline.stages import STAGE_ORDER
from wintersar.util.clistate import state
from wintersar.util.masking import mask_text
from wintersar.util.output import console, emit_json, err_console, print_findings

USAGE_RULE = "PIPELINE-014"

cache_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and prune the work-directory cache (work/<stage>/<hash>).",
)

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="config.yaml (plan §4.4)")]
UntilOpt = Annotated[str | None, typer.Option("--until", help="Stop after this stage.")]
FromOpt = Annotated[
    str | None,
    typer.Option("--from", help="Start at this stage; upstream stages come from the cache."),
]
ForceOpt = Annotated[
    list[str] | None,
    typer.Option("--force", help="Re-run this stage and everything downstream (repeatable)."),
]
SetOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--set",
        help="Override a stage parameter: stage.key=value (YAML scalar; dotted keys nest).",
    ),
]


# ---------------------------------------------------------------------- helpers


def usage_error(command: str, detail: str, code: int = 2) -> typer.Exit:
    """Bad input/usage: report ``detail`` and exit with ``code`` (2 by default).

    Under ``--json`` the envelope is emitted as well — a machine caller that only gets an
    exit code and a line on stderr cannot tell a usage error from a crash.
    """
    if state.json:
        emit_json(
            command,
            None,
            [
                Finding(
                    rule_id=USAGE_RULE,
                    severity="FAIL",
                    message_key="pipeline.PIPELINE-014.cause",
                    fix_key="pipeline.PIPELINE-014.fix",
                    params={"command": command, "detail": detail},
                    evidence={"detail": detail},
                    scope=command,
                )
            ],
            ok=False,
        )
    else:
        err_console.print(f"[red]{detail}[/]")
    return typer.Exit(code=code)


def _load(config: Path, command: str = "plan") -> Config:
    try:
        return load_config(config)
    except FileNotFoundError:
        detail = t("cli.config_not_found", path=mask_text(str(config)))
        raise usage_error(command, detail) from None
    except (ValidationError, yaml.YAMLError, ValueError) as exc:
        detail = t("cli.invalid_config", error=mask_text(str(exc)))
        raise usage_error(command, detail) from None


def _check_stage(name: str | None, command: str = "plan") -> None:
    if name is not None and name not in STAGE_ORDER:
        detail = t("pipeline.cli.unknown_stage", stage=name, stages=", ".join(STAGE_ORDER))
        raise usage_error(command, detail)


def parse_set(values: list[str] | None, command: str = "plan") -> dict[str, dict[str, Any]]:
    """``["unwrap.coherence_threshold=0.4", "interferogram.shape=[24,24]"]`` -> overrides."""
    overrides: dict[str, dict[str, Any]] = {}
    for raw in values or []:
        key, sep, value = raw.partition("=")
        parts = key.strip().split(".")
        if not sep or len(parts) < 2 or not all(parts):
            raise usage_error(command, t("pipeline.cli.bad_set", value=raw))
        stage, *path = parts
        _check_stage(stage, command)
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            parsed = value
        target = overrides.setdefault(stage, {})
        for p in path[:-1]:
            nxt = target.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                target[p] = nxt
            target = nxt
        target[path[-1]] = parsed
    return overrides


def _fmt(value: float | None, unit: str, digits: int = 1) -> str:
    return t("common.unknown") if value is None else f"{value:.{digits}f} {unit}"


def _resources_line(res: Resources) -> str:
    wall = t("common.unknown") if res.wall_time_s is None else f"{res.wall_time_s / 60:.1f} min"
    return t(
        "pipeline.cli.resources",
        wall=wall,
        mem=_fmt(res.peak_rss_gb, "GB"),
        disk=_fmt(res.disk_gb, "GB"),
        credits=t("common.none") if res.credits is None else f"{res.credits:.0f}",
    )


def _status_label(rec: StageRecord, in_plan: bool) -> str:
    if rec.status == "skipped":
        return f"[dim]{t('pipeline.cli.status_skipped')}[/]"
    if rec.extra.get("cache_hit"):
        return f"[green]{t('pipeline.cli.status_cached')}[/]"
    if rec.status == "failed":
        label = "status_blocked" if rec.extra.get("provisional") else "status_failed"
        return f"[bold red]{t('pipeline.cli.' + label)}[/]"
    if rec.status == "ok":
        return f"[cyan]{t('pipeline.cli.status_ran')}[/]"
    if in_plan:
        return f"[yellow]{t('pipeline.cli.status_to_run')}[/]"
    return t("pipeline.cli.status_pending")


def _stage_table(records: list[StageRecord], in_plan: bool) -> Table:
    table = Table(expand=True)
    table.add_column(t("pipeline.cli.col_stage"))
    table.add_column(t("pipeline.cli.col_engine"))
    table.add_column(t("pipeline.cli.col_version"))
    table.add_column(t("pipeline.cli.col_status"))
    table.add_column(t("pipeline.cli.col_hash"))
    table.add_column(t("pipeline.cli.col_wall"), justify="right")
    for rec in records:
        wall = rec.resources.wall_time_s
        table.add_row(
            rec.stage,
            rec.engine or "-",
            rec.engine_version or "-",
            _status_label(rec, in_plan),
            "-" if rec.extra.get("provisional") or rec.status == "skipped" else rec.node_hash,
            "-" if wall is None else f"{wall:.1f}",
        )
    return table


def _print_plan(cfg: Config, plan: Plan) -> None:
    console.print(
        f"[bold]{t('pipeline.cli.plan_title', project=cfg.project.name, workdir=mask_text(str(cfg.workdir)))}[/]"
    )
    console.print(_stage_table(plan.stages, in_plan=True))
    n_skipped = sum(1 for r in plan.stages if r.status == "skipped")
    console.print(
        t(
            "pipeline.cli.summary",
            n_run=len(plan.to_run),
            n_cached=len(plan.cached),
            n_skipped=n_skipped,
        )
    )
    console.print(_resources_line(plan.resources))
    if plan.findings:
        print_findings(plan.findings)


# ---------------------------------------------------------------------- commands


def plan_cmd(
    config: ConfigOpt = Path("config.yaml"),
    until: UntilOpt = None,
    from_stage: FromOpt = None,
    force: ForceOpt = None,
    set_: SetOpt = None,
) -> None:
    cfg = _load(config, "plan")
    _check_stage(until, "plan")
    _check_stage(from_stage, "plan")
    for s in force or []:
        _check_stage(s, "plan")
    overrides = parse_set(set_, "plan")
    plan = api.plan(cfg, until=until, from_stage=from_stage, force=force, param_overrides=overrides)
    ok = not any(f.is_fail for f in plan.findings)
    if state.json:
        emit_json("plan", plan.model_dump(mode="json"), plan.findings, ok=ok)
    else:
        _print_plan(cfg, plan)
    if not ok:
        raise typer.Exit(code=1)


def run_cmd(
    config: ConfigOpt = Path("config.yaml"),
    until: UntilOpt = None,
    from_stage: FromOpt = None,
    force: ForceOpt = None,
    set_: SetOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only print the plan (same as `plan`).")
    ] = False,
) -> None:
    cfg = _load(config, "run")
    _check_stage(until, "run")
    _check_stage(from_stage, "run")
    for s in force or []:
        _check_stage(s, "run")
    overrides = parse_set(set_, "run")
    result = api.run(
        cfg,
        until=until,
        from_stage=from_stage,
        force=force,
        param_overrides=overrides,
        dry_run=dry_run,
    )
    if state.json:
        emit_json("run", result.to_dict(), result.findings, ok=result.ok)
    else:
        console.print(
            f"[bold]{t('pipeline.cli.run_title', project=cfg.project.name, workdir=mask_text(str(cfg.workdir)))}[/]"
        )
        console.print(_stage_table(result.records, in_plan=result.dry_run))
        if result.dry_run:
            console.print(t("pipeline.cli.dry_run"))
            console.print(_resources_line(result.plan.resources))
        elif result.ok:
            wall = sum(r.resources.wall_time_s or 0.0 for r in result.ran)
            console.print(
                f"[green]{t('pipeline.cli.run_ok', n_ran=len(result.ran), n_cached=len(result.cached), wall=wall)}[/]"
            )
        else:
            failed = next((r for r in result.records if r.status == "failed"), None)
            log_dir = mask_text(str(failed.log_path)) if failed and failed.log_path else "-"
            console.print(
                f"[bold red]{t('pipeline.cli.run_failed', stage=result.failed_stage or '?', log_dir=log_dir)}[/]"
            )
        if result.findings:
            print_findings(result.findings)
    if not result.ok:
        raise typer.Exit(code=1)


def _workdir(config: Path | None, workdir: Path | None, command: str = "cache ls") -> Path:
    if workdir is not None:
        return workdir
    return _load(config or Path("config.yaml"), command).workdir


WorkdirOpt = Annotated[
    Path | None, typer.Option("--workdir", help="Work directory (default: from --config).")
]
ConfigOptional = Annotated[Path | None, typer.Option("--config", "-c", help="config.yaml")]


def cache_ls(
    config: ConfigOptional = None,
    workdir: WorkdirOpt = None,
    stage: Annotated[str | None, typer.Option("--stage", help="Only this stage.")] = None,
) -> None:
    _check_stage(stage, "cache ls")
    wd = _workdir(config, workdir, "cache ls")
    entries = cache.list_records(wd, stage)
    if state.json:
        emit_json(
            "cache ls",
            {
                "workdir": str(wd),
                "entries": [
                    {
                        "stage": e.stage,
                        "node_hash": e.node_hash,
                        # an orphan (crash before the first manifest) has no record at all
                        "status": e.status,
                        "orphan": e.orphan,
                        "engine": e.record.engine if e.record else None,
                        "engine_version": e.record.engine_version if e.record else None,
                        "finished_at": e.finished_at.isoformat() if e.finished_at else None,
                        "size_bytes": e.size_bytes,
                        "path": str(e.path),
                    }
                    for e in entries
                ],
                "size_by_stage": cache.cache_size(wd),
            },
        )
        return
    if not entries:
        console.print(t("pipeline.cli.cache_empty", workdir=mask_text(str(wd))))
        return
    table = Table(expand=True)
    table.add_column(t("pipeline.cli.col_stage"))
    table.add_column(t("pipeline.cli.col_hash"))
    table.add_column(t("pipeline.cli.col_status"))
    table.add_column(t("pipeline.cli.col_engine"))
    table.add_column(t("pipeline.cli.col_version"))
    table.add_column(t("pipeline.cli.col_finished"))
    table.add_column(t("pipeline.cli.col_size"), justify="right")
    for e in entries:
        table.add_row(
            e.stage,
            e.node_hash,
            e.status,
            (e.record.engine if e.record else None) or "-",
            (e.record.engine_version if e.record else None) or "-",
            e.finished_at.strftime("%Y-%m-%d %H:%M") if e.finished_at else "-",
            cache.human_size(e.size_bytes),
        )
    console.print(table)
    console.print(
        t(
            "pipeline.cli.cache_total",
            n=len(entries),
            size=cache.human_size(sum(e.size_bytes for e in entries)),
        )
    )


def cache_gc(
    config: ConfigOptional = None,
    workdir: WorkdirOpt = None,
    keep: Annotated[int, typer.Option("--keep", min=0, help="Entries kept per stage.")] = 3,
    max_size: Annotated[
        float | None,
        typer.Option(
            "--max-size",
            min=0.0,
            help="Cache budget in GB: evict the oldest survivors until the work directory "
            "fits (PERF-03). Default: no size limit.",
        ),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only report.")] = False,
) -> None:
    wd = _workdir(config, workdir, "cache gc")
    max_bytes = None if max_size is None else int(max_size * 1e9)
    report = cache.gc(wd, keep_latest=keep, dry_run=dry_run, max_bytes=max_bytes)
    if state.json:
        emit_json(
            "cache gc",
            {
                "workdir": str(wd),
                "dry_run": dry_run,
                "keep": keep,
                "max_size_gb": max_size,
                "freed_bytes": report.freed_bytes,
                "removed": [{"stage": e.stage, "node_hash": e.node_hash} for e in report.removed],
                "kept": [{"stage": e.stage, "node_hash": e.node_hash} for e in report.kept],
            },
        )
        return
    for e in report.removed:
        console.print(f"  - {e.stage}/{e.node_hash} ({cache.human_size(e.size_bytes)})")
    console.print(
        t(
            "pipeline.cli.gc_summary",
            n_removed=len(report.removed),
            freed=cache.human_size(report.freed_bytes),
            n_kept=len(report.kept),
            dry=t("pipeline.cli.gc_dry_suffix") if dry_run else "",
        )
    )


def register(app: typer.Typer) -> None:
    app.command(
        "plan",
        help="Build the DAG and print which stages are cached / will run plus estimated "
        "resources (no execution).",
    )(plan_cmd)
    app.command(
        "run",
        help="Run the pipeline. Cached stages are skipped; only changed stages and their "
        "downstream re-run (PERF-03).",
    )(run_cmd)
    cache_app.command("ls", help="List cached stage results.")(cache_ls)
    cache_app.command(
        "gc",
        help="Keep the N most recent entries per stage (and an optional --max-size budget) "
        "and delete the rest.",
    )(cache_gc)
    app.add_typer(cache_app, name="cache")
