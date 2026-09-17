"""``wintersar diagnose PATH [--engine ...] [--out report.md]`` (plan §4.5, §5.5)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from wintersar.diagnose.api import attach_retry_hint, diagnose_logs, iter_log_files
from wintersar.diagnose.kb_loader import KB_ENGINES, load_kb
from wintersar.diagnose.report import findings_report
from wintersar.i18n import t
from wintersar.util.clistate import state
from wintersar.util.masking import mask_text
from wintersar.util.output import console, emit_json, err_console, print_findings


def register(app: typer.Typer) -> None:
    @app.command("diagnose")
    def diagnose_cmd(
        path: Annotated[
            Path | None,
            typer.Argument(
                help=(
                    "Log file or directory (e.g. work/ or work/<stage>/<hash>/logs). "
                    "Omit with --list-kb. "
                    "Scanning a directory skips wintersar's own records "
                    "(manifest.json, runs/*.json) so each failure is reported once; "
                    "name such a file explicitly to diagnose it anyway."
                ),
                show_default=False,
            ),
        ] = None,
        engine: Annotated[
            str | None,
            typer.Option(
                "--engine",
                "-e",
                help="Try this engine's KB first: " + " | ".join(KB_ENGINES),
            ),
        ] = None,
        out: Annotated[
            Path | None,
            typer.Option("--out", "-o", help="Write a Markdown report to this path."),
        ] = None,
        assume_failed: Annotated[
            bool,
            typer.Option(
                "--assume-failed",
                help="Emit KB-UNKNOWN even when no error marker is found (the stage failed).",
            ),
        ] = False,
        list_kb: Annotated[
            bool, typer.Option("--list-kb", help="List the knowledge-base entries and exit.")
        ] = False,
    ) -> None:
        """Explain failures in engine logs: KB match -> cause -> fix (R-02, R-14)."""
        if list_kb:
            _list_kb()
            return
        if path is None:
            err_console.print(f"[red]{t('diagnose.cli.path_required')}[/]")
            raise typer.Exit(code=2)
        if engine is not None and engine.lower() not in KB_ENGINES:
            err_console.print(
                f"[red]{t('diagnose.cli.unknown_engine', engine=engine, engines=', '.join(KB_ENGINES))}[/]"
            )
            raise typer.Exit(code=2)
        if not path.exists():
            err_console.print(
                f"[red]{t('diagnose.cli.path_not_found', path=mask_text(str(path)))}[/]"
            )
            raise typer.Exit(code=2)
        files = iter_log_files(path)
        findings = diagnose_logs(path, engine, assume_failed=assume_failed)
        hint = attach_retry_hint(findings)
        report_path: str | None = None
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                findings_report(findings, state.lang, source=str(path), retry_hint=hint),
                encoding="utf-8",
            )
            report_path = str(out)
        data = {
            "path": mask_text(str(path)),
            "engine": engine,
            "n_files": len(files),
            "files": [mask_text(str(f)) for f in files[:200]],
            "retry_hint": hint,
            "report_path": mask_text(report_path) if report_path else None,
        }
        if state.json:
            emit_json("diagnose", data, findings, ok=not any(f.is_fail for f in findings))
            return
        console.print(f"[bold]{t('diagnose.cli.title')}[/] — {mask_text(str(path))}")
        if not files:
            console.print(t("diagnose.cli.no_logs", path=mask_text(str(path))))
            return
        console.print(
            t("diagnose.cli.files_scanned", n=len(files), engine=engine or t("common.unknown"))
        )
        print_findings(findings)
        if hint:
            console.print(
                t(
                    "diagnose.cli.retry_hint",
                    rule_id=hint["rule_id"],
                    stage=hint["stage"],
                    action=hint["action"],
                    params=hint["params"],
                )
            )
        if report_path:
            console.print(t("cli.wrote", path=mask_text(report_path)))


def _list_kb() -> None:
    entries = load_kb()
    rows = [
        {
            "id": e.id,
            "engine": e.engine,
            "stage": e.stage,
            "severity": e.severity,
            "pattern_verified": e.pattern_verified,
            "retry_hint": e.retry_hint.action if e.retry_hint else None,
        }
        for e in entries
    ]
    if state.json:
        emit_json("diagnose --list-kb", {"n": len(rows), "entries": rows})
        return
    from rich.table import Table

    table = Table(title=t("diagnose.cli.kb_title", n=len(rows)), expand=True)
    for col in ("ID", "engine", "stage", "severity", "verified", "retry"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["id"]),
            str(r["engine"]),
            str(r["stage"] or "-"),
            str(r["severity"]),
            t("common.yes") if r["pattern_verified"] else t("common.no"),
            str(r["retry_hint"] or "-"),
        )
    console.print(table)
