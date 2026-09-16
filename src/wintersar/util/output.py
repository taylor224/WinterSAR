"""Console / JSON output helpers shared by every CLI sub-command.

Every command supports ``--json`` (plan §4.5): the QGIS plugin parses that output, so the
JSON envelope is stable: ``{"ok": bool, "command": str, "data": ..., "findings": [...]}``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from wintersar.i18n import t
from wintersar.io.schemas import Finding, sort_findings
from wintersar.util.masking import mask_mapping

console = Console(stderr=False)
err_console = Console(stderr=True)


def _default(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, set | frozenset):
        return sorted(o)
    return str(o)


def to_jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=_default, ensure_ascii=False))


def emit_json(
    command: str,
    data: Any = None,
    findings: list[Finding] | None = None,
    ok: bool = True,
    stream: Any = None,
) -> None:
    payload = {
        "ok": ok,
        "command": command,
        "data": mask_mapping(to_jsonable(data)),
        "findings": mask_mapping(to_jsonable(sort_findings(findings or []))),
    }
    (stream or sys.stdout).write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def render_finding(f: Finding, lang: str | None = None) -> tuple[str, str, str, str]:
    """(severity label, rule id, cause text, fix text) for tables and reports."""
    sev = t(f"common.severity.{f.severity}", lang)
    cause = t(f.message_key, lang, **f.params)
    fix = t(f.fix_key, lang, **f.params) if f.fix_key else ""
    return sev, f.rule_id, cause, fix


def print_findings(
    findings: list[Finding], lang: str | None = None, title: str | None = None
) -> None:
    if not findings:
        console.print(t("cli.no_findings", lang))
        return
    table = Table(title=title, show_lines=False, expand=True)
    table.add_column("", width=6)
    table.add_column("ID", width=18)
    table.add_column(t("common.cause", lang), ratio=3)
    table.add_column(t("common.fix", lang), ratio=3)
    colors = {"FAIL": "bold red", "WARN": "yellow", "INFO": "cyan"}
    for f in sort_findings(findings):
        sev, rid, cause, fix = render_finding(f, lang)
        scope = f" [{f.scope}]" if f.scope else ""
        table.add_row(f"[{colors[f.severity]}]{sev}[/]", rid + scope, cause, fix)
    console.print(table)
    n = {s: sum(1 for f in findings if f.severity == s) for s in ("FAIL", "WARN", "INFO")}
    console.print(
        t("cli.findings_summary", lang, n_fail=n["FAIL"], n_warn=n["WARN"], n_info=n["INFO"])
    )


def findings_to_markdown(findings: list[Finding], lang: str | None = None) -> str:
    if not findings:
        return t("cli.no_findings", lang) + "\n"
    lines = [
        f"| | ID | {t('common.cause', lang)} | {t('common.fix', lang)} |",
        "|---|---|---|---|",
    ]
    for f in sort_findings(findings):
        sev, rid, cause, fix = render_finding(f, lang)
        scope = f" `{f.scope}`" if f.scope else ""
        lines.append(f"| {sev} | {rid}{scope} | {cause} | {fix} |")
    return "\n".join(lines) + "\n"
