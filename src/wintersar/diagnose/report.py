"""Markdown diagnosis report: cause → fix → refs per finding (plan §5.5, rule 11.6/11.11)."""

from __future__ import annotations

import json
from typing import Any

from wintersar.i18n import t
from wintersar.io.schemas import Finding, sort_findings
from wintersar.util.masking import mask_mapping, mask_text
from wintersar.util.output import render_finding


def _code_block(text: str) -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}text\n{text}\n{fence}"


def _fmt_params(params: dict[str, Any]) -> str:
    return json.dumps(mask_mapping(params), ensure_ascii=False, sort_keys=True)


def finding_section(f: Finding, index: int, lang: str | None = None) -> str:
    sev, rid, cause, fix = render_finding(f, lang)
    scope = f" — `{mask_text(f.scope)}`" if f.scope else ""
    lines = [f"## {index}. [{sev}] {rid}{scope}", ""]
    lines.append(f"**{t('common.cause', lang)}**: {cause}")
    lines.append("")
    if fix:
        lines.append(f"**{t('common.fix', lang)}**: {fix}")
        lines.append("")
    ev = f.evidence or {}
    if ev.get("pattern_verified") is False:
        lines.append(f"> {t('diagnose.report.unverified', lang)}")
        lines.append("")
    if ev.get("engine_mismatch"):
        lines.append(f"> {t('diagnose.report.engine_mismatch', lang, engine=ev.get('engine'))}")
        lines.append("")
    if f.refs:
        lines.append(f"**{t('common.refs', lang)}**:")
        lines.extend(f"- {mask_text(str(r))}" for r in f.refs)
        lines.append("")
    if ev:
        header = f"**{t('diagnose.report.evidence', lang)}**"
        if "line" in ev:
            header += (
                " ("
                + t("diagnose.report.line", lang, line=ev.get("line"), count=ev.get("count", 1))
                + ")"
            )
        lines.append(header)
        excerpt = ev.get("excerpt")
        if excerpt:
            lines.append("")
            lines.append(_code_block(mask_text(str(excerpt))))
        rest = {
            k: v
            for k, v in ev.items()
            if k not in {"excerpt", "line", "count", "matched", "events"} and v not in (None, "")
        }
        if rest:
            lines.append("")
            lines.append(f"`{_fmt_params(rest)}`")
        lines.append("")
    return "\n".join(lines)


def retry_hint_section(hint: dict[str, Any], lang: str | None = None) -> str:
    lines = [f"## {t('diagnose.report.retry_hint', lang)}", ""]
    lines.append(
        t(
            "diagnose.report.retry_detail",
            lang,
            rule_id=hint.get("rule_id"),
            stage=hint.get("stage"),
            action=hint.get("action"),
        )
    )
    params = hint.get("params") or {}
    if params:
        lines.append("")
        lines.append(f"**{t('diagnose.report.retry_params', lang)}**: `{_fmt_params(params)}`")
    note = hint.get("note")
    if note:
        lines.append("")
        lines.append(f"> {mask_text(str(note))}")
    lines.append("")
    return "\n".join(lines)


def findings_report(
    findings: list[Finding],
    lang: str | None = None,
    *,
    source: str | None = None,
    retry_hint: dict[str, Any] | None = None,
    title: str | None = None,
) -> str:
    """Render findings as Markdown (severity order, cause → fix → refs → evidence)."""
    parts: list[str] = [f"# {title or t('diagnose.report.title', lang)}", ""]
    if source:
        parts.append(t("diagnose.report.source", lang, path=mask_text(source)))
        parts.append("")
    if not findings:
        parts.append(t("cli.no_findings", lang))
        parts.append("")
        return "\n".join(parts)
    n = {s: sum(1 for f in findings if f.severity == s) for s in ("FAIL", "WARN", "INFO")}
    parts.append(
        t("cli.findings_summary", lang, n_fail=n["FAIL"], n_warn=n["WARN"], n_info=n["INFO"])
    )
    parts.append("")
    for i, f in enumerate(sort_findings(findings), start=1):
        parts.append(finding_section(f, i, lang))
    if retry_hint:
        parts.append(retry_hint_section(retry_hint, lang))
    parts.append(f"_{t('diagnose.report.generated', lang)}_")
    parts.append("")
    return "\n".join(parts)


__all__ = ["finding_section", "findings_report", "retry_hint_section"]
