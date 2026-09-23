"""``wintersar diagnose PATH [--engine ...] [--out report.md]`` (plan §4.5, §5.5)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from wintersar.diagnose.api import attach_retry_hint, diagnose_logs, iter_log_files
from wintersar.diagnose.kb_loader import KB_ENGINES, load_kb
from wintersar.diagnose.report import findings_report
from wintersar.i18n import t
from wintersar.util.clihelp import h
from wintersar.util.clistate import state
from wintersar.util.masking import mask_text
from wintersar.util.output import (
    CLI_BAD_VALUE,
    CLI_INPUT_MISSING,
    CLI_USAGE,
    cli_finding,
    console,
    emit_json,
    exit_with_findings,
    print_findings,
)


def register(app: typer.Typer) -> None:
    @app.command("diagnose", help=h("cli_help.diagnose.help"))
    def diagnose_cmd(
        path: Annotated[
            Path | None,
            typer.Argument(help=h("cli_help.diagnose.path"), show_default=False),
        ] = None,
        engine: Annotated[
            str | None,
            typer.Option(
                "--engine",
                "-e",
                help=h("cli_help.diagnose.engine", engines=" | ".join(KB_ENGINES)),
            ),
        ] = None,
        out: Annotated[
            Path | None, typer.Option("--out", "-o", help=h("cli_help.diagnose.out"))
        ] = None,
        assume_failed: Annotated[
            bool, typer.Option("--assume-failed", help=h("cli_help.diagnose.assume_failed"))
        ] = False,
        list_kb: Annotated[
            bool, typer.Option("--list-kb", help=h("cli_help.diagnose.list_kb"))
        ] = False,
    ) -> None:
        """Explain failures in engine logs: KB match -> cause -> fix (R-02, R-14)."""
        command = "diagnose"
        if list_kb:
            _list_kb()
            return
        if path is None:
            finding = cli_finding(
                CLI_USAGE,
                message_key="diagnose.cli.path_required",
                command=command,
                detail="PATH",
            )
            raise exit_with_findings(command, [finding])
        if engine is not None and engine.lower() not in KB_ENGINES:
            finding = cli_finding(
                CLI_BAD_VALUE,
                message_key="diagnose.cli.unknown_engine",
                engine=engine,
                engines=", ".join(KB_ENGINES),
                option="--engine",
                value=engine,
                allowed=" | ".join(KB_ENGINES),
                command=command,
            )
            raise exit_with_findings(command, [finding])
        if not path.exists():
            finding = cli_finding(
                CLI_INPUT_MISSING,
                message_key="diagnose.cli.path_not_found",
                path=str(path),
                option="PATH",
            )
            raise exit_with_findings(command, [finding])
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
