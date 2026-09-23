"""Console / JSON output helpers shared by every CLI sub-command.

Every command supports ``--json`` (plan §4.5): the QGIS plugin parses that output, so the
JSON envelope is stable: ``{"ok": bool, "command": str, "data": ..., "findings": [...]}``.

Error paths use the same contract (CLAUDE.md "envelope vs exit code", ADR-0091): a bad
input becomes a :class:`~wintersar.io.schemas.Finding` with a ``CLI-xxx`` (or module) rule
id, is printed as the envelope under ``--json`` or as "cause → fix" lines on stderr
otherwise, and the command exits 2 (usage) or 1 (action failed). :func:`exit_with_findings`
and :func:`cli_finding` are the two helpers every CLI uses for that.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from wintersar.i18n import t
from wintersar.io.schemas import Finding, sort_findings
from wintersar.util.clistate import state
from wintersar.util.masking import mask_mapping, mask_text

console = Console(stderr=False)
err_console = Console(stderr=True)

# CLI-xxx rule ids (ADR-0091). Text lives in i18n/{ko,en}.yaml (001/002) and
# i18n/{ko,en}/cli_help.yaml (003+); every id has ``cli.<ID>.cause`` and ``cli.<ID>.fix``.
CLI_UNEXPECTED = "CLI-001"
CLI_EXISTS = "CLI-002"
CLI_USAGE = "CLI-003"
CLI_CONFIG_MISSING = "CLI-004"
CLI_CONFIG_INVALID = "CLI-005"
CLI_INPUT_MISSING = "CLI-006"
CLI_INPUT_INVALID = "CLI-007"
CLI_BAD_VALUE = "CLI-008"
CLI_EMPTY_INPUT = "CLI-009"
CLI_COMPONENT_UNAVAILABLE = "CLI-010"


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
        # catalogue text and scopes are data, not rich markup: ``[unwrap run]`` or
        # ``[dry-run: …]`` would otherwise be parsed as a style tag and vanish
        table.add_row(
            f"[{colors[f.severity]}]{escape(sev)}[/]",
            escape(rid + scope),
            escape(cause),
            escape(fix),
        )
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


# ------------------------------------------------------------------ error-path contract


def cli_finding(
    rule_id: str,
    *,
    message_key: str | None = None,
    fix_key: str | None = None,
    severity: str = "FAIL",
    scope: str | None = None,
    **params: Any,
) -> Finding:
    """A ``CLI-xxx`` finding whose cause/fix keys default to ``cli.<ID>.cause`` / ``.fix``.

    A module may keep its own, more specific cause text (``message_key``) and still share
    the generic fix; string params are masked so the envelope never carries a home path.
    """
    masked = {k: mask_text(v) if isinstance(v, str) else v for k, v in params.items()}
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=message_key or f"cli.{rule_id}.cause",
        fix_key=fix_key or f"cli.{rule_id}.fix",
        params=masked,
        evidence=dict(masked),
        scope=scope,
    )


_ERROR_STYLES = {"FAIL": "red", "WARN": "yellow", "INFO": "cyan"}


def print_error_findings(findings: Sequence[Finding], lang: str | None = None) -> None:
    """stderr rendering of an error path: ``ID: cause`` (coloured by severity), then
    ``fix`` on the next line (rule 11.6 order). Text is printed verbatim, never as markup.
    """
    for f in findings:
        _sev, rid, cause, fix = render_finding(f, lang)
        err_console.print(
            f"{rid}: {cause}", style=_ERROR_STYLES.get(f.severity, "red"), markup=False
        )
        if fix:
            err_console.print(fix, markup=False)


def exit_with_findings(
    command: str,
    findings: Sequence[Finding],
    code: int = 2,
    data: Any = None,
) -> typer.Exit:
    """Report an error path and return the ``typer.Exit`` to raise.

    Under ``--json`` the envelope (``ok: false``) is the only thing written, and it goes to
    stdout; otherwise the findings are printed cause → fix on stderr. ``code`` follows the
    CLAUDE.md rule: 2 for bad input/usage, 1 for an action that failed.
    """
    fs = list(findings)
    if state.json:
        emit_json(command, data, fs, ok=False)
    else:
        print_error_findings(fs, state.lang)
    return typer.Exit(code=code)


def invoked_command(argv: Sequence[str] | None = None) -> str:
    """Best-effort command name for an envelope (``--json plan …`` → ``plan``).

    Sub-command groups are joined with a space (``unwrap run``) so the value matches what
    the commands themselves pass to :func:`emit_json`.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    words: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg.startswith("-"):
            if words:
                break
            skip_value = arg == "--lang"
            continue
        words.append(arg)
        if len(words) == 2:
            break
    if not words:
        return "wintersar"
    if len(words) == 2 and words[0] in _GROUPS:
        return " ".join(words)
    return words[0]


# groups whose sub-command name is part of the envelope's ``command`` field
_GROUPS: frozenset[str] = frozenset({"cache", "unwrap", "research"})


def report_unexpected(exc: BaseException, argv: Sequence[str] | None = None) -> None:
    """Turn an exception that escaped a sub-command into the normal output contract.

    Typer re-raises unexpected exceptions instead of converting them to an exit code
    (``Typer.__call__``), so without this the process ends in a traceback: no ``--json``
    envelope at all, and unmasked home paths in the stderr frames (rule 11.11).
    # source: .venv/lib/python3.11/site-packages/typer/main.py (Typer.__call__)
    """
    detail = mask_text(f"{type(exc).__name__}: {exc}")
    finding = Finding(
        rule_id=CLI_UNEXPECTED,
        severity="FAIL",
        message_key="cli.CLI-001.cause",
        fix_key="cli.CLI-001.fix",
        params={"error": detail},
        evidence={"error": detail},
    )
    if state.json:
        emit_json(invoked_command(argv), {"error": detail}, [finding], ok=False)
    else:
        err_console.print(t("cli.CLI-001.cause", error=detail), style="red", markup=False)
        err_console.print(t("cli.CLI-001.fix", error=detail), markup=False)


def print_action_failed(command: str, error: str) -> None:
    """Text-mode line for an action that failed without a finding of its own (exit 1)."""
    err_console.print(
        t("cli.action_failed", command=command, error=mask_text(error)), style="red", markup=False
    )
